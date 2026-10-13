import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]


def test_baseline_runbook_documents_partial_pregame_and_atomic_replacement() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    for value in ("build_baselines", "baseline-jsonl-v1", "pregame live", "atomic replacement"):
        assert value in readme
    assert "build-baselines:" in makefile and "OUTPUT is required" in makefile


def test_feature_dataset_runbook_pins_offline_causal_two_table_contract() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    required = (
        "backfill -> build_possessions -> build_lineups -> build_dataset",
        "strict causal manifest",
        "no NBA network fallback",
        "--rebuild",
        "--verify",
        "quality-invalid",
        "allocated",
        "writing",
        "verified",
        "nba.game_state_features_v1",
        "nba.player_pregame_features_v1",
        "1_000_000",
    )
    assert all(value in readme for value in required)
    assert "dataset" in makefile.splitlines()[0]
    target = makefile.split("dataset:", 1)[1].split("\n\n", 1)[0]
    assert "scripts.build_dataset" in target
    assert "GAME_ID requires SEASON" in target
    assert "choose REBUILD or VERIFY" in target


def test_feature_acceptance_is_documented_and_offline() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    acceptance = (PROJECT_ROOT / "tests/e2e/test_feature_engineering.py").read_text(
        encoding="utf-8"
    )
    assert "tests/e2e/test_feature_engineering.py" in readme
    assert "far-future" in readme
    assert "NBA call" in readme
    assert "network_fallback_allowed" in acceptance


@pytest.mark.parametrize(
    "missing",
    [
        "TEST_NBA_SEASON",
        "TEST_NBA_GAME_ID",
        "TEST_DATABASE_URL",
        "TEST_CLICKHOUSE_URL",
        "TEST_LIVE_ISOLATED_STORES",
    ],
)
def test_lineup_live_canary_requires_every_explicit_opt_in(missing: str) -> None:
    environment = os.environ | {
        "TEST_NBA_SEASON": "2022-23",
        "TEST_NBA_GAME_ID": "0022200001",
        "TEST_DATABASE_URL": "postgresql+asyncpg://nba:nba@127.0.0.1:1/nba",
        "TEST_CLICKHOUSE_URL": "http://127.0.0.1:1",
        "TEST_LIVE_ISOLATED_STORES": "1",
    }
    environment.pop(missing)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/live/test_nba_lineup_canary.py",
            "--collect-only",
            "-q",
            "-rs",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 5, result.stdout + result.stderr
    assert "TEST_LIVE_ISOLATED_STORES=1" in result.stdout


def lineup_canary_namespace() -> dict[str, object]:
    # Importing the opted-in module defines the harness but performs no IO.
    import runpy

    with pytest.MonkeyPatch.context() as patch:
        for key, value in {
            "TEST_NBA_SEASON": "2022-23",
            "TEST_NBA_GAME_ID": "0022200001",
            "TEST_DATABASE_URL": "postgresql+asyncpg://nba:nba@127.0.0.1:1/nba",
            "TEST_CLICKHOUSE_URL": "http://127.0.0.1:1",
            "TEST_LIVE_ISOLATED_STORES": "1",
        }.items():
            patch.setenv(key, value)
        return runpy.run_path(str(PROJECT_ROOT / "tests/live/test_nba_lineup_canary.py"))


def test_lineup_live_canary_journals_before_failure_and_refuses_collisions() -> None:
    from typing import Any, cast

    from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, select

    namespace = cast(dict[str, Any], lineup_canary_namespace())
    owned = namespace["OwnedIdentities"]()
    table = Table("canary_identities", MetaData(), Column("id", Integer, primary_key=True))
    engine = create_engine("sqlite://")
    table.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=9))
    event.listen(engine, "before_cursor_execute", owned.before_insert)
    with engine.begin() as connection:
        connection.execute(table.insert().values([{"id": 1}, {"id": 2}]))
        connection.execute(table.insert(), [{"id": 3}, {"id": 4}])
        with pytest.raises(AssertionError, match="unowned identity collision"):
            connection.execute(table.insert().values(id=9))
        assert owned.rows[table] == {(1,), (2,), (3,), (4,)}
        with pytest.raises(RuntimeError, match="later work"):
            connection.execute(table.insert().values(id=5))
            raise RuntimeError("later work failed after persistence")
        assert (5,) in owned.rows[table]
        assert set(connection.scalars(select(table.c.id))) == {1, 2, 3, 4, 5, 9}
    engine.dispose()


