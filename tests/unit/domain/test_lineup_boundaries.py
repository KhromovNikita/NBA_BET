"""Boundary coverage and fail-closed lineup lookup tests."""

from __future__ import annotations

import pytest

from nba_live_totals.domain.lineup_reconstructor import DeterministicLineupReconstructor
from nba_live_totals.domain.lineups import Substitute, find_lineup_segment
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary
from tests.unit.domain.lineup_helpers import (
    make_lineup_event,
    make_normalized_game,
    make_reconstruction_context,
    make_segment,
)


def test_every_event_side_boundary_has_exactly_one_inclusive_segment() -> None:
    normalized = make_normalized_game(
        make_lineup_event(1),
        make_lineup_event(2, Substitute(10, 101, 106), clock_ms=700_000),
        make_lineup_event(3, Substitute(20, 201, 206), period=2),
        make_lineup_event(4, period=5, clock_ms=300_000),
    )
    result = DeterministicLineupReconstructor().reconstruct(
        make_reconstruction_context(), normalized
    )

    assert result.segments[0].start_boundary == EventBoundary(1, BoundarySide.BEFORE)
    assert result.segments[-1].end_boundary == EventBoundary(4, BoundarySide.AFTER)
    for sequence_number in range(1, 5):
        for side in BoundarySide:
            boundary = EventBoundary(sequence_number, side)
            matches = [
                segment
                for segment in result.segments
                if segment.start_boundary <= boundary <= segment.end_boundary
            ]
            assert len(matches) == 1
            assert find_lineup_segment(result.segments, boundary) is matches[0]


def test_lookup_is_inclusive_at_both_segment_ends() -> None:
    segment = make_segment()

    assert find_lineup_segment((segment,), segment.start_boundary) is segment
    assert find_lineup_segment((segment,), segment.end_boundary) is segment


def test_lookup_fails_closed_for_zero_or_multiple_matches() -> None:
    boundary = EventBoundary(2, BoundarySide.BEFORE)
    first = make_segment(end_boundary=boundary)
    overlapping = make_segment(
        segment_number=2,
        start_boundary=boundary,
        end_boundary=EventBoundary(3, BoundarySide.AFTER),
    )

    with pytest.raises(ValueError, match="exactly one"):
        find_lineup_segment((first,), EventBoundary(3, BoundarySide.AFTER))
    with pytest.raises(ValueError, match="exactly one"):
        find_lineup_segment((first, overlapping), boundary)


def test_lookup_rejects_invalid_boundary_type() -> None:
    with pytest.raises(ValueError, match="boundary"):
        find_lineup_segment((make_segment(),), object())  # type: ignore[arg-type]
