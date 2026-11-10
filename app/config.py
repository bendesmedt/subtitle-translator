"""Application settings: environment, persisted config file and validation."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("subtitle_translator.config")

CONFIG_FILENAME = "config.json"
LANG_CODE_PATTERN = r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EnvSettings:
    """Deployment settings that come from the environment, not from the UI."""

    config_dir: Path
    media_base: Path
    cookie_secure: bool = False
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    max_body_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> EnvSettings:
        origins = {
            o.strip().rstrip("/").lower()
            for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        }
        return cls(
            config_dir=Path(os.environ.get("CONFIG_DIR", "/config")),
            media_base=Path(os.environ.get("MEDIA_BASE", "/media")),
            cookie_secure=_env_bool("COOKIE_SECURE"),
            allowed_origins=frozenset(origins),
        )


def validate_service_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must start with http:// or https:// and include a host")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("URL must not contain a query string or fragment")
    return value.rstrip("/")


class Settings(BaseModel):
    """Persisted application configuration (config.json)."""

    username: str = Field(min_length=1, max_length=64)
    password_hash: str = Field(min_length=1)
    libretranslate_url: str
    libretranslate_api_key: str = Field(default="", max_length=256)
    media_root: str
    default_source: str = Field(default="en", pattern=LANG_CODE_PATTERN)
    default_target: str = Field(default="fr", pattern=LANG_CODE_PATTERN)
    batch_size: int = Field(default=50, ge=1, le=200)
    max_file_mb: float = Field(default=5.0, gt=0, le=50)
    max_files_per_job: int = Field(default=500, ge=1, le=5000)
    microdvd_fps: float = Field(default=23.976, gt=1, le=240)
    sentence_grouping: bool = True
    max_gap_seconds: float = Field(default=2.0, ge=0, le=10)
    session_idle_hours: float = Field(default=12, gt=0, le=24 * 30)
    session_absolute_hours: float = Field(default=72, gt=0, le=24 * 90)

    @field_validator("libretranslate_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return validate_service_url(v)

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)


def config_path(env: EnvSettings) -> Path:
    return env.config_dir / CONFIG_FILENAME


def load_settings(path: Path) -> Settings | None:
    """Return the persisted settings, or None when the app is not set up yet."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Settings.model_validate(data)
    except (OSError, ValueError, ValidationError) as exc:
        # Refuse to start instead of silently re-entering setup mode, which would
        # let anyone with access to the logs take over the instance.
        raise RuntimeError(f"Config file {path} is unreadable or invalid") from exc


def save_settings(path: Path, settings: Settings) -> None:
    """Atomically write the settings with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings.model_dump(), indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
