.PHONY: help env venv install deps-up deps-down deps-logs deps-ps run repl test clean stop

VENV       := .venv
PYTHON     := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
COMPOSE    := docker compose

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

dev: deps-up run

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
