"""Shared exact PBP lineage for analytical dataset compatibility."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from nba_live_totals.domain.identifiers import GameId

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256(value: object, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PbpLineage:
    """The five fields that must match before analytical datasets may be joined."""

    game_id: GameId
    input_pbp_document_id: UUID
    input_pbp_sha256: str
    canonical_event_sha256: str
    game_context_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, GameId):
            raise ValueError("game ID must be a GameId")
        if type(self.input_pbp_document_id) is not UUID:
            raise ValueError("input PBP document ID must be a UUID")
        _require_sha256(self.input_pbp_sha256, field_name="input PBP SHA-256")
        _require_sha256(self.canonical_event_sha256, field_name="canonical event SHA-256")
        _require_sha256(self.game_context_sha256, field_name="game context SHA-256")


def require_matching_pbp_lineage(left: PbpLineage, right: PbpLineage) -> None:
    """Fail closed unless two analytical inputs have identical PBP lineage."""
    if not isinstance(left, PbpLineage) or not isinstance(right, PbpLineage):
        raise TypeError("PBP lineage values must be PbpLineage")
    if left != right:
        raise ValueError("PBP lineage must match exactly")


__all__ = ["PbpLineage", "require_matching_pbp_lineage"]
