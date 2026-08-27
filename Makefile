.PHONY: help env venv install deps-up deps-down deps-logs deps-ps run repl test clean stop \
        tools-check operations analyze analyze-sample generate-diagrams \
        install-analyzers validate

VENV       := .venv
PYTHON     := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
COMPOSE    := docker compose

# Repository to analyze: a directory path, or the name of a repo staged under
# stage/. Defaults to the bundled multi-language test fixtures, so
# `make analyze-sample` works on a fresh clone with nothing staged.
REPO       ?= tests/fixtures
DOCS_DIR   ?= generated-docs

help:
	@echo "zdocs-ai — infra runs in Docker, app + frontend run locally"
	@echo ""
	@echo "  make deps-up     Start local dependencies (MinIO) in Docker"
	@echo "  make deps-down   Stop and remove the dependency containers"
	@echo "  make deps-logs   Follow logs from the dependency containers"
	@echo "  make deps-ps     Show dependency container status"
	@echo ""
	@echo "  make install     Create .venv and install Python dependencies"
	@echo "  make run         Run the web app locally (uvicorn server:app --reload)"
	@echo "  make repl        Run the CLI agent locally (interactive REPL)"
	@echo "  make test        Run the offline test suite locally"
	@echo ""
	@echo "  Repository Operations Agent:"
	@echo "  make tools-check       Report which analysis tools (rg/git/tree-sitter/ast-grep/ctags) are installed"
	@echo "  make operations        List the operations the read-only profile permits"
	@echo "  make analyze-sample    Run OOP analysis on the bundled fixture repositories"
	@echo "  make analyze           Run OOP analysis on REPO=<path-or-staged-name>"
	@echo "  make generate-diagrams Write Mermaid diagrams + Markdown reports for REPO"
	@echo "  make install-analyzers Install optional tree-sitter grammars (higher-confidence results)"
	@echo "  make validate          Run TOOL=<pytest|ruff|mypy|...> on REPO in an isolated container"
	@echo ""
	@echo "  make dev         deps-up + run (the usual local dev loop)"
	@echo "  make stop        Alias for deps-down"
	@echo "  make clean       Remove .venv and Python cache directories"

env:
	@[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example — fill in your API key(s)."; }

$(VENV)/bin/activate:
	python3 -m venv $(VENV)

venv: $(VENV)/bin/activate

install: venv
	$(PIP) install -q -r requirements.txt

# Optional: real syntax-tree parsing for Go/Java/JavaScript/TypeScript. Without
# these the built-in lexical analyzers are used instead — same findings, lower
# confidence. Nothing else changes.
install-analyzers: venv
	$(PIP) install -q -r requirements-analyzers.txt
	@$(PYTHON) -m operations.cli tools

deps-up: env
	$(COMPOSE) up -d
	@echo "MinIO API:     http://localhost:$${MINIO_PORT:-9000}"
	@echo "MinIO Console: http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

deps-down:
	$(COMPOSE) down

deps-logs:
	$(COMPOSE) logs -f

deps-ps:
	$(COMPOSE) ps

stop: deps-down

run: install env
	$(PYTHON) -m uvicorn server:app --reload --port 8000

repl: install env
	$(PYTHON) runner.py

test: install
	$(PYTHON) -m pytest -q

# --- Repository Operations Agent -------------------------------------------
# None of these need network access, an API key, Docker or MinIO.

tools-check: install
	$(PYTHON) -m operations.cli tools

operations: install
	$(PYTHON) -m operations.cli operations

analyze: install
	$(PYTHON) -m operations.cli analyze $(REPO)

analyze-sample: install
	@echo "Analyzing the bundled fixture repositories (tests/fixtures)..."
	$(PYTHON) -m operations.cli analyze tests/fixtures

generate-diagrams: install
	$(PYTHON) -m operations.cli diagrams $(REPO) --out $(DOCS_DIR) --docs

# Development-validation profile: runs a project's own tooling inside a
# disposable container (no network, read-only filesystem, dropped capabilities,
# CPU/memory/PID/time limits). Requires Docker; disabled unless invoked here.
# Example: make validate REPO=stage/myrepo TOOL=ruff
TOOL ?= ruff
validate: install
	$(PYTHON) -m operations.cli validate $(REPO) --tool $(TOOL)

dev: deps-up run

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
