import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from app.jobs import write_output
from app.routes import files as files_routes
from tests.conftest import LT_URL, wait_job

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n\n2\n00:00:03,000 --> 00:00:04,000\n<i>Good bye</i>\n"


def _echo(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"translatedText": [f"FR {q}" for q in body["q"]]})


@pytest.fixture
def lt():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{LT_URL}/translate").mock(side_effect=_echo)
        mock.get(f"{LT_URL}/languages").mock(
            return_value=httpx.Response(200, json=[{"code": "en", "name": "English", "targets": ["fr"]}])
        )
        mock.post(f"{LT_URL}/detect").mock(
            return_value=httpx.Response(200, json=[{"language": "en", "confidence": 95}])
        )
        yield mock


def _job(client, files, **kw):
    body = {"files": files, "source": "en", "target": "fr", "overwrite": False}
    body.update(kw)
    resp = client.post("/api/jobs", json=body)
    assert resp.status_code == 200, resp.text
    return wait_job(client, resp.json()["id"])


# --- Jobs ---------------------------------------------------------------------------


def test_translate_file(authed, root: Path, lt):
    (root / "Film.en.srt").write_text(SRT)
    job = _job(authed, ["Film.en.srt"])
    assert job["status"] == "done"
    assert job["files"][0]["status"] == "done", job
    assert job["files"][0]["output"] == "Film.fr.srt"
    out = (root / "Film.fr.srt").read_text()
    assert "00:00:01,000 --> 00:00:02,000" in out
    assert "FR Hello world" in out and "<i>FR Good bye</i>" in out
    assert not [p for p in root.iterdir() if p.name.startswith(".st-")]


def test_skip_existing_then_overwrite(authed, root: Path, lt):
    (root / "Film.en.srt").write_text(SRT)
    (root / "Film.fr.srt").write_text("keep me")
    job = _job(authed, ["Film.en.srt"])
    assert job["files"][0]["status"] == "skipped"
    assert (root / "Film.fr.srt").read_text() == "keep me"

    job = _job(authed, ["Film.en.srt"], overwrite=True)
    assert job["files"][0]["status"] == "done"
    assert "FR Hello world" in (root / "Film.fr.srt").read_text()


def test_error_isolation(authed, root: Path, lt, tmp_path: Path):
    (root / "good.en.srt").write_text(SRT)
    (root / "bad.en.ass").write_text("not an ass file at all")
    (root / "big.en.srt").write_text(SRT * 20)
    authed.app.state.settings.max_file_mb = 0.0001
    job = _job(authed, ["../outside.srt", "bad.en.ass", "big.en.srt", "missing.en.srt"])
    statuses = [f["status"] for f in job["files"]]
    assert statuses == ["error", "error", "error", "error"]
    assert all(str(tmp_path) not in f["message"] for f in job["files"])
    authed.app.state.settings.max_file_mb = 5
    job = _job(authed, ["bad.en.ass", "good.en.srt"])
    assert [f["status"] for f in job["files"]] == ["error", "done"]


def test_translator_failure_reported(authed, root: Path):
    (root / "Film.en.srt").write_text(SRT)
    with respx.mock:
        respx.post(f"{LT_URL}/translate").mock(return_value=httpx.Response(400, json={"error": "bad"}))
        job = _job(authed, ["Film.en.srt"])
    assert job["files"][0]["status"] == "error"
    assert "LibreTranslate" in job["files"][0]["message"]
    assert not (root / "Film.fr.srt").exists()


def test_cancel_queued_job(authed, root: Path, lt):
    (root / "a.en.srt").write_text(SRT)
    jobs = authed.app.state.jobs
    job = jobs.submit(["a.en.srt"], "en", "fr", False)
    jobs.cancel(job.id)
    assert job.status == "cancelled"
    assert job.files[0].status == "cancelled"
    assert authed.post(f"/api/jobs/{job.id}/cancel", json={}).status_code == 200


def test_auto_source_and_same_language(authed, root: Path, lt):
    (root / "a.fr.srt").write_text(SRT)
    (root / "b.en.srt").write_text(SRT)
    job = _job(authed, ["a.fr.srt", "b.en.srt"], source="auto")
    assert [f["status"] for f in job["files"]] == ["skipped", "done"]
    assert authed.post("/api/jobs", json={"files": ["x"], "source": "fr", "target": "fr"}).status_code == 400


def test_job_limits(authed):
    authed.app.state.settings.max_files_per_job = 2
    resp = authed.post("/api/jobs", json={"files": ["a", "b", "c"], "source": "en", "target": "fr"})
    assert resp.status_code == 400