def test_lineup_live_canary_uses_owned_snapshot_cleanup() -> None:
    from typing import Any, cast
    from unittest.mock import AsyncMock
    from uuid import UUID

    namespace = cast(dict[str, Any], lineup_canary_namespace())
    import asyncio

    client = AsyncMock()
    owned = namespace["OwnedIdentities"]()
    first = UUID("00000000-0000-0000-0000-000000000001")
    second = UUID("00000000-0000-0000-0000-000000000002")
    owned.lineup_ids.update((first, second))
    asyncio.run(owned.cleanup_clickhouse(client))
    assert client.execute.await_args_list == [
        (
            (
                "ALTER TABLE nba.lineup_segments_v1 DELETE WHERE snapshot_id = "
                "'00000000-0000-0000-0000-000000000001' SETTINGS mutations_sync = 2",
            ),
            {},
        ),
        (
            (
                "ALTER TABLE nba.lineup_segments_v1 DELETE WHERE snapshot_id = "
                "'00000000-0000-0000-0000-000000000002' SETTINGS mutations_sync = 2",
            ),
            {},
        ),
    ]
    client.reset_mock()
    asyncio.run(namespace["OwnedIdentities"]().cleanup_clickhouse(client))
    client.execute.assert_not_awaited()


def test_lineup_operator_workflow_documents_guards_and_offline_recovery() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "build-lineups" in makefile.splitlines()[0]
    target = makefile.split("build-lineups:", 1)[1].split("\n\n", 1)[0]
    possession_target = makefile.split("build-possessions:", 1)[1].split("\n\n", 1)[0]
    assert target == possession_target.replace("build_possessions", "build_lineups")
    for setting in (
        "LINEUP_NORMALIZER_VERSION=nba-pbp-v3-lineup-v1",
        "LINEUP_RECONSTRUCTOR_VERSION=causal-lineup-v1",
        "LINEUP_RULES_VERSION=nba-5x5-v1",
        "LINEUP_OUTPUT_SCHEMA_VERSION=lineup-segments-v1",
        "LINEUP_BUILD_MAX_CONCURRENCY=4",
        "LINEUP_CLAIM_LEASE_SECONDS=300",
    ):
        assert setting in env_example
    section = readme.split("## Lineup reconstruction runbook", 1)[1].split("\n## ", 1)[0]
    assert_command_order(
        section,
        (
            "alembic upgrade head",
            "python -m scripts.migrate_clickhouse",
            "python -m scripts.build_lineups",
        ),
    )
    for invocation in (
        "SEASON=2025-26",
        "SEASON=2025-26 GAME_ID=0022500123",
        "SEASON_FROM=2021-22 SEASON_TO=2025-26",
        "SEASON=2025-26 REBUILD=1",
        "SEASON=2025-26 VERIFY=1",
    ):
        assert f"make build-lineups {invocation}" in section
    for contract in (
        "dual-input",
        "no NBA.com requests",
        "--rebuild",
        "--verify",
        "invalid",
        "authoritative PostgreSQL pointer",
        "snapshot_id",
        "lineup_revision_id",
        "generation",
        "allocated",
        "writing",
        "verified",
        "poisoned",
        "superseded",
        "orphan",
        "raw descriptions",
        "credentials",
        "deferred",
        "possessions_v1",
        "lineup_segments_v1",
        "TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1",
        "exit code `0`",
        "exit code `1`",
        "exit code `2`",
        "status `130`",
    ):
        assert contract in section


def assert_command_order(text: str, commands: tuple[str, ...]) -> None:
    positions = tuple(text.index(command) for command in commands)

    assert positions == tuple(sorted(positions))


