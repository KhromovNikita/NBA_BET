import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


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
        "POSSESSION_NORMALIZER_VERSION=nba-pbp-v1",
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
