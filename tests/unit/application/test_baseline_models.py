from pathlib import Path

import pytest

from nba_live_totals.application.baselines.models import (
    BaselineArtifactStats,
    BuildBaselinesRequest,
    BuildBaselinesResult,
)
from nba_live_totals.domain.identifiers import GameId, SeasonId


def test_request_range_and_result_exit_contract(tmp_path: Path) -> None:
    request = BuildBaselinesRequest.from_range(
        SeasonId("2024-25"), SeasonId("2025-26"), output_path=tmp_path / "out.jsonl"
    )
    assert request.seasons == (SeasonId("2024-25"), SeasonId("2025-26"))
    assert (
        BuildBaselinesResult(
            request.output_path, BaselineArtifactStats(1, 1, 3, 3, 0, 0, "a" * 64, "b" * 64)
        ).exit_code
        == 0
    )
    assert (
        BuildBaselinesResult(
            request.output_path, BaselineArtifactStats(1, 1, 3, 2, 0, 1, "a" * 64, "b" * 64)
        ).exit_code
        == 1
    )


def test_invalid_selector_and_counts_reject(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BuildBaselinesRequest(
            (SeasonId("2025-26"),), tmp_path / "out", game_id=GameId("0022400001")
        )
    with pytest.raises(ValueError):
        BaselineArtifactStats(1, 1, 4, 3, 0, 0, "a" * 64, "b" * 64)