def test_operator_documentation_matches_container_and_persistence_contracts() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "docker" / "api.Dockerfile").read_text(encoding="utf-8")

    assert "Docker Compose v2.17+" in readme
    assert "docker compose version --short" in readme
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    assert dockerfile.index("COPY alembic.ini ./") < dockerfile.index("USER app")
    assert dockerfile.index("COPY alembic ./alembic") < dockerfile.index("USER app")
    assert 'CMD ["/app/.venv/bin/uvicorn"' in dockerfile
    assert 'CMD ["uv", "run"' not in dockerfile
    assert "docker compose exec -T api /app/.venv/bin/alembic upgrade head" in readme

    persistence_section = readme.split("## Persistence and partial-outage verification", 1)[1]
    linux_commands = persistence_section.split("### Linux and macOS", 1)[1].split(
        "### Windows PowerShell", 1
    )[0]
    powershell_commands = persistence_section.split("### Windows PowerShell", 1)[1]
    recreation_commands = (
        "redis-cli SET phase1_volume_check preserved",
        "redis-cli SAVE",
        "docker compose stop redis",
        "docker compose rm -f redis",
        "docker compose up -d --wait redis",
        "redis-cli GET phase1_volume_check",
    )
    for command in (
        "redis-cli SET phase1_volume_check preserved",
        "redis-cli SAVE",
        "docker compose rm -f redis",
        "redis-cli GET phase1_volume_check",
        "redis-cli DEL phase1_volume_check",
        "docker compose start redis",
    ):
        assert readme.count(command) == 2
    assert readme.count("docker compose stop redis") == 4
    assert readme.count("docker compose up -d --wait redis") == 4
    assert_command_order(linux_commands, recreation_commands)
    assert_command_order(powershell_commands, recreation_commands)
    assert linux_commands.index("docker compose stop redis", linux_commands.index("GET")) < (
        linux_commands.index("curl --silent --output readiness.json")
    )
    powershell_outage_stop = powershell_commands.index(
        "docker compose stop redis", powershell_commands.index("GET")
    )
    assert powershell_outage_stop < powershell_commands.index(
        "curl.exe --silent --output readiness.json"
    )

    assert "$hadDatabaseUrl = Test-Path Env:DATABASE_URL" in readme
    assert "try {" in readme
    assert "} finally {" in readme
    assert "$env:DATABASE_URL = $previousDatabaseUrl" in readme
    assert "Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue" in readme
    assert "${API_HOST_PORT:-8000}" in readme
    assert "${POSTGRES_HOST_PORT:-5432}" in readme
    assert "$apiHostPort" in readme
    assert "$postgresHostPort" in readme
    assert "Do not set `*_HOST_PORT` only in `.env`" in readme
    assert "export API_HOST_PORT=18000" in readme
    assert "$env:API_HOST_PORT = '18000'" in readme
    for variable in (
        "TEST_DATABASE_URL",
        "TEST_CLICKHOUSE_URL",
        "TEST_REDIS_URL",
        "TEST_API_URL",
    ):
        assert variable in readme
    assert "ALLOW_REMOTE_TEST_TARGETS=1" in readme
    assert "TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS=1" in readme


def test_historical_backfill_runbook_documents_supported_operations() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for required_text in (
        "python -m scripts.backfill_games --season 2025-26",
        "--season-from 2021-22",
        "--season-to 2025-26",
        "--game-id 0022500123",
        "--refresh",
        "resume",
        "exit code `1`",
        "Regular Season",
        "Playoffs",
        "multi-hour",
        "rate limiting",
        "structured summary",
        "Ctrl+C",
        "raw JSON",
        "PostgreSQL",
        "opt-in live canary",
        "current at specification time",
        "future ranges",
    ):
        assert required_text in readme

    for forbidden_route in (
        "GET /games",
        "GET /predictions",
        "POST /games",
        "POST /predictions",
    ):
        assert forbidden_route not in readme


def test_make_backfill_requires_one_explicit_season_selector() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("backfill:", 1)[1].split("\n\n", 1)[0]

    assert "backfill" in makefile.splitlines()[0]
    assert 'uv run python -m scripts.backfill_games --season "$(SEASON)"' in target
    assert (
        'uv run python -m scripts.backfill_games --season-from "$(SEASON_FROM)" '
        '--season-to "$(SEASON_TO)"'
    ) in target
    assert "SEASON or both SEASON_FROM and SEASON_TO are required" in target
    assert "choose SEASON or SEASON_FROM/SEASON_TO, not both" in target
    assert "$(if $(filter 1,$(REFRESH)),--refresh,)" in target
    assert re.search(r"\b20[0-9]{2}-[0-9]{2}\b", target) is None


