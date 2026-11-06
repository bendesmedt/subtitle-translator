"""Shared FastAPI dependencies and helpers."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request

from .config import EnvSettings, Settings
from .security import CSRF_HEADER, SESSION_COOKIE, Session, tokens_equal
from .translator import LibreTranslateClient


def env(request: Request) -> EnvSettings:
    return request.app.state.env


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_settings(request: Request) -> Settings:
    settings = request.app.state.settings
    if settings is None:
        raise HTTPException(status_code=409, detail="Setup required")
    return settings


def require_session(request: Request, _: Settings = Depends(require_settings)) -> Session:
    sess = request.app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return sess


def require_csrf(request: Request, sess: Session = Depends(require_session)) -> Session:
    token = request.headers.get(CSRF_HEADER, "")
    if not token or not tokens_equal(token, sess.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return sess


def media_root(settings: Settings = Depends(require_settings)) -> Path:
    root = Path(settings.media_root)
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="Media folder is not available")
    return root


def apply_settings(app: FastAPI, new: Settings) -> None:
    """Install new settings into the running app (client, session timeouts)."""
    state = app.state
    state.settings = new
    state.client = LibreTranslateClient(
        base_url=new.libretranslate_url,
        api_key=new.libretranslate_api_key,
        batch_size=new.batch_size,
    )
    state.sessions.idle_seconds = new.session_idle_hours * 3600
    state.sessions.absolute_seconds = new.session_absolute_hours * 3600
