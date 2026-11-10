"""Settings, language list, connection test and password change."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import LANG_CODE_PATTERN, Settings, save_settings
from ..deps import apply_settings, client_ip, require_csrf, require_session, require_settings
from ..paths import PathError, to_relative, validate_media_root
from ..security import Session, hash_password, password_problem, verify_password
from ..translator import TranslatorError, is_insecure_remote
from .auth import _rate_limited, list_subdirs, test_connection

log = logging.getLogger("subtitle_translator.settings")
router = APIRouter(dependencies=[Depends(require_session)])


def _public(request: Request, s: Settings) -> dict:
    base = request.app.state.env.media_base
    try:
        media_rel = to_relative(base, Path(s.media_root))
    except (ValueError, OSError):
        media_rel = ""
    return {
        "username": s.username,
        "libretranslate_url": s.libretranslate_url,
        "api_key_set": bool(s.libretranslate_api_key),
        "insecure_translator_url": is_insecure_remote(s.libretranslate_url),
        "media_root": media_rel,
        "default_source": s.default_source,
        "default_target": s.default_target,
        "batch_size": s.batch_size,
        "max_file_mb": s.max_file_mb,
        "max_files_per_job": s.max_files_per_job,
        "microdvd_fps": s.microdvd_fps,
        "sentence_grouping": s.sentence_grouping,
        "max_gap_seconds": s.max_gap_seconds,
        "session_idle_hours": s.session_idle_hours,
        "session_absolute_hours": s.session_absolute_hours,
    }


@router.get("/api/settings")
def get_settings(request: Request, settings: Settings = Depends(require_settings)) -> dict:
    return _public(request, settings)


class SettingsIn(BaseModel):
    libretranslate_url: str = Field(max_length=2048)
    # None = keep the stored key, "" = remove it
    libretranslate_api_key: str | None = Field(default=None, max_length=256)
    media_root: str = Field(default="", max_length=4096)
    default_source: str = Field(pattern=LANG_CODE_PATTERN)
    default_target: str = Field(pattern=LANG_CODE_PATTERN)
    batch_size: int = Field(ge=1, le=200)
    max_file_mb: float = Field(gt=0, le=50)
    max_files_per_job: int = Field(ge=1, le=5000)
    microdvd_fps: float = Field(gt=1, le=240)
    sentence_grouping: bool = True
    max_gap_seconds: float = Field(default=2.0, ge=0, le=10)
    session_idle_hours: float = Field(gt=0, le=24 * 30)
    session_absolute_hours: float = Field(gt=0, le=24 * 90)


@router.put("/api/settings", dependencies=[Depends(require_csrf)])
def update_settings(
    body: SettingsIn, request: Request, settings: Settings = Depends(require_settings)
) -> dict:
    try:
        root = validate_media_root(request.app.state.env.media_base, body.media_root)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=f"Media folder: {exc}") from exc
    data = settings.model_dump()
    data.update(body.model_dump(exclude={"libretranslate_api_key", "media_root"}))
    data["media_root"] = str(root)
    if body.libretranslate_api_key is not None:
        data["libretranslate_api_key"] = body.libretranslate_api_key
    try:
        new = Settings.model_validate(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid settings (check the LibreTranslate URL)") from exc
    save_settings(request.app.state.config_path, new)
    apply_settings(request.app, new)
    log.info("Settings updated")
    return _public(request, new)


@router.get("/api/settings/folders")
def settings_folders(request: Request, path: str = "") -> dict:
    return list_subdirs(request.app.state.env.media_base, path[:4096])


class TestIn(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=256)


@router.post("/api/settings/test", dependencies=[Depends(require_csrf)])
async def settings_test(
    body: TestIn, request: Request, settings: Settings = Depends(require_settings)
) -> dict:
    url = body.url or settings.libretranslate_url
    api_key = body.api_key if body.api_key is not None else settings.libretranslate_api_key
    return await test_connection(url, api_key)


@router.get("/api/languages")
async def languages(request: Request) -> dict:
    try:
        langs = await request.app.state.client.languages()
    except TranslatorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"languages": langs}


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)
    new_password_confirm: str = Field(max_length=256)


@router.post("/api/settings/password")
def change_password(
    body: PasswordIn,
    request: Request,
    sess: Session = Depends(require_csrf),
    settings: Settings = Depends(require_settings),
) -> dict:
    key = f"pw:{client_ip(request)}"
    _rate_limited(request, key)
    if not verify_password(settings.password_hash, body.current_password):
        request.app.state.limiter.failure(key)
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    request.app.state.limiter.success(key)
    problem = password_problem(body.new_password, body.new_password_confirm)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    new = settings.model_copy(update={"password_hash": hash_password(body.new_password)})
    save_settings(request.app.state.config_path, new)
    apply_settings(request.app, new)
    request.app.state.sessions.delete_all(except_id=sess.id)
    log.info("Password changed; other sessions logged out")
    return {"ok": True}