def test_phase3_operator_contract_matches_cli_and_recovery_model() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "migrate-clickhouse" in makefile.splitlines()[0]
    assert "build-possessions" in makefile.splitlines()[0]
    migration_target = makefile.split("migrate-clickhouse:", 1)[1].split("\n\n", 1)[0]
    build_target = makefile.split("build-possessions:", 1)[1].split("\n\n", 1)[0]
    assert "uv run python -m scripts.migrate_clickhouse" in migration_target
    assert 'uv run python -m scripts.build_possessions --season "$(SEASON)"' in build_target
    assert (
        'uv run python -m scripts.build_possessions --season-from "$(SEASON_FROM)" '
        '--season-to "$(SEASON_TO)"' in build_target
    )
    assert "--game-id" in build_target
    assert "--rebuild" in build_target
    assert "--verify" in build_target
    assert "choose REBUILD or VERIFY, not both" in build_target
    assert "GAME_ID requires SEASON" in build_target
    assert "REFRESH" not in build_target
    assert "backfill_games" not in build_target

    for setting in (
        "POSSESSION_RESOURCE_LEASE_SECONDS=300",
        "POSSESSION_BUILD_MAX_CONCURRENCY=4",
        "CLICKHOUSE_OPERATION_TIMEOUT_SECONDS=30",
        "POSSESSION_NORMALIZER_VERSION=nba-pbp-v2",
        "POSSESSION_PARSER_VERSION=analytical-v1",
        "POSSESSION_RULES_VERSION=nba-2025-v1",
        "POSSESSION_OUTPUT_SCHEMA_VERSION=possessions-v1",
    ):
        assert setting in env_example

    phase3 = readme.split("## Possession reconstruction runbook", 1)[1].split("\n## ", 1)[0]
    assert_command_order(
        phase3,
        (
            "alembic upgrade head",
            "python -m scripts.migrate_clickhouse",
            "python -m scripts.build_possessions",
        ),
    )
    for required_text in (
        "--rebuild",
        "--verify",
        "no NBA.com requests",
        "authoritative PostgreSQL pointer",
        "immutable",
        "orphan",
        "poisoned",
        "writing",
        "Ctrl+C",
        "exit code `0`",
        "exit code `1`",
        "exit code `2`",
        "status `130`",
        "TEST_NBA_SEASON",
        "TEST_NBA_GAME_ID",
        "Phase 4",
    ):
        assert required_text in phase3


def test_possession_live_canary_requires_explicit_isolated_store_targets() -> None:
    environment = os.environ | {
        "TEST_NBA_SEASON": "2022-23",
        "TEST_NBA_GAME_ID": "0022200001",
    }
    for name in (
        "TEST_DATABASE_URL",
        "TEST_CLICKHOUSE_URL",
        "TEST_LIVE_ISOLATED_STORES",
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/live/test_nba_possession_canary.py",
            "--collect-only",
            "-q",
            "-rs",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 5
    assert "TEST_DATABASE_URL" in output
    assert "TEST_CLICKHOUSE_URL" in output
    assert "TEST_LIVE_ISOLATED_STORES=1" in output


def test_possession_live_runbook_preserves_and_restores_powershell_environment() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    phase3 = readme.split("## Possession reconstruction runbook", 1)[1].split("\n## ", 1)[0]

    for variable in (
        "TEST_NBA_SEASON",
        "TEST_NBA_GAME_ID",
        "TEST_DATABASE_URL",
        "TEST_CLICKHOUSE_URL",
        "TEST_LIVE_ISOLATED_STORES",
    ):
        assert f"$had{variable.title().replace('_', '')} = Test-Path Env:{variable}" in phase3
        assert f"Remove-Item Env:{variable} -ErrorAction SilentlyContinue" in phase3
    assert "} finally {" in phase3


def test_live_canary_cleanup_uses_only_harness_owned_identities() -> None:
    source = (PROJECT_ROOT / "tests" / "live" / "test_nba_possession_canary.py").read_text(
        encoding="utf-8"
    )
    cleanup = source.split("    async def cleanup(self) -> None:", 1)[1].split(
        "\n\n@pytest.fixture", 1
    )[0]
    test_body = source.split(
        "async def test_one_completed_live_game_builds_verifies_and_reruns_offline", 1
    )[1]

    assert "season_games =" not in cleanup
    assert "self.game_ids" in cleanup
    assert "self.player_ids" in cleanup
    assert "self.team_ids" in cleanup
    assert ".run_ids.append(" not in test_body
