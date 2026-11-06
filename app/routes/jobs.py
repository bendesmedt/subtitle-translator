"""Translation jobs: create, list, status, cancel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import LANG_CODE_PATTERN, Settings
from ..deps import require_csrf, require_session, require_settings
from ..jobs import QueueFull
from ..subtitles import same_language

router = APIRouter(dependencies=[Depends(require_session)])

SOURCE_PATTERN = rf"^auto$|{LANG_CODE_PATTERN}"


class JobIn(BaseModel):
    files: list[str] = Field(min_length=1, max_length=5000)
    source: str = Field(pattern=SOURCE_PATTERN)
    target: str = Field(pattern=LANG_CODE_PATTERN)
    overwrite: bool = False


@router.post("/api/jobs", dependencies=[Depends(require_csrf)])
async def create_job(body: JobIn, request: Request, settings: Settings = Depends(require_settings)) -> dict:
    if len(body.files) > settings.max_files_per_job:
        raise HTTPException(
            status_code=400, detail=f"Too many files (max {settings.max_files_per_job} per job)"
        )
    if any(len(f) > 4096 or "\x00" in f for f in body.files):
        raise HTTPException(status_code=400, detail="Invalid path")
    if same_language(body.source, body.target):
        raise HTTPException(status_code=400, detail="Source and target language are the same")
    try:
        job = request.app.state.jobs.submit(body.files, body.source, body.target, body.overwrite)
    except QueueFull as exc:
        raise HTTPException(status_code=429, detail="Too many queued jobs, try again later") from exc
    return job.to_dict(include_files=False)


@router.get("/api/jobs")
async def list_jobs(request: Request) -> dict:
    return {"jobs": [j.to_dict(include_files=False) for j in request.app.state.jobs.list()]}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@router.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_csrf)])
async def cancel_job(job_id: str, request: Request) -> dict:
    job = request.app.state.jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict(include_files=False)
