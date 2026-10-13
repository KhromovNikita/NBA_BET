import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.baseline_harness import PRIOR, SEASON, TARGET, baseline_acceptance

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


async def test_real_baseline_cli_exports_pinned_reproducible_artifact(tmp_path: Path) -> None:
    async with baseline_acceptance() as harness:
        sources = await harness.seed_pipeline()
        before = await harness.projection()
        first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
        for path in (first, second):
            code, out, err = await harness.run_cli(
                "--season", SEASON.value, "--game-id", TARGET.value, "--output", str(path)
            )
            assert code == 1, (out, err)
            assert "status=partial" in out and "artifact_sha256=" in out
        data = first.read_bytes()
        assert data == second.read_bytes()
        manifest, *rows = data.splitlines(keepends=True)
        metadata = json.loads(manifest)
        assert metadata["sources"][0]["feature_snapshot_id"] == str(
            sources[1].reference.attempt.snapshot_id
        )
        assert len(rows) == 3 * sources[1].reference.state_row_count
        assert metadata["outcome_sha256"] == hashlib.sha256(b"".join(rows)).hexdigest()
        historical = json.loads(rows[0])
        assert historical["home_ppp"] is not None and historical["home_games_used"] == 1
        assert json.loads(rows[1])["status"] == "unavailable"
        source_dataset = await harness.source.read(sources[1])
        h, a = source_dataset.states[0].home_pregame, source_dataset.states[0].away_pregame
        assert (
            h is not None
            and a is not None
            and h.off_rating_20 is not None
            and a.def_rating_20 is not None
        )
        assert historical["home_ppp"] == (h.off_rating_20 + a.def_rating_20 + 100) // 200
        cold = tmp_path / "cold.jsonl"
        code, out, err = await harness.run_cli(
            "--season", SEASON.value, "--game-id", PRIOR.value, "--output", str(cold)
        )
        assert code == 1, (out, err)
        cold_rows = [json.loads(line) for line in cold.read_bytes().splitlines()[1:]]
        assert all(row["home_ppp"] is None for row in cold_rows if row["method"] == "historical")
        assert await harness.projection() == before
        first.write_bytes(b"old")
        code, _, _ = await harness.run_cli("--season", "2095-96", "--output", str(first))
        assert code == 2 and first.read_bytes() == b"old"


async def test_invalid_snapshot_recovers_only_at_explicit_fixture_reset(tmp_path: Path) -> None:
    async with baseline_acceptance(recovery=True) as harness:
        sources = await harness.seed_pipeline()
        dataset = await harness.source.read(sources[1])
        invalid = [s.state_number for s in dataset.states if not s.is_valid]
        assert invalid and any(s.is_valid and s.state_number > max(invalid) for s in dataset.states)
        path = tmp_path / "recovered.jsonl"
        code, out, err = await harness.run_cli(
            "--season", SEASON.value, "--game-id", TARGET.value, "--output", str(path)
        )
        assert code == 1, (out, err)
        manifest, *lines = path.read_bytes().splitlines()
        assert json.loads(manifest)["sources"][0]["source_status"] == "invalid"
        rows = [json.loads(line) for line in lines]
        assert all(row["status"] == "unavailable" for row in rows if row["state_number"] in invalid)
        assert any(
            row["home_ppp"] is not None for row in rows if row["state_number"] > max(invalid)
        )
