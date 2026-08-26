# zdocs-ai

A documentation-assistant agent built on the **Google Agent Development Kit
(ADK)**. It reads files staged in the `stage/` directory and answers questions
about them.

For a deeper look at the architecture and business logic, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Structure

```
agent.py                     # LlmAgent definition (model + system instruction + tools)
runner.py                    # CLI: interactive REPL or one-shot prompt
server.py                    # FastAPI web app: stage a repo (URL/upload), then chat
static/                      # vanilla HTML/CSS/JS frontend for server.py
tools/file_reader.py         # file-reader tool (list_files, read_file) sandboxed to stage/
tools/github_downloader.py   # download+stage a GitHub repo by URL
tools/zip_stager.py          # stage an uploaded .zip
tools/stage_registry.py      # list/lookup staged repos
stage/                       # staged repos live here; the agent reads from here
test_*.py                    # deterministic, offline tests (no LLM calls)
docker-compose.yml           # local dev dependency (MinIO) — see "Local dependencies" below
Makefile                     # `make` targets tying the above together
```

## Setup

Dependencies (currently just MinIO) run in Docker; the app and frontend
always run locally against your own Python. Requires Docker with the
`docker compose` plugin.

```bash
make install      # create .venv, install Python dependencies
make env          # create .env from .env.example (then set ADK_MODEL + your API key)
```

## Run

```bash
make deps-up       # start MinIO in Docker (see "Local dependencies")
make run           # run the web app locally: http://localhost:8000/
# or:
make repl          # run the CLI agent locally, interactive
make dev           # deps-up + run, in one step

make deps-down      # stop and remove the dependency container(s)
```

Without Docker/MinIO involved at all, the CLI and one-shot mode still work
exactly as before:

```bash
python runner.py "Summarize sample.md"
python runner.py
```

## Local dependencies (Docker)

`docker-compose.yml` currently defines one service: **MinIO**, an S3-compatible
object store, exposed at `localhost:9000` (API) and `localhost:9001` (console,
default login `minioadmin` / `minioadmin`, overridable via `MINIO_ROOT_USER` /
`MINIO_ROOT_PASSWORD` in `.env`). Data persists in a named Docker volume
across restarts; `make deps-down` stops the container but keeps the volume.

It is **not yet wired into the app** — `stage/` is still the only storage the
agent reads from. The container exists as the local dev seed for a future
MinIO-backed `tools/stage_registry.py` implementation (see Design notes).

```bash
make deps-up     # start
make deps-ps     # status
make deps-logs   # follow logs
make deps-down   # stop + remove containers (volume persists)
```

## Web app

Stage a repo by pasting a GitHub URL or uploading a `.zip`, then chat with it
in the browser (`make run`, then open `http://localhost:8000/`).

Both staging paths land in `stage/<reponame>/` (extracted files plus the
saved zip) using the same zip-slip-guarded extraction as the CLI's GitHub
download. Re-staging an already-staged repo is idempotent — it reuses the
existing directory instead of erroring. Uploads are capped at 50 MB by
default; override with `ZDOCS_MAX_UPLOAD_MB`.

## Test

```bash
make test
# or: python -m pytest -q
```

## Design notes

- **Sandboxed file access** — the file-reader tool resolves every path inside
  the `stage/` directory and rejects traversal escapes, so the agent can never
  read files outside it.
- **Context safety** — `read_file` truncates oversized files with an explicit
  marker instead of flooding the model context window.
- **Configurable model** — set `ADK_MODEL` to any LiteLLM-compatible string
  (`gemini-2.5-flash`, `openai/gpt-4o`, etc.).
- **No required external infra** — the runner uses in-memory session/artifact
  services, so it runs locally with just an API key. MinIO (via
  `docker-compose.yml`) is an optional local dependency for future
  object-storage work, not something the app currently needs to function.