def test_write_output_never_clobbers(tmp_path: Path):
    target = tmp_path / "out.srt"
    write_output(target, b"one", overwrite=False)
    with pytest.raises(FileExistsError):
        write_output(target, b"two", overwrite=False)
    assert target.read_bytes() == b"one"
    write_output(target, b"three", overwrite=True)
    assert target.read_bytes() == b"three"
    assert [p.name for p in tmp_path.iterdir()] == ["out.srt"]


# --- Browse / scan / search ---------------------------------------------------------


@pytest.fixture
def library(root: Path, tmp_path: Path) -> Path:
    (root / "Show" / "S01").mkdir(parents=True)
    (root / "Show" / "S01" / "e1.en.srt").write_text(SRT)
    (root / "Show" / "S01" / "e2.en.srt").write_text(SRT)
    (root / "Show" / "S01" / "e2.fr.srt").write_text(SRT)
    (root / "Show" / "S01" / "e3.srt").write_text(SRT)
    (root / "Show" / "S01" / "e4.de.vtt").write_text("WEBVTT\n")
    (root / "Show" / "S01" / "video.mkv").write_text("")
    (root / "Show" / "S01" / "vob.sub").write_bytes(b"\x00\x00")
    (root / "Show" / "S01" / "vob.idx").write_text("")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.en.srt").write_text(SRT)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.en.srt").write_text(SRT)
    os.symlink(outside, root / "escape")
    os.symlink(outside / "leak.en.srt", root / "leak.en.srt")
    return root


def test_browse(authed, library):
    data = authed.get("/api/files", params={"path": "", "target": "fr"}).json()
    names = [e["name"] for e in data["entries"]]
    assert names == ["Show"]
    data = authed.get("/api/files", params={"path": "Show/S01", "target": "fr"}).json()
    by_name = {e["name"]: e for e in data["entries"]}
    assert set(by_name) == {"e1.en.srt", "e2.en.srt", "e2.fr.srt", "e3.srt", "e4.de.vtt"}
    assert by_name["e2.en.srt"]["translated"] is True
    assert by_name["e1.en.srt"]["translated"] is False
    assert by_name["e2.fr.srt"]["is_target"] is True
    assert [c["path"] for c in data["breadcrumbs"]] == ["", "Show", "Show/S01"]
    assert authed.get("/api/files", params={"path": "../"}).status_code == 400
    assert authed.get("/api/files", params={"path": "escape"}).status_code == 400


def test_scan_modes(authed, library, lt):
    data = authed.post("/api/scan", json={"source": "en", "target": "fr"}).json()
    assert [r["path"] for r in data["results"]] == ["Show/S01/e1.en.srt"]
    data = authed.post("/api/scan", json={"source": "en", "target": "fr", "include_existing": True}).json()
    assert [r["path"] for r in data["results"]] == ["Show/S01/e1.en.srt", "Show/S01/e2.en.srt"]
    data = authed.post("/api/scan", json={"source": "en", "target": "fr", "mode": "none"}).json()
    assert [r["path"] for r in data["results"]] == ["Show/S01/e3.srt"]
    data = authed.post("/api/scan", json={"source": "en", "target": "fr", "mode": "detect"}).json()
    assert [r["path"] for r in data["results"]] == ["Show/S01/e3.srt"]
    assert data["results"][0]["detected"] == "en"
    assert authed.post("/api/scan", json={"source": "en", "target": "en"}).status_code == 400
    assert authed.post("/api/scan", json={"source": "en", "target": "fr", "path": "../.."}).status_code == 400


def test_search(authed, library, monkeypatch):
    data = authed.get("/api/search", params={"q": "S01/E"}).json()
    assert len(data["results"]) == 5 and not data["truncated"]
    assert all("hidden" not in r["path"] and "leak" not in r["path"] for r in data["results"])
    monkeypatch.setattr(files_routes, "MAX_SEARCH_RESULTS", 2)
    data = authed.get("/api/search", params={"q": "show"}).json()
    assert len(data["results"]) == 2 and data["truncated"]
    assert authed.get("/api/search", params={"q": ""}).status_code == 422


def test_settings_roundtrip(authed, lt):
    s = authed.get("/api/settings").json()
    assert "password_hash" not in s and "libretranslate_api_key" not in s
    s.update({"libretranslate_api_key": "secret-key", "default_target": "de"})
    for k in ("username", "api_key_set", "insecure_translator_url"):
        s.pop(k)
    resp = authed.put("/api/settings", json=s)
    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key_set"] is True and "secret-key" not in resp.text
    s.pop("libretranslate_api_key")
    s["media_root"] = "../"
    assert authed.put("/api/settings", json=s).status_code == 400
    assert authed.get("/api/languages").json()["languages"][0]["code"] == "en"
