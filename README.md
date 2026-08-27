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
agents/                      # agent registry + the Repository Operations Agent
operations/                  # analysis engine: policy, executor, analyzers, diagrams, sandbox
api_operations.py            # FastAPI routes for repository operations
tools/file_reader.py         # file-reader tool (list_files, read_file) sandboxed to stage/
tools/repository_operations.py  # structured ADK tools over the approved operations
tools/github_downloader.py   # download+stage a GitHub repo by URL
tools/zip_stager.py          # stage an uploaded .zip
tools/stage_registry.py      # list/lookup staged repos
stage/                       # staged repos live here; the agent reads from here
static/analysis.js           # browser panel for the operations API (Analysis tab)
generated-docs/              # Mermaid diagrams + Markdown reports (git-ignored)
requirements-analyzers.txt   # optional tree-sitter grammars (higher-confidence results)
tests/fixtures/              # small per-language repos used by the test suite
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


## Repository Operations Agent

The **Repository Operations Agent** (agent 9 of the planned nine) analyzes a
staged repository and returns *structured evidence*: what files exist, which
classes and interfaces are declared, what inherits from what, which types
implement which interfaces — plus Mermaid diagrams of all of it.

It exists so that repository access lives behind exactly one sandbox. Other
analysis agents will ask it for facts instead of reading files themselves, and
it deliberately returns evidence rather than architectural conclusions: every
finding carries a file path, a line number, a detection method and a confidence
level. Interpreting that is a different agent's job.

Full design: [docs/ARCHITECTURE.md §8](docs/ARCHITECTURE.md).

### What it can do

| Group | Operations |
|---|---|
| Inventory | `list_repository_files`, `count_files_and_directories`, `detect_languages`, `git_metadata` |
| Declarations | `find_class`, `find_interface`, `find_function`, `find_method`, `find_symbol` |
| Relationships | `find_references`, `find_implementations`, `find_inheritance`, `find_imports`, `find_calls` |
| Reading | `read_file_range` |
| Analysis | `analyze_oop`, `build_relationship_graph` |
| Diagrams | `generate_class_diagram`, `generate_inheritance_diagram`, `generate_dependency_diagram`, `generate_component_diagram`, `generate_sequence_diagram` |

### Security model

The agent has **no shell**. No operation accepts a command string, so there is
nothing for an instruction hidden in a README to aim at. Concretely:

- **Paths** are resolved inside the staged repository only. Absolute paths,
  `..` traversal, NUL bytes and symlinked components are all rejected, and what
  survives is re-checked for containment.
- **Commands** come from a five-entry allowlist (`rg`, `git`, `ast-grep`,
  `ctags`, `tree-sitter`), always as an argument array, never `shell=True`. A
  separate deny-list (`rm`, `mv`, `chmod`, `kill`, `sudo`, `sh`, `curl`, `pip`,
  …) is checked first. `git` is limited to read-only subcommands and may not be
  pointed at another directory.
- **Execution** is non-interactive, time-limited, output-capped, run with `cwd`
  pinned inside the repository and an environment that forwards **no API keys**.
- **Secrets** are redacted from command output, evidence, logs and generated
  documentation.
- **Everything is read-only.** Nothing is installed, nothing is modified, no
  network request is made. Analysis artefacts go to `generated-docs/`, never
  into `stage/`.
- **Repository content is untrusted data.** Instructions found in source,
  comments or READMEs never override policy.

### Supported languages

| Language | Parser | Inheritance | Interface implementation |
|---|---|---|---|
| Python | stdlib `ast` | confirmed | inferred from an abstract/`Protocol` base |
| Go | tree-sitter, else lexical | struct embedding | inferred by method-set matching |
| Java | tree-sitter, else lexical | declared (`extends`) | declared (`implements`) |
| TypeScript | tree-sitter, else lexical | declared (`extends`) | declared (`implements`) |
| JavaScript | tree-sitter, else lexical | declared (`extends`) | n/a (not in the language) |

