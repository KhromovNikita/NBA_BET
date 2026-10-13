.PHONY: sync infra dev backfill migrate-clickhouse build-possessions build-lineups dataset build-baselines test test-unit test-integration test-e2e lint typecheck

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

migrate-clickhouse:
	uv run python -m scripts.migrate_clickhouse

build-possessions:
	@if [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO, not both"; exit 2; \
	elif [ -z "$(SEASON)" ] && { [ -z "$(SEASON_FROM)" ] || [ -z "$(SEASON_TO)" ]; }; then \
		echo "SEASON or both SEASON_FROM and SEASON_TO are required"; exit 2; \
	elif [ -n "$(GAME_ID)" ] && [ -z "$(SEASON)" ]; then \
		echo "GAME_ID requires SEASON"; exit 2; \
	elif [ "$(REBUILD)" = "1" ] && [ "$(VERIFY)" = "1" ]; then \
		echo "choose REBUILD or VERIFY, not both"; exit 2; \
	elif [ -n "$(REBUILD)" ] && [ "$(REBUILD)" != "1" ]; then \
		echo "REBUILD must be 1 when set"; exit 2; \
	elif [ -n "$(VERIFY)" ] && [ "$(VERIFY)" != "1" ]; then \
		echo "VERIFY must be 1 when set"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.build_possessions --season "$(SEASON)" $(if $(GAME_ID),--game-id "$(GAME_ID)",) $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	else \
		uv run python -m scripts.build_possessions --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	fi

build-lineups:
	@if [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO, not both"; exit 2; \
	elif [ -z "$(SEASON)" ] && { [ -z "$(SEASON_FROM)" ] || [ -z "$(SEASON_TO)" ]; }; then \
		echo "SEASON or both SEASON_FROM and SEASON_TO are required"; exit 2; \
	elif [ -n "$(GAME_ID)" ] && [ -z "$(SEASON)" ]; then \
		echo "GAME_ID requires SEASON"; exit 2; \
	elif [ "$(REBUILD)" = "1" ] && [ "$(VERIFY)" = "1" ]; then \
		echo "choose REBUILD or VERIFY, not both"; exit 2; \
	elif [ -n "$(REBUILD)" ] && [ "$(REBUILD)" != "1" ]; then \
		echo "REBUILD must be 1 when set"; exit 2; \
	elif [ -n "$(VERIFY)" ] && [ "$(VERIFY)" != "1" ]; then \
		echo "VERIFY must be 1 when set"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.build_lineups --season "$(SEASON)" $(if $(GAME_ID),--game-id "$(GAME_ID)",) $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	else \
		uv run python -m scripts.build_lineups --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	fi

dataset:
	@if [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO, not both"; exit 2; \
	elif [ -z "$(SEASON)" ] && { [ -z "$(SEASON_FROM)" ] || [ -z "$(SEASON_TO)" ]; }; then \
		echo "SEASON or both SEASON_FROM and SEASON_TO are required"; exit 2; \
	elif [ -n "$(GAME_ID)" ] && [ -z "$(SEASON)" ]; then \
		echo "GAME_ID requires SEASON"; exit 2; \
	elif [ "$(REBUILD)" = "1" ] && [ "$(VERIFY)" = "1" ]; then \
		echo "choose REBUILD or VERIFY, not both"; exit 2; \
	elif [ -n "$(REBUILD)" ] && [ "$(REBUILD)" != "1" ]; then \
		echo "REBUILD must be 1 when set"; exit 2; \
	elif [ -n "$(VERIFY)" ] && [ "$(VERIFY)" != "1" ]; then \
		echo "VERIFY must be 1 when set"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.build_dataset --season "$(SEASON)" $(if $(GAME_ID),--game-id "$(GAME_ID)",) $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	else \
		uv run python -m scripts.build_dataset --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" $(if $(filter 1,$(REBUILD)),--rebuild,) $(if $(filter 1,$(VERIFY)),--verify,); \
	fi

build-baselines:
	@if [ -z "$(OUTPUT)" ]; then \
		echo "OUTPUT is required"; exit 2; \
	elif [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO, not both"; exit 2; \
	elif [ -n "$(GAME_ID)" ] && [ -z "$(SEASON)" ]; then \
		echo "GAME_ID requires SEASON"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.build_baselines --season "$(SEASON)" $(if $(GAME_ID),--game-id "$(GAME_ID)",) --output "$(OUTPUT)" $(if $(HISTORY_WINDOW),--history-window "$(HISTORY_WINDOW)",) $(if $(PPP_PRIOR_POSSESSIONS),--ppp-prior-possessions "$(PPP_PRIOR_POSSESSIONS)",) $(if $(PACE_PRIOR_COMBINED_POSSESSIONS),--pace-prior-combined-possessions "$(PACE_PRIOR_COMBINED_POSSESSIONS)",); \
	elif [ -n "$(SEASON_FROM)" ] && [ -n "$(SEASON_TO)" ]; then \
		uv run python -m scripts.build_baselines --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" --output "$(OUTPUT)" $(if $(HISTORY_WINDOW),--history-window "$(HISTORY_WINDOW)",) $(if $(PPP_PRIOR_POSSESSIONS),--ppp-prior-possessions "$(PPP_PRIOR_POSSESSIONS)",) $(if $(PACE_PRIOR_COMBINED_POSSESSIONS),--pace-prior-combined-possessions "$(PACE_PRIOR_COMBINED_POSSESSIONS)",); \
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
