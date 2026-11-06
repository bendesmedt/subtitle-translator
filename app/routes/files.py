"""Browse, scan and search the media folder."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..config import LANG_CODE_PATTERN, Settings
from ..deps import media_root, require_csrf, require_session, require_settings
from ..paths import PathError, entry_inside_root, resolve_media_path, to_relative
from ..subtitles import (
    SubtitleError,
    language_from_name,
    load_subtitles,
    output_path,
    same_language,
    sample_text,
    subtitle_format,
)
from ..translator import TranslatorError

router = APIRouter(dependencies=[Depends(require_session)])

MAX_LISTING = 5000
MAX_SCAN_RESULTS = 2000
MAX_SEARCH_RESULTS = 500
MAX_WALK_FILES = 200_000
MAX_DETECTIONS = 200


def _folder(root: Path, rel: str) -> Path:
    try:
        folder = resolve_media_path(root, rel)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="Not a folder")
    return folder


def _is_vobsub(path: Path) -> bool:
    return path.suffix.lower() == ".sub" and (
        path.with_suffix(".idx").exists() or path.with_suffix(".IDX").exists()
    )


def _file_info(root: Path, path: Path, target: str | None) -> dict:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    info = {
        "name": path.name,
        "path": to_relative(root, path),
        "type": "file",
        "size": size,
        "lang": language_from_name(path),
    }
    if target:
        out = output_path(path, target)
        info["translated"] = out.exists()
        info["is_target"] = same_language(info["lang"], target)
    return info


def walk_subtitles(root: Path, start: Path) -> Iterator[Path]:
    """Recursively yield subtitle files under ``start`` that are safe to expose."""
    root_resolved = root.resolve()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=str.lower)
        for name in sorted(filenames, key=str.lower):
            seen += 1
            if seen > MAX_WALK_FILES:
                return
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if subtitle_format(path) is None or _is_vobsub(path):
                continue
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
                    continue
            yield path


@router.get("/api/files")
def browse(
    path: str = Query(default="", max_length=4096),
    target: str | None = Query(default=None, pattern=LANG_CODE_PATTERN),
    root: Path = Depends(media_root),
) -> dict:
    folder = _folder(root, path)
    root_resolved = root.resolve()
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                resolved = entry_inside_root(root_resolved, entry)
                if resolved is None:
                    continue
                entry_path = folder / entry.name
                if resolved.is_dir():
                    dirs.append({"name": entry.name, "path": to_relative(root, entry_path), "type": "dir"})
                elif subtitle_format(entry_path) and not _is_vobsub(entry_path) and resolved.is_file():
                    files.append(_file_info(root, entry_path, target))
                if len(dirs) + len(files) >= MAX_LISTING:
                    break
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Folder cannot be read") from exc
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    rel = to_relative(root, folder)
    crumbs = [{"name": "Media", "path": ""}]
    acc: list[str] = []
    for part in rel.split("/") if rel else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return {"path": rel, "breadcrumbs": crumbs, "entries": dirs + files}


class ScanIn(BaseModel):
    path: str = Field(default="", max_length=4096)
    source: str = Field(pattern=LANG_CODE_PATTERN)
    target: str = Field(pattern=LANG_CODE_PATTERN)
    mode: Literal["code", "none", "detect"] = "code"
    include_existing: bool = False


async def _detect(request: Request, path: Path, settings: Settings) -> str | None:
    fmt = subtitle_format(path)
    try:
        if path.stat().st_size > settings.max_file_bytes:
            return None
        data = await asyncio.to_thread(path.read_bytes)
        subs = await asyncio.to_thread(load_subtitles, data, fmt, settings.microdvd_fps)
    except (OSError, SubtitleError):
        return None
    text = sample_text(subs)
    if not text:
        return None
    result = await request.app.state.client.detect(text)
    if result and result[1] >= 50:
        return result[0]
    return None


@router.post("/api/scan", dependencies=[Depends(require_csrf)])
async def scan(
    body: ScanIn,
    request: Request,
    root: Path = Depends(media_root),
    settings: Settings = Depends(require_settings),
) -> dict:
    """List subtitles in the source language that lack a translation to the target."""
    if same_language(body.source, body.target):
        raise HTTPException(status_code=400, detail="Source and target language are the same")
    folder = _folder(root, body.path)
    candidates = await asyncio.to_thread(lambda: list(walk_subtitles(root, folder)))

    results: list[dict] = []
    truncated = False
    detections = 0
    detect_skipped = 0
    for path in candidates:
        lang = language_from_name(path)
        detected = None
        if body.mode == "code":
            if not same_language(lang, body.source):
                continue
        else:
            if lang is not None:
                continue
            if body.mode == "detect":
                if detections >= MAX_DETECTIONS:
                    detect_skipped += 1
                    continue
                detections += 1
                try:
                    detected = await _detect(request, path, settings)
                except TranslatorError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                if not same_language(detected, body.source):
                    continue
        exists = output_path(path, body.target).exists()
        if exists and not body.include_existing:
            continue
        info = _file_info(root, path, body.target)
        info["detected"] = detected
        results.append(info)
        if len(results) >= MAX_SCAN_RESULTS:
            truncated = True
            break
    return {
        "results": results,
        "truncated": truncated,
        "scanned": len(candidates),
        "detect_skipped": detect_skipped,
    }


@router.get("/api/search")
def search(
    q: str = Query(min_length=1, max_length=200),
    path: str = Query(default="", max_length=4096),
    target: str | None = Query(default=None, pattern=LANG_CODE_PATTERN),
    root: Path = Depends(media_root),
) -> dict:
    """Case-insensitive substring search on media-relative file paths."""
    folder = _folder(root, path)
    needle = q.strip().casefold()
    if not needle:
        raise HTTPException(status_code=400, detail="Empty search")
    results: list[dict] = []
    truncated = False
    for file in walk_subtitles(root, folder):
        rel = to_relative(root, file)
        if needle in rel.casefold():
            results.append(_file_info(root, file, target))
            if len(results) >= MAX_SEARCH_RESULTS:
                truncated = True
                break
    return {"results": results, "truncated": truncated}
