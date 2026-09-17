from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_compose_publishes_dev_ports_on_loopback_only() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    ports = {
        "API_HOST_PORT": 8000,
        "POSTGRES_HOST_PORT": 5432,
        "CLICKHOUSE_HOST_PORT": 8123,
        "REDIS_HOST_PORT": 6379,
    }
    for variable, port in ports.items():
        assert f'"127.0.0.1:${{{variable}:-{port}}}:{port}"' in compose
        assert f'"{port}:{port}"' not in compose


def test_compose_project_name_supports_unique_parallel_clone_override() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'name: "${COMPOSE_PROJECT_NAME:-nba-live-totals}"' in compose
    assert "COMPOSE_PROJECT_NAME" in readme
    assert "parallel clone" in readme.lower()


def test_api_image_contains_historical_backfill_module_before_final_sync() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "api.Dockerfile").read_text(encoding="utf-8")

    scripts_copy = dockerfile.index("COPY scripts ./scripts")
    final_sync = dockerfile.index("RUN uv sync --frozen --no-dev", scripts_copy)
    non_root_user = dockerfile.index("USER app")

    assert scripts_copy < final_sync < non_root_user


def test_api_image_contains_clickhouse_migrations_before_switching_to_non_root() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "api.Dockerfile").read_text(encoding="utf-8")

    clickhouse_copy = dockerfile.index("COPY clickhouse ./clickhouse")
    non_root_user = dockerfile.index("USER app")

    assert clickhouse_copy < non_root_user


def test_compose_exposes_phase3_settings_without_adding_a_service() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    api_environment = compose.split("    environment:\n", 1)[1].split("    ports:\n", 1)[0]

    expected_settings = {
        "LINEUP_CLAIM_LEASE_SECONDS": "300",
        "LINEUP_BUILD_MAX_CONCURRENCY": "4",
        "LINEUP_NORMALIZER_VERSION": "nba-pbp-v3-lineup-v1",
        "LINEUP_RECONSTRUCTOR_VERSION": "causal-lineup-v1",
        "LINEUP_RULES_VERSION": "nba-5x5-v1",
        "LINEUP_OUTPUT_SCHEMA_VERSION": "lineup-segments-v1",
        "POSSESSION_RESOURCE_LEASE_SECONDS": "300",
        "POSSESSION_BUILD_MAX_CONCURRENCY": "4",
        "CLICKHOUSE_OPERATION_TIMEOUT_SECONDS": "30",
        "POSSESSION_NORMALIZER_VERSION": "nba-pbp-v2",
        "POSSESSION_PARSER_VERSION": "analytical-v1",
        "POSSESSION_RULES_VERSION": "nba-2025-v1",
        "POSSESSION_OUTPUT_SCHEMA_VERSION": "possessions-v1",
    }
    for name, default in expected_settings.items():
        assert f'{name}: "${{{name}:-{default}}}"' in api_environment

    services = compose.split("services:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    service_names = tuple(
        line.removesuffix(":").strip()
        for line in services.splitlines()
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":")
    )
    assert service_names == ("api", "postgres", "clickhouse", "redis")
