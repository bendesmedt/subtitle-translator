# AGENTS.md

Guidance for contributors and coding agents working on this repository.

## What this is

A lightweight, single-user FastAPI app that translates subtitle files in a mounted media folder through a
self-hosted LibreTranslate instance. Deployed with Docker Compose (see README.md).

## Layout

| Path | Responsibility |
|------|----------------|
| `app/main.py` | App factory (`create_app`), lifespan, `gate` middleware (setup-mode gating, Origin and Content-Type checks on unsafe `/api/*`), page routes, error handlers |
| `app/config.py` | `EnvSettings` (environment) and `Settings` (persisted `CONFIG_DIR/config.json`, written atomically with mode 0600) |
| `app/security.py` | Argon2id hashing, in-memory `SessionStore`, `LoginRateLimiter`, origin check, `SecurityMiddleware` (security headers, body size limit) |
| `app/deps.py` | FastAPI dependencies (`require_session`, `require_csrf`) and `apply_settings` |
| `app/paths.py` | **All** filesystem path handling: `resolve_media_path`, listing, walking. Hides hidden entries and symlinks escaping the media root |
| `app/subtitles.py` | Format detection, decoding, pysubs2 load/dump, filename language codes, tag protection, `translate_subtitles` |
| `app/translator.py` | Async LibreTranslate client (batching, retries, `/languages` cache, `/detect`) |
| `app/jobs.py` | `JobManager` (single asyncio worker, in-memory history) and `write_output` (no-clobber atomic write) |
| `app/routes/` | API routers: `auth` (login, setup wizard), `files` (browse/scan/search), `jobs`, `settings` |
| `static/` | Vanilla HTML/CSS/JS frontend, no build step |
| `tests/` | pytest suite; LibreTranslate is mocked with respx |

## Conventions

- **Security first; the repo is public.** Never commit secrets, `config/` or `.env`.
- User-supplied paths are always relative to the media root and must go through `app.paths.resolve_media_path`.
  Never build filesystem paths from request data in any other way.
- Every state-changing endpoint depends on `require_csrf` (header `X-CSRF-Token`). Read-only endpoints
  depend on `require_session`.
- Error messages returned to the client are generic. Never return absolute paths, upstream response bodies
  or stack traces. Log details server-side instead.
- Frontend: no inline scripts/styles (strict CSP). Build DOM with `ST.el(...)` / `textContent`. Never use
  `innerHTML` with data.
- Job endpoints are `async def` because `JobManager` uses an `asyncio.Queue`, which is not thread-safe.
- Translated files are written with `write_output` only. It never overwrites unless `overwrite=True`.
- Run one uvicorn worker: sessions, rate limits and jobs live in memory.
- Dependencies: runtime deps in `requirements.in`, compiled with hashes into `requirements.txt` for
  Python 3.12 (`pip-compile --generate-hashes`, run inside `python:3.12-slim`). Ask before adding any.
- Style: ruff (`pyproject.toml`), line length 120.

## Checks

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
docker build -t subtitle-translator:test .
```

Add or update tests with every behaviour change.
