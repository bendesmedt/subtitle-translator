import json
import stat

import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import EnvSettings
from tests.conftest import LT_URL, ORIGIN, PASSWORD, USERNAME, login, make_client, write_config


def _setup_body(token: str, **overrides) -> dict:
    body = {
        "token": token,
        "username": "owner",
        "password": "a-very-long-password",
        "password_confirm": "a-very-long-password",
        "libretranslate_url": LT_URL,
        "libretranslate_api_key": "",
        "media_root": "Movies",
        "default_source": "en",
        "default_target": "nl",
    }
    body.update(overrides)
    return body


# --- Setup mode ---------------------------------------------------------------------


def test_setup_mode_redirects_everything(setup_client):
    resp = setup_client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    assert setup_client.get("/login", follow_redirects=False).headers["location"] == "/setup"
    assert setup_client.get("/api/files").status_code == 409
    assert setup_client.get("/setup").status_code == 200
    assert setup_client.get("/healthz").status_code == 200


def test_setup_token_logged(env: EnvSettings, caplog):
    caplog.set_level("WARNING")
    with make_client(env) as client:
        token = client.app.state.setup_token
    assert token and len(token) >= 40
    assert any(token in r.getMessage() for r in caplog.records)


def test_setup_requires_token(setup_client):
    assert setup_client.post("/api/setup/check", json={"token": "wrong"}).status_code == 403
    assert setup_client.post("/api/setup", json=_setup_body("wrong")).status_code == 403
    assert setup_client.post("/api/setup/folders", json={"token": "nope", "path": ""}).status_code == 403


def test_setup_token_rate_limited(setup_client):
    codes = [setup_client.post("/api/setup/check", json={"token": "x"}).status_code for _ in range(5)]
    assert 429 in codes
    token = setup_client.app.state.setup_token
    # Even the right token is refused while blocked.
    assert setup_client.post("/api/setup/check", json={"token": token}).status_code == 429


def test_setup_flow_and_disabled_afterwards(setup_client, env: EnvSettings):
    token = setup_client.app.state.setup_token
    assert setup_client.post("/api/setup/check", json={"token": token}).json()["ok"]
    folders = setup_client.post("/api/setup/folders", json={"token": token, "path": ""}).json()
    assert folders["folders"] == ["Movies"]
    escape = setup_client.post("/api/setup/folders", json={"token": token, "path": ".."})
    assert escape.status_code == 400

    short = setup_client.post("/api/setup", json=_setup_body(token, password="short", password_confirm="short"))
    assert short.status_code == 400
    bad_root = setup_client.post("/api/setup", json=_setup_body(token, media_root="../x"))
    assert bad_root.status_code == 400
    bad_url = setup_client.post("/api/setup", json=_setup_body(token, libretranslate_url="ftp://x"))
    assert bad_url.status_code == 400

    resp = setup_client.post("/api/setup", json=_setup_body(token))
    assert resp.status_code == 200, resp.text

    cfg_file = env.config_dir / "config.json"
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    saved = json.loads(cfg_file.read_text())
    assert saved["password_hash"].startswith("$argon2id$")
    assert "a-very-long-password" not in cfg_file.read_text()

    assert setup_client.get("/setup").status_code == 404
    assert setup_client.post("/api/setup", json=_setup_body(token)).status_code == 404
    assert setup_client.post("/api/setup/check", json={"token": token}).status_code == 404
    login_resp = setup_client.post("/api/login", json={"username": "owner", "password": "a-very-long-password"})
    assert login_resp.status_code == 200


def test_setup_connection_test(setup_client):
    token = setup_client.app.state.setup_token
    with respx.mock:
        respx.get(f"{LT_URL}/languages").mock(
            return_value=Response(200, json=[{"code": "en", "name": "English", "targets": ["fr"]}])
        )
        data = setup_client.post("/api/setup/test", json={"token": token, "url": LT_URL}).json()
    assert data["ok"] and data["languages"][0]["code"] == "en"


# --- Login / sessions ---------------------------------------------------------------


def test_index_requires_login(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/files").status_code == 401
    assert client.get("/setup").status_code == 404


def test_login_logout(client):
    bad = client.post("/api/login", json={"username": USERNAME, "password": "wrong-password"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "Invalid username or password"
    unknown = client.post("/api/login", json={"username": "nobody", "password": PASSWORD})
    assert unknown.json()["detail"] == bad.json()["detail"]

    resp = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/", follow_redirects=False).status_code == 200

    csrf = client.get("/api/session").json()["csrf_token"]
    assert client.post("/api/logout", json={}).status_code == 403  # missing CSRF
    assert client.post("/api/logout", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/session").status_code == 401


def test_secure_cookie_flag(env: EnvSettings):
    write_config(env)
    secure_env = EnvSettings(config_dir=env.config_dir, media_base=env.media_base, cookie_secure=True)
    with make_client(secure_env) as client:
        resp = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        assert "secure" in resp.headers["set-cookie"].lower()


def test_login_rate_limit(client):
    codes = [
        client.post("/api/login", json={"username": USERNAME, "password": "nope-nope"}).status_code for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes
    blocked = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert blocked.status_code == 429 and "retry-after" in blocked.headers


def test_session_idle_expiry(client):
    login(client)
    client.app.state.sessions.idle_seconds = 0
    assert client.get("/api/session").status_code == 401


def test_csrf_and_origin_checks(client):
    login(client)
    body = {"files": ["a.en.srt"], "source": "en", "target": "fr"}
    ok_headers = dict(client.headers)
    no_csrf = client.post("/api/jobs", json=body, headers={"X-CSRF-Token": "bad"})
    assert no_csrf.status_code == 403

    client.headers.pop("Origin")
    assert client.post("/api/jobs", json=body).status_code == 403
    evil = client.post("/api/jobs", json=body, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    ref = client.post("/api/jobs", json=body, headers={"Referer": f"{ORIGIN}/"})
    assert ref.status_code == 200
    client.headers.update(ok_headers)

    form = client.post("/api/jobs", content="files=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 415


def test_security_headers_and_no_docs(client):
    resp = client.get("/login")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code in (401, 404)


def test_body_size_limit(client):
    login(client)
    big = {"files": ["x" * 1000] * 3000, "source": "en", "target": "fr"}
    assert client.post("/api/jobs", json=big).status_code == 413


def test_change_password(client):
    login(client)
    other = TestClient(client.app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    login(other)
    bad = client.post(
        "/api/settings/password",
        json={"current_password": "wrong", "new_password": "x" * 12, "new_password_confirm": "x" * 12},
    )
    assert bad.status_code == 403
    ok = client.post(
        "/api/settings/password",
        json={"current_password": PASSWORD, "new_password": "new-password-123", "new_password_confirm": "new-password-123"},
    )
    assert ok.status_code == 200
    # The other session was invalidated, the current one survives.
    assert other.get("/api/session").status_code == 401
    assert client.get("/api/session").status_code == 200
    client.post("/api/logout", json={})
    assert client.post("/api/login", json={"username": USERNAME, "password": "new-password-123"}).status_code == 200