Only these five are claimed. Other languages are counted in the inventory and
reported as unanalyzed rather than guessed at. Findings are marked `high`
(a real parser, or an explicit declaration), `medium` (lexical parse or
structural inference) or `low` (a text-search candidate) — an inferred
relationship is never presented as a confirmed one.

**Two backends, one contract.** Go, Java, JavaScript and TypeScript are parsed
by tree-sitter when its grammars are installed, and by built-in lexical
analyzers otherwise. The two agree on declarations, relationships and
visibility — the test suite asserts that parity — so installing the optional
dependency never changes *what* is reported. What it changes is how much you
can trust it, and how precise call graphs are:

|  | lexical (always available) | tree-sitter (`make install-analyzers`) |
|---|---|---|
| Declarations | pattern-matched, `medium` | parsed, `high` |
| Calls | name only (`log`) | receiver-typed (`Logger.log`) |
| Odd syntax | may be missed | handled by the grammar |

`make tools-check` prints which backend is active per language.

### Command-line tools

Nothing external is required; every optional tool has a documented fallback,
and the app starts regardless.

```bash
make tools-check
```

| Tool | Level | If missing |
|---|---|---|
| `rg` (ripgrep) | recommended | pure-Python walk and scan (slower, same results) |
| `git` | recommended | **no fallback** — `git_metadata` returns a structured error |
| `tree-sitter` (Python bindings) | recommended | built-in lexical analyzers, `medium` confidence |
| `ast-grep` | optional | built-in lexical analyzers |
| `ctags` | optional | built-in analyzers |

```bash
dnf install ripgrep git     # or: apt install ripgrep git / brew install ripgrep git
make install-analyzers      # tree-sitter grammars for Go/Java/JS/TS
```

The whole test suite passes with none of them installed — that configuration is
tested, not just claimed.

### Try it from the command line

```bash
make tools-check                          # what's installed, and what it buys you
make analyze-sample                       # analyze the bundled fixture repos
make analyze REPO=path/to/repo            # or a staged repo name
make generate-diagrams REPO=path/to/repo  # writes generated-docs/<name>/
make operations                           # list permitted operations
make install-analyzers                    # optional tree-sitter grammars
```

### In the browser

`make run`, open `http://localhost:8000/`, stage a repo, then switch to the
**Analysis** tab: inventory, OOP structure and Mermaid diagrams, rendered in
place. Every finding is shown with its file, line, detection method and
confidence badge, and anything a size limit dropped is stated explicitly.

Mermaid is loaded from a CDN and rendered progressively; offline, the panel
shows the diagram source instead — the `.mmd` files are written to
`generated-docs/` either way, so a missing renderer costs presentation, never
results.

### API examples

```bash
# Which operations and tools are available
curl localhost:8000/api/operations
curl localhost:8000/api/operations/tools

# Inventory: counts, languages, git metadata
curl localhost:8000/api/repos/myrepo/inventory

# One structured operation (the model picks the enum, never a command)
curl -X POST localhost:8000/api/repos/myrepo/operations \
     -H 'content-type: application/json' \
     -d '{"operation":"find_class","symbol":"UserService","arguments":{"exact":true}}'

# Who implements an interface, with evidence
curl -X POST localhost:8000/api/repos/myrepo/operations \
     -H 'content-type: application/json' \
     -d '{"operation":"find_implementations","symbol":"Store"}'

# Full OOP analysis and the relationship graph
curl localhost:8000/api/repos/myrepo/oop
curl 'localhost:8000/api/repos/myrepo/relationships?include_calls=false'

# Generate diagrams + the Markdown report bundle
curl -X POST localhost:8000/api/repos/myrepo/diagrams \
     -H 'content-type: application/json' \
     -d '{"kinds":["class","inheritance","dependency"],"write":true,"write_documents":true}'

curl localhost:8000/api/repos/myrepo/diagrams   # list what has been generated
```

A `find_class` result looks like this — note that every match carries its own
provenance:

