"""FastAPI web app for zdocs-ai: stage a GitHub repo (URL or upload), then chat.

Run with:
    uvicorn server:app --reload --port 8000

Uses the same in-memory session/artifact services and ``build_agent`` as the
CLI runner — no external infrastructure required.
"""

from __future__ import annotations

import os
import threading
import urllib.error
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from pydantic import BaseModel

from agent import build_agent
from api_operations import create_operations_router
from runner import run_turn
from tools.github_downloader import download_repo, parse_repo
from tools.stage_registry import is_staged, list_staged_repos, staged_repo_dir
from tools.zip_stager import repo_name_from_filename, stage_uploaded_zip

load_dotenv()

APP_NAME = "zdocs-ai"
USER_ID = "web-user"

STAGE_DIR = Path(__file__).resolve().parent / "stage"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Analysis artefacts (Mermaid diagrams, generated Markdown) are written here,
# namespaced per repository. Never inside stage/ — analysis is read-only with
# respect to the code it analyzes.
GENERATED_DOCS_DIR = Path(
    os.getenv("ZDOCS_GENERATED_DOCS_DIR")
    or Path(__file__).resolve().parent / "generated-docs"
)
MAX_UPLOAD_BYTES = int(os.getenv("ZDOCS_MAX_UPLOAD_MB", "50")) * 1024 * 1024

app = FastAPI(title="zdocs-ai")

# Repository Operations Agent routes. The directories are passed as callables
# so the router always reads the current module-level values (tests monkeypatch
# STAGE_DIR).
app.include_router(
    create_operations_router(lambda: STAGE_DIR, lambda: GENERATED_DOCS_DIR)
)

_runners: dict[str, Runner] = {}
_runners_lock = threading.Lock()


class StageUrlRequest(BaseModel):
    url: str
    branch: str | None = None


class StageResponse(BaseModel):
    repo: str
    status: Literal["staged", "already_staged"]


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


def _get_or_build_runner(reponame: str) -> Runner:
    repo_dir = staged_repo_dir(reponame, stage_dir=STAGE_DIR)
    with _runners_lock:
        runner = _runners.get(reponame)
        if runner is None:
            runner = Runner(
                app_name=APP_NAME,
                agent=build_agent(stage_dir=repo_dir),
                session_service=InMemorySessionService(),
                artifact_service=InMemoryArtifactService(),
                auto_create_session=True,
            )
            _runners[reponame] = runner
        return runner


def _run_agent_turn(reponame: str, session_id: str, message: str) -> str:
    runner = _get_or_build_runner(reponame)
    return run_turn(runner, user_id=USER_ID, session_id=session_id, prompt=message)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/repos")
def list_repos() -> dict:
    return {"repos": list_staged_repos(stage_dir=STAGE_DIR)}


@app.post("/api/repos/from-url", response_model=StageResponse)
def stage_from_url(body: StageUrlRequest) -> StageResponse:
    try:
        _, reponame = parse_repo(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_staged(reponame, stage_dir=STAGE_DIR):
        return StageResponse(repo=reponame, status="already_staged")

    try:
        repo_dir = download_repo(body.url, stage_dir=STAGE_DIR, branch=body.branch)
    except FileExistsError:
        return StageResponse(repo=reponame, status="already_staged")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            detail = f"Repository or branch not found on GitHub: {body.url}"
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(
            status_code=502, detail=f"GitHub returned an error ({exc.code})"
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to reach GitHub: {exc.reason}"
        ) from exc

    return StageResponse(repo=repo_dir.name, status="staged")


@app.post("/api/repos/upload", response_model=StageResponse)
async def upload_repo(file: UploadFile) -> StageResponse:
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    try:
        reponame = repo_name_from_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_staged(reponame, stage_dir=STAGE_DIR):
        await file.close()
        return StageResponse(repo=reponame, status="already_staged")

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            await file.close()
            raise HTTPException(status_code=413, detail="Uploaded file is too large")
        chunks.append(chunk)
    await file.close()
    data = b"".join(chunks)

    try:
        repo_dir = stage_uploaded_zip(data, file.filename, stage_dir=STAGE_DIR)
    except FileExistsError:
        return StageResponse(repo=reponame, status="already_staged")
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StageResponse(repo=repo_dir.name, status="staged")


@app.post("/api/repos/{reponame}/chat", response_model=ChatResponse)
def chat(reponame: str, body: ChatRequest) -> ChatResponse:
    if not is_staged(reponame, stage_dir=STAGE_DIR):
        raise HTTPException(status_code=404, detail=f"Repo not staged: {reponame!r}")

    session_id = body.session_id or str(uuid.uuid4())
    reply = _run_agent_turn(reponame, session_id, body.message)
    return ChatResponse(reply=reply, session_id=session_id)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
