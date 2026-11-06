from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import EnvSettings, Settings, save_settings
from app.main import create_app
from app.security import hash_password

ORIGIN = "http://testserver"
USERNAME = "admin"
PASSWORD = "correct horse battery"
LT_URL = "http://lt.test"

_HASH = hash_password(PASSWORD)


@pytest.fixture
def media(tmp_path: Path) -> Path:
    base = tmp_path / "media"
    (base / "Movies").mkdir(parents=True)
    return base


@pytest.fixture
def env(tmp_path: Path, media: Path) -> EnvSettings:
    return EnvSettings(config_dir=tmp_path / "config", media_base=media)


def write_config(env: EnvSettings, **overrides) -> Settings:
    data = dict(
        username=USERNAME,
        password_hash=_HASH,
        libretranslate_url=LT_URL,
        media_root=str((env.media_base / "Movies").resolve()),
        default_source="en",
        default_target="fr",
    )
    data.update(overrides)
    settings = Settings(**data)
    save_settings(env.config_dir / "config.json", settings)
    return settings


def make_client(env: EnvSettings) -> TestClient:
    app = create_app(env)
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


@pytest.fixture
def setup_client(env: EnvSettings):
    with make_client(env) as client:
        yield client


@pytest.fixture
def client(env: EnvSettings):
    write_config(env)
    with make_client(env) as client:
        yield client


def login(client: TestClient) -> str:
    resp = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    info = client.get("/api/session").json()
    client.headers["X-CSRF-Token"] = info["csrf_token"]
    return info["csrf_token"]


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    login(client)
    return client


@pytest.fixture
def root(media: Path) -> Path:
    return media / "Movies"


def wait_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")
