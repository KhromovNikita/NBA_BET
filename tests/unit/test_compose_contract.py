from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_compose_publishes_dev_ports_on_loopback_only() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    for port in (8000, 5432, 8123, 6379):
        assert f'"127.0.0.1:{port}:{port}"' in compose
        assert f'"{port}:{port}"' not in compose


def test_compose_project_name_supports_unique_parallel_clone_override() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'name: "${COMPOSE_PROJECT_NAME:-nba-live-totals}"' in compose
    assert "COMPOSE_PROJECT_NAME" in readme
    assert "parallel clone" in readme.lower()
