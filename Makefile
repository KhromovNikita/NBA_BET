.PHONY: sync infra dev backfill migrate-clickhouse build-possessions build-lineups dataset build-baselines training-dataset train-models predict-models test test-unit test-integration test-e2e lint typecheck

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

training-dataset:
	@if [ -z "$(OUTPUT)" ] || [ -z "$(VALIDATION_FROM)" ] || [ -z "$(TEST_FROM)" ]; then \
		echo "OUTPUT, VALIDATION_FROM and TEST_FROM are required"; exit 2; \
	elif [ -n "$(GAME_ID)" ]; then \
		echo "training-dataset does not accept GAME_ID"; exit 2; \
	elif [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run python -m scripts.build_training_dataset --season "$(SEASON)" --validation-from "$(VALIDATION_FROM)" --test-from "$(TEST_FROM)" --output "$(OUTPUT)" $(if $(EMBARGO_DAYS),--embargo-days "$(EMBARGO_DAYS)",) $(if $(HISTORY_WINDOW),--history-window "$(HISTORY_WINDOW)",) $(if $(PPP_PRIOR_POSSESSIONS),--ppp-prior-possessions "$(PPP_PRIOR_POSSESSIONS)",) $(if $(PACE_PRIOR_COMBINED_POSSESSIONS),--pace-prior-combined-possessions "$(PACE_PRIOR_COMBINED_POSSESSIONS)",); \
	elif [ -n "$(SEASON_FROM)" ] && [ -n "$(SEASON_TO)" ]; then \
		uv run python -m scripts.build_training_dataset --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" --validation-from "$(VALIDATION_FROM)" --test-from "$(TEST_FROM)" --output "$(OUTPUT)" $(if $(EMBARGO_DAYS),--embargo-days "$(EMBARGO_DAYS)",) $(if $(HISTORY_WINDOW),--history-window "$(HISTORY_WINDOW)",) $(if $(PPP_PRIOR_POSSESSIONS),--ppp-prior-possessions "$(PPP_PRIOR_POSSESSIONS)",) $(if $(PACE_PRIOR_COMBINED_POSSESSIONS),--pace-prior-combined-possessions "$(PACE_PRIOR_COMBINED_POSSESSIONS)",); \
	else \
		echo "SEASON or both SEASON_FROM and SEASON_TO are required"; exit 2; \
	fi

train-models:
	@if [ -z "$(DATASET)" ] || [ -z "$(OUTPUT)" ]; then \
		echo "DATASET and OUTPUT are required"; exit 2; \
	else \
		uv run --extra ml python -m scripts.train_models --dataset "$(DATASET)" --output "$(OUTPUT)" $(if $(ITERATIONS),--iterations "$(ITERATIONS)",) $(if $(DEPTH),--depth "$(DEPTH)",) $(if $(LEARNING_RATE),--learning-rate "$(LEARNING_RATE)",) $(if $(L2_LEAF_REG),--l2-leaf-reg "$(L2_LEAF_REG)",) $(if $(RANDOM_SEED),--random-seed "$(RANDOM_SEED)",) $(if $(EARLY_STOPPING_ROUNDS),--early-stopping-rounds "$(EARLY_STOPPING_ROUNDS)",) $(if $(MAX_ROWS_PER_PARTITION),--max-rows-per-partition "$(MAX_ROWS_PER_PARTITION)",); \
	fi

predict-models:
	@if [ -z "$(MODEL)" ] || [ -z "$(OUTPUT)" ]; then \
		echo "MODEL and OUTPUT are required"; exit 2; \
	elif [ -n "$(SEASON)" ] && { [ -n "$(SEASON_FROM)" ] || [ -n "$(SEASON_TO)" ]; }; then \
		echo "choose SEASON or SEASON_FROM/SEASON_TO"; exit 2; \
	elif [ -n "$(GAME_ID)" ] && [ -z "$(SEASON)" ]; then \
		echo "GAME_ID requires SEASON"; exit 2; \
	elif [ -n "$(SEASON)" ]; then \
		uv run --extra ml python -m scripts.predict_models --model "$(MODEL)" --season "$(SEASON)" --output "$(OUTPUT)" $(if $(GAME_ID),--game-id "$(GAME_ID)",); \
	elif [ -n "$(SEASON_FROM)" ] && [ -n "$(SEASON_TO)" ]; then \
		uv run --extra ml python -m scripts.predict_models --model "$(MODEL)" --season-from "$(SEASON_FROM)" --season-to "$(SEASON_TO)" --output "$(OUTPUT)"; \
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
