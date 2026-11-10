"""FastAPI application factory. Run with: uvicorn --factory app.main:create_app"""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import EnvSettings, Settings, config_path, load_settings
from .deps import apply_settings
from .jobs import JobContext, JobManager
from .routes import auth, files, jobs, settings
from .security import (
    SESSION_COOKIE,
    LoginRateLimiter,
    SecurityMiddleware,
    SessionStore,
    origin_allowed,
)

log = logging.getLogger("subtitle_translator")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _setup_path(path: str) -> bool:
    return path == "/setup" or path == "/api/setup" or path.startswith("/api/setup/")


def create_app(env: EnvSettings | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = env or EnvSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.settings is None:
            log.warning("=" * 64)
            log.warning("SETUP MODE: open the web UI and enter this one-time setup token:")
            log.warning("SETUP TOKEN: %s", app.state.setup_token)
            log.warning("=" * 64)
        await app.state.jobs.start()
        try:
            yield
        finally:
            await app.state.jobs.stop()

    app = FastAPI(
        title="Subtitle Translator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    state = app.state
    state.env = env
    state.config_path = config_path(env)
    state.sessions = SessionStore()
    state.limiter = LoginRateLimiter()
    state.setup_lock = threading.Lock()
    state.settings = None
    state.client = None
    state.setup_token = None

    current = load_settings(state.config_path)
    if current is None:
        state.setup_token = secrets.token_urlsafe(32)
    else:
        apply_settings(app, current)
        if not Path(current.media_root).is_dir():
            log.warning("Configured media folder is not available")

    def job_context() -> JobContext:
        s: Settings = state.settings
        return JobContext(
            root=Path(s.media_root),
            client=state.client,
            max_file_bytes=s.max_file_bytes,
            microdvd_fps=s.microdvd_fps,
            sentence_grouping=s.sentence_grouping,
            max_gap_seconds=s.max_gap_seconds,
        )

    state.jobs = JobManager(job_context)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        configured = state.settings is not None
        if not configured:
            allowed = _setup_path(path) or path.startswith("/static/") or path == "/healthz"
            if not allowed:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Setup required"}, status_code=409)
                return RedirectResponse("/setup", status_code=303)
        elif _setup_path(path):
            return JSONResponse({"detail": "Not found"}, status_code=404)

        if request.method not in SAFE_METHODS and path.startswith("/api/"):
            if not origin_allowed(dict(request.headers), env.allowed_origins):
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            problems.append(f"{loc}: {err.get('msg', 'invalid')}" if loc else err.get("msg", "invalid"))
        return JSONResponse({"detail": "; ".join(problems) or "Invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    app.include_router(auth.router)
    app.include_router(files.router)
    app.include_router(jobs.router)
    app.include_router(settings.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _page(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, media_type="text/html")

    def _logged_in(request: Request) -> bool:
        return state.sessions.get(request.cookies.get(SESSION_COOKIE)) is not None

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        if not _logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return _page("index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request) -> Response:
        if _logged_in(request):
            return RedirectResponse("/", status_code=303)
        return _page("login.html")

    @app.get("/setup", include_in_schema=False)
    async def setup_page() -> Response:
        if state.settings is not None:
            raise HTTPException(status_code=404)
        return _page("setup.html")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    app.add_middleware(SecurityMiddleware, max_body_bytes=env.max_body_bytes)
    return app