```json
{
  "status": "success",
  "operation": "find_class",
  "repository": "myrepo",
  "matches": [
    {
      "file_path": "src/store.go",
      "symbol": "MemoryStore",
      "symbol_type": "struct",
      "line": 22,
      "language": "go",
      "visibility": "public",
      "detection_method": "lexical_parse",
      "confidence": "medium"
    }
  ],
  "evidence": [
    {"file_path": "src/store.go", "line": 22, "excerpt": "type MemoryStore struct {"}
  ],
  "warnings": [],
  "duration_ms": 12
}
```

### Running a repository's own tooling (development-validation)

Analysis never executes repository code. Running a project's **tests, linters
or type checkers** does — so it is a separate, opt-in profile with a different
mechanism: a disposable container.

```bash
docker pull python:3.12-slim                       # once; runs never pull
make validate REPO=stage/myrepo TOOL=ruff          # or pytest, mypy, go-vet, tsc, eslint
```

What the container gets:

| Control | Setting |
|---|---|
| Network | `--network none` — none at all |
| Filesystem | `--read-only`; repository mounted `:ro`; only `/tmp` writable (`noexec`) |
| Privileges | all capabilities dropped, `no-new-privileges`, runs as uid 65534 |
| Limits | memory (swap disabled), CPU, PID count, wall-clock timeout, output cap |
| Lifetime | `--rm` — destroyed on exit |
| Images | `--pull never` — a missing image fails fast instead of downloading |

The caller names an approved **tool**, never a command line. Only one narrowly
scoped policy may launch a container at all, and it permits `docker run`,
`docker version|info` and `docker image inspect` — nothing that mutates Docker
state, no privileged flags, and no mount that is not read-only and outside the
sensitive host paths.

If no container runtime is available the operation **fails loudly**. It never
falls back to running the repository's code on the host: that would execute
untrusted code with the server's privileges, which is worse than declining.

### Generated output

```
generated-docs/<reponame>/
├── OOP_ANALYSIS.md                counts, polymorphism, encapsulation, limitations
├── CLASS_CATALOG.md               every declared type and where it lives
├── INTERFACE_IMPLEMENTATIONS.md   implementation evidence + confidence
├── FUNCTION_CALL_GRAPH.md         repository-internal call sites
└── diagrams/
    ├── class-diagram.mmd
    ├── inheritance-diagram.mmd
    ├── package-dependency.mmd
    └── component-diagram.mmd
```

Diagrams are size-capped and, for large repositories, split per package —
with everything omitted reported explicitly, because a silently truncated
diagram reads as a complete one.

### Known limitations

- Without the optional tree-sitter grammars, Go/Java/JS/TS fall back to lexical
  analyzers: dependable for ordinary code, but not full grammars, so unusual
  constructs are missed rather than mis-parsed (and reported at `medium`).
- Name resolution is by name within one repository; a name declared twice
  produces an `ambiguous` edge rather than a guessed one.
- Call receivers resolve one field hop through *declared* types only. An
  untyped JavaScript receiver, a value from a factory, or anything dynamic
  stays a name-only edge — reported as `unresolved`, never guessed.
- Dynamic patterns (mixins, prototype manipulation, reflection-based DI) are
  not detected at all.
- `PUBLISHES`/`CONSUMES` are reserved in the relationship vocabulary but no
  analyzer emits them.
- The development-validation profile needs Docker and an image you have already
  pulled that carries the toolchain; there is no host fallback by design.
- The browser panel needs a CDN for Mermaid rendering (it degrades to showing
  diagram source offline), and has no UI for the validation profile.

### Adding a language

See [docs/ARCHITECTURE.md §8.10](docs/ARCHITECTURE.md): add a
`LanguageAnalyzer` subclass under `operations/languages/`, register it, map its
extensions, and add a fixture repository plus tests (including a negative case).
Nothing else changes.

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
- **Structured operations, not shell** — the Repository Operations Agent picks
  a member of an enum; there is no tool that takes a command string, so a
  prompt injection has nothing to aim at.
- **Evidence over conclusions** — analysis results carry file, line, detection
  method and confidence, and an inferred relationship is never reported as a
  confirmed one.
- **Degrade, never lie** — when an external tool is absent the agent uses a
  documented Python fallback or returns a structured error naming the tool. It
  never silently returns a worse answer.
