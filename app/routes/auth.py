"""Login, logout, session info and the first-run setup wizard."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..config import LANG_CODE_PATTERN, Settings, save_settings, validate_service_url
from ..deps import apply_settings, client_ip, require_csrf, require_session, require_settings
from ..paths import PathError, entry_inside_root, resolve_media_path, to_relative, validate_media_root
from ..security import (
    SESSION_COOKIE,
    Session,
    check_credentials,
    hash_password,
    password_problem,
    tokens_equal,
)
from ..translator import LibreTranslateClient, TranslatorError, is_insecure_remote

log = logging.getLogger("subtitle_translator.auth")
router = APIRouter()


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


def _rate_limited(request: Request, key: str) -> None:
    wait = request.app.state.limiter.retry_after(key)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts, try again later",
            headers={"Retry-After": str(math.ceil(wait))},
        )


def _set_session_cookie(request: Request, response: Response, sess: Session) -> None:
    state = request.app.state
    response.set_cookie(
        SESSION_COOKIE,
        sess.id,
        max_age=int(state.sessions.absolute_seconds),
        httponly=True,
        samesite="strict",
        secure=state.env.cookie_secure,
        path="/",
    )


@router.post("/api/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    settings: Settings = require_settings(request)
    ip = client_ip(request)
    _rate_limited(request, ip)
    if not check_credentials(settings.username, settings.password_hash, body.username, body.password):
        request.app.state.limiter.failure(ip)
        log.warning("Failed login attempt from %s", ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    request.app.state.limiter.success(ip)
    old = request.cookies.get(SESSION_COOKIE)
    request.app.state.sessions.delete(old)
    sess = request.app.state.sessions.create(settings.username)
    _set_session_cookie(request, response, sess)
    log.info("User logged in from %s", ip)
    return {"ok": True}


@router.post("/api/logout")
def logout(request: Request, response: Response, sess: Session = Depends(require_csrf)) -> dict:
    request.app.state.sessions.delete(sess.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/session")
def session_info(request: Request, sess: Session = Depends(require_session)) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "username": sess.username,
        "csrf_token": sess.csrf_token,
        "default_source": settings.default_source,
        "default_target": settings.default_target,
        "max_files_per_job": settings.max_files_per_job,
        "insecure_translator_url": is_insecure_remote(settings.libretranslate_url),
    }


# --- Setup wizard -------------------------------------------------------------------


class SetupTokenIn(BaseModel):
    token: str = Field(max_length=256)


class SetupFoldersIn(SetupTokenIn):
    path: str = Field(default="", max_length=4096)


class SetupTestIn(SetupTokenIn):
    url: str = Field(max_length=2048)
    api_key: str = Field(default="", max_length=256)


class SetupIn(SetupTokenIn):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(max_length=256)
    password_confirm: str = Field(max_length=256)
    libretranslate_url: str = Field(max_length=2048)
    libretranslate_api_key: str = Field(default="", max_length=256)
    media_root: str = Field(default="", max_length=4096)
    default_source: str = Field(default="en", pattern=LANG_CODE_PATTERN)
    default_target: str = Field(default="fr", pattern=LANG_CODE_PATTERN)


def _check_setup_token(request: Request, token: str) -> None:
    state = request.app.state
    if state.settings is not None or state.setup_token is None:
        raise HTTPException(status_code=404, detail="Not found")
    key = f"setup:{client_ip(request)}"
    _rate_limited(request, key)
    if not tokens_equal(token, state.setup_token):
        state.limiter.failure(key)
        log.warning("Invalid setup token from %s", client_ip(request))
        raise HTTPException(status_code=403, detail="Invalid setup token")


def list_subdirs(base: Path, rel: str) -> dict:
    try:
        folder = resolve_media_path(base, rel)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="Not a folder")
    base_resolved = base.resolve()
    dirs = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                resolved = entry_inside_root(base_resolved, entry)
                if resolved is not None and resolved.is_dir():
                    dirs.append(entry.name)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Folder cannot be read") from exc
    return {"path": to_relative(base, folder), "folders": sorted(dirs, key=str.lower)[:1000]}


@router.post("/api/setup/check")
def setup_check(body: SetupTokenIn, request: Request) -> dict:
    _check_setup_token(request, body.token)
    base: Path = request.app.state.env.media_base
    return {"ok": True, "media_base_exists": base.is_dir()}


@router.post("/api/setup/folders")
def setup_folders(body: SetupFoldersIn, request: Request) -> dict:
    _check_setup_token(request, body.token)
    return list_subdirs(request.app.state.env.media_base, body.path)


async def test_connection(url: str, api_key: str) -> dict:
    try:
        url = validate_service_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client = LibreTranslateClient(base_url=url, api_key=api_key, retries=0, timeout=15)
    try:
        langs = await client.languages(use_cache=False)
    except TranslatorError as exc:
        return {"ok": False, "error": str(exc), "languages": [], "insecure": is_insecure_remote(url)}
    return {"ok": True, "languages": langs, "insecure": is_insecure_remote(url)}


@router.post("/api/setup/test")
async def setup_test(body: SetupTestIn, request: Request) -> dict:
    _check_setup_token(request, body.token)
    return await test_connection(body.url, body.api_key)


@router.post("/api/setup")
def setup_save(body: SetupIn, request: Request) -> dict:
    _check_setup_token(request, body.token)
    problem = password_problem(body.password, body.password_confirm)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    try:
        root = validate_media_root(request.app.state.env.media_base, body.media_root)
    except PathError as exc:
        raise HTTPException(status_code=400, detail=f"Media folder: {exc}") from exc
    try:
        settings = Settings(
            username=body.username,
            password_hash=hash_password(body.password),
            libretranslate_url=body.libretranslate_url,
            libretranslate_api_key=body.libretranslate_api_key,
            media_root=str(root),
            default_source=body.default_source,
            default_target=body.default_target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid LibreTranslate URL") from exc

    state = request.app.state
    with state.setup_lock:
        if state.settings is not None:
            raise HTTPException(status_code=404, detail="Not found")
        save_settings(state.config_path, settings)
        apply_settings(request.app, settings)
        state.setup_token = None
    log.info("Setup completed; setup mode disabled")
    return {"ok": True}
