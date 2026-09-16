"""Golden projection/checksum coverage for normalized NBA lineups."""

from __future__ import annotations

import json
from pathlib import Path

from nba_live_totals.domain.lineup_hashing import (
    normalized_lineups_checksum,
    normalized_lineups_projection,
)
from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import LineupStatus
from nba_live_totals.infrastructure.nba.lineup_normalizer import NBAPlayByPlayV3LineupNormalizer

from .test_lineup_normalizer_inputs import _reconstruction_context
from .test_lineup_normalizer_substitutions import _substitution_snapshot


def test_normalized_projection_is_golden() -> None:
    snapshot = _substitution_snapshot()
    normalized = NBAPlayByPlayV3LineupNormalizer().normalize(snapshot)
    fixture_path = (
        Path(__file__).parents[3] / "fixtures" / "nba" / "lineups" / "normalized_v1.expected.json"
    )
    expected = json.loads(fixture_path.read_text())
    projection = json.loads(json.dumps(normalized_lineups_projection(normalized)))
    assert expected["projection"] == projection
    assert expected["checksum"] == normalized_lineups_checksum(normalized)

    result = DeterministicLineupReconstructor().reconstruct(
        _reconstruction_context(snapshot), normalized
    )
    assert result.status is LineupStatus.VALID
    assert len(result.segments) == 2
