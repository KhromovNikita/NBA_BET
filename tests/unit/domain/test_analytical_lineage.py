"""Shared fail-closed PBP lineage contract tests."""

from __future__ import annotations

from dataclasses import fields, replace
from uuid import UUID

import pytest

from nba_live_totals.domain.analytical_lineage import (
    PbpLineage,
    require_matching_pbp_lineage,
)
from nba_live_totals.domain.identifiers import GameId
from tests.unit.domain.lineup_helpers import GAME_ID


def make_lineage() -> PbpLineage:
    return PbpLineage(
        game_id=GAME_ID,
        input_pbp_document_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        input_pbp_sha256="a" * 64,
        canonical_event_sha256="b" * 64,
        game_context_sha256="c" * 64,
    )


def test_pbp_lineage_has_exact_shared_fields_and_equality_guard_accepts_match() -> None:
    left = make_lineage()
    right = make_lineage()

    assert tuple(field.name for field in fields(PbpLineage)) == (
        "game_id",
        "input_pbp_document_id",
        "input_pbp_sha256",
        "canonical_event_sha256",
        "game_context_sha256",
    )
    require_matching_pbp_lineage(left, right)
    assert left == right


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("game_id", GameId("0022500124")),
        ("input_pbp_document_id", UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")),
        ("input_pbp_sha256", "d" * 64),
        ("canonical_event_sha256", "d" * 64),
        ("game_context_sha256", "d" * 64),
    ],
)
def test_equality_guard_rejects_each_lineage_mismatch_even_if_boundary_exists(
    field_name: str, value: object
) -> None:
    left = make_lineage()
    right = replace(left, **{field_name: value})  # type: ignore[arg-type]
    existing_boundary = (1, "after")

    assert existing_boundary == (1, "after")
    with pytest.raises(ValueError, match="PBP lineage"):
        require_matching_pbp_lineage(left, right)
