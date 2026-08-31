from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def assert_command_order(text: str, commands: tuple[str, ...]) -> None:
    positions = tuple(text.index(command) for command in commands)

    assert positions == tuple(sorted(positions))


def test_operator_documentation_matches_container_and_persistence_contracts() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "docker" / "api.Dockerfile").read_text(encoding="utf-8")
    report = (
        PROJECT_ROOT
        / ".superpowers"
        / "sdd"
        / "2026-08-30-phase-1-infrastructure"
        / "task-10-report.md"
    ).read_text(encoding="utf-8")

    assert "Docker Compose v2.17+" in readme
    assert "docker compose version --short" in readme
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    assert dockerfile.index("COPY alembic.ini ./") < dockerfile.index("USER app")
    assert dockerfile.index("COPY alembic ./alembic") < dockerfile.index("USER app")
    assert "docker compose exec -T api uv run alembic upgrade head" in readme

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
    assert "285726148ca548277be6f04e16bbe4104d0d3909" in report
