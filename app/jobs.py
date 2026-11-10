"""In-memory job queue with a single async worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PathError, resolve_media_path, to_relative
from .subtitles import (
    SubtitleError,
    dump_subtitles,
    language_from_name,
    load_nonempty,
    output_path,
    same_language,
    subtitle_format,
    translate_subtitles,
)
from .translator import LibreTranslateClient, TranslationCancelled, TranslatorError

log = logging.getLogger("subtitle_translator.jobs")

_UMASK = os.umask(0)
os.umask(_UMASK)

MAX_HISTORY = 50
HISTORY_TTL = 24 * 3600
MAX_QUEUED_JOBS = 20


class QueueFull(Exception):
    pass


@dataclass
class JobContext:
    root: Path
    client: LibreTranslateClient
    max_file_bytes: int
    microdvd_fps: float
    sentence_grouping: bool = True
    max_gap_seconds: float = 2.0


@dataclass
class FileResult:
    path: str
    status: str = "pending"  # pending|running|done|skipped|error|cancelled
    message: str = ""
    output: str = ""


@dataclass
class Job:
    id: str
    source: str
    target: str
    overwrite: bool
    files: list[FileResult]
    created: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "queued"  # queued|running|done|cancelled
    cancel_requested: bool = False
    current_done: int = 0
    current_total: int = 0

    def to_dict(self, include_files: bool = True) -> dict:
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.status] = counts.get(f.status, 0) + 1
        data = {
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "target": self.target,
            "overwrite": self.overwrite,
            "created": self.created,
            "finished": self.finished,
            "total": len(self.files),
            "counts": counts,
            "current": {"done": self.current_done, "total": self.current_total},
        }
        if include_files:
            data["files"] = [vars(f).copy() for f in self.files]
        return data


def write_output(path: Path, data: bytes, overwrite: bool) -> None:
    """Write via a temp file in the same directory; never clobber unless asked."""
    fd, tmp_name = tempfile.mkstemp(prefix=".st-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o666 & ~_UMASK)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp_name, path)
            return
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            raise
        except OSError:
            # Filesystem without hard links (e.g. some SMB/FUSE mounts).
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            out_fd = os.open(path, flags, 0o666)
            try:
                with os.fdopen(out_fd, "wb") as fh:
                    fh.write(data)
            except BaseException:
                os.unlink(path)
                raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


class JobManager:
    def __init__(self, context_factory: Callable[[], JobContext]) -> None:
        self._context_factory = context_factory
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] | None = None
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._run(), name="job-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def submit(self, files: list[str], source: str, target: str, overwrite: bool) -> Job:
        if self._queue is None:
            raise RuntimeError("Job manager not started")
        self._purge()
        queued = sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
        if queued >= MAX_QUEUED_JOBS:
            raise QueueFull
        unique = list(dict.fromkeys(files))
        job = Job(
            id=secrets.token_urlsafe(12),
            source=source,
            target=target,
            overwrite=overwrite,
            files=[FileResult(path=p) for p in unique],
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        self._purge()
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            job.cancel_requested = True
            if job.status == "queued":
                self._finish(job, "cancelled")
        return job

    def _purge(self) -> None:
        now = time.time()
        finished = [j for j in self._jobs.values() if j.finished is not None]
        finished.sort(key=lambda j: j.created)
        excess = len(self._jobs) - MAX_HISTORY
        for job in finished:
            if excess > 0 or now - (job.finished or now) > HISTORY_TTL:
                del self._jobs[job.id]
                excess -= 1

    def _finish(self, job: Job, status: str) -> None:
        for f in job.files:
            if f.status in ("pending", "running"):
                f.status = "cancelled"
        job.status = status
        job.finished = time.time()

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            job.status = "running"
            try:
                await self.run_job(job)
            except Exception:
                log.exception("Job %s crashed", job.id)
            finally:
                if job.status == "running":
                    self._finish(job, "cancelled" if job.cancel_requested else "done")

    async def run_job(self, job: Job) -> None:
        for fr in job.files:
            if job.cancel_requested:
                break
            fr.status = "running"
            job.current_done = job.current_total = 0
            try:
                ctx = self._context_factory()
                await self._process(job, fr, ctx)
            except TranslationCancelled:
                fr.status = "cancelled"
                break
            except (PathError, SubtitleError, TranslatorError) as exc:
                fr.status, fr.message = "error", str(exc)
            except FileExistsError:
                fr.status, fr.message = "skipped", "Translation already exists"
            except OSError as exc:
                log.warning("I/O error on %s: %s", fr.path, exc)
                fr.status, fr.message = "error", "File system error (see server log)"
            except Exception:
                log.exception("Unexpected error on %s", fr.path)
                fr.status, fr.message = "error", "Unexpected error (see server log)"

    async def _process(self, job: Job, fr: FileResult, ctx: JobContext) -> None:
        src = resolve_media_path(ctx.root, fr.path)
        fmt = subtitle_format(src)
        if fmt is None or not src.is_file():
            raise SubtitleError("Not a supported subtitle file")
        if src.stat().st_size > ctx.max_file_bytes:
            raise SubtitleError("File is larger than the configured limit")

        source = job.source
        if source == "auto":
            source = language_from_name(src) or "auto"
        if same_language(source, job.target):
            fr.status, fr.message = "skipped", "Already in the target language"
            return

        out = output_path(src, job.target)
        if out == src:
            fr.status, fr.message = "skipped", "Output would overwrite the source"
            return
        parent_rel = to_relative(ctx.root, out.parent)
        fr.output = f"{parent_rel}/{out.name}" if parent_rel else out.name
        if out.is_dir():
            raise SubtitleError("Output path is a directory")
        if (out.exists() or out.is_symlink()) and not job.overwrite:
            fr.status, fr.message = "skipped", "Translation already exists"
            return

        data = await asyncio.to_thread(src.read_bytes)
        subs = await asyncio.to_thread(load_nonempty, data, fmt, ctx.microdvd_fps)

        def progress(done: int, total: int) -> None:
            job.current_done, job.current_total = done, total

        async def translate(texts: list[str]) -> list[str]:
            job.current_total = len(texts)
            return await ctx.client.translate(
                texts, source, job.target, progress=progress, cancelled=lambda: job.cancel_requested
            )

        result = await translate_subtitles(
            subs, translate, sentence_grouping=ctx.sentence_grouping, max_gap_seconds=ctx.max_gap_seconds
        )
        if job.cancel_requested:
            raise TranslationCancelled
        payload = dump_subtitles(subs, fmt)
        await asyncio.to_thread(write_output, out, payload, job.overwrite)
        fr.status = "done"
        fr.message = f"{result.cues_translated} cues translated"
