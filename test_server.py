"""Offline tests for server.py's FastAPI routes.

No network calls and no LLM calls: the GitHub fetch is monkeypatched at
``tools.github_downloader._fetch`` (the established seam) and the agent turn
is monkeypatched at ``server._run_agent_turn``.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import tools.github_downloader as gd
from conftest import make_zip


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(server, "STAGE_DIR", stage)
    server._runners.clear()
    return TestClient(server.app)


def test_list_repos_empty(client: TestClient):
    res = client.get("/api/repos")
    assert res.status_code == 200
    assert res.json() == {"repos": []}


def test_stage_from_url_happy_path_and_repeat(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gd, "_fetch", lambda url: make_zip({"hello-main/README.md": b"# hi\n"}))

    res = client.post("/api/repos/from-url", json={"url": "https://github.com/octo/hello"})
    assert res.status_code == 200
    assert res.json() == {"repo": "hello", "status": "staged"}

    res2 = client.post("/api/repos/from-url", json={"url": "https://github.com/octo/hello"})
    assert res2.status_code == 200
    assert res2.json() == {"repo": "hello", "status": "already_staged"}

    assert client.get("/api/repos").json() == {"repos": ["hello"]}


def test_stage_from_url_bad_url_rejected(client: TestClient):
    res = client.post("/api/repos/from-url", json={"url": "not a url"})
    assert res.status_code == 400


def test_stage_from_url_github_404_maps_to_clean_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def _boom(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(gd, "_fetch", _boom)

    res = client.post("/api/repos/from-url", json={"url": "https://github.com/octo/missing"})
    assert res.status_code == 404
    assert not (server.STAGE_DIR / "missing").exists()


def test_stage_from_url_github_network_error_maps_to_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def _boom(url):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gd, "_fetch", _boom)

    res = client.post("/api/repos/from-url", json={"url": "https://github.com/octo/hello"})
    assert res.status_code == 502


def test_upload_happy_path(client: TestClient):
    data = make_zip({"README.md": b"# hi\n"})
    res = client.post(
        "/api/repos/upload",
        files={"file": ("hello.zip", data, "application/zip")},
    )
    assert res.status_code == 200
    assert res.json() == {"repo": "hello", "status": "staged"}
    assert (server.STAGE_DIR / "hello" / "README.md").read_bytes() == b"# hi\n"


def test_upload_rejects_non_zip(client: TestClient):
    res = client.post(
        "/api/repos/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400
    assert list(server.STAGE_DIR.iterdir()) == []


def test_upload_rejects_oversized(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    data = make_zip({"README.md": b"x" * 1000})
    res = client.post(
        "/api/repos/upload",
        files={"file": ("hello.zip", data, "application/zip")},
    )
    assert res.status_code == 413
    assert list(server.STAGE_DIR.iterdir()) == []


def test_chat_unknown_repo_404(client: TestClient):
    res = client.post("/api/repos/nope/chat", json={"message": "hi"})
    assert res.status_code == 404


def test_chat_happy_path_and_session_id(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    (server.STAGE_DIR / "hello").mkdir()
    monkeypatch.setattr(server, "_run_agent_turn", lambda repo, session_id, message: "canned reply")

    res = client.post("/api/repos/hello/chat", json={"message": "hi"})
    assert res.status_code == 200
    body = res.json()
    assert body["reply"] == "canned reply"
    assert body["session_id"]

    res2 = client.post(
        "/api/repos/hello/chat", json={"message": "again", "session_id": body["session_id"]}
    )
    assert res2.json()["session_id"] == body["session_id"]
