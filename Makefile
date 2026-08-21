.PHONY: dev test lint security-test e2e format migrate seed run-gateway run-execution run-agent run-channels docker-up docker-down

VENV := .venv
PY := $(VENV)/Scripts/python.exe

dev:
	python -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	cp -n .env.example .env || true

test:
	$(PY) -m pytest tests/unit tests/integration -q

security-test:
	$(PY) -m pytest tests/security -q

e2e:
	$(PY) -m pytest tests/e2e -q

test-all:
	$(PY) -m pytest tests -q

lint:
	$(PY) -m ruff check src tests
	$(PY) -m mypy src

format:
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

migrate:
	$(PY) -m alembic upgrade head

run-gateway:
	$(PY) -m uvicorn inumi.gateway.api.app:app --reload --port 8001

run-execution:
	$(PY) -m uvicorn inumi.execution.api.app:app --reload --port 8002

run-agent:
	$(PY) -m uvicorn inumi.agent.api.app:app --reload --port 8000

run-channels:
	$(PY) -m uvicorn inumi.channels.api.app:app --reload --port 8003

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v
