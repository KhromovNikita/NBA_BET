.PHONY: sync infra dev test test-unit test-integration test-e2e lint typecheck

sync:
	uv sync --frozen

infra:
	docker compose up -d --wait postgres clickhouse redis

dev: sync
	docker compose up -d --build --wait

test-unit: sync
	uv run pytest tests/unit tests/api -v

test-integration: sync infra
	uv run pytest tests/integration -v -m integration

test-e2e: sync
	docker compose up -d --build --wait
	uv run pytest tests/e2e -v -m e2e

test: test-unit test-integration test-e2e

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src tests
