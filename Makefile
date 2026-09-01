.PHONY: sync infra dev backfill test test-unit test-integration test-e2e lint typecheck

sync:
	uv sync --frozen

infra:
	docker compose up -d --wait postgres clickhouse redis

dev: sync
	docker compose up -d --build --wait

backfill:
	@if [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO, not both"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.backfill_games --season "$(SEASON)" $(if $(filter 1,$(REFRESH)),--refresh,); \
	elif [ -n "$(SEASON_FROM)" ] && [ -n "$(SEASON_TO)" ]; then \
		uv run python -m scripts.backfill_games --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" $(if $(filter 1,$(REFRESH)),--refresh,); \
	else \
		echo "SEASON or both SEASON_FROM and SEASON_TO are required"; exit 2; \
	fi

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
	uv run mypy src scripts tests
