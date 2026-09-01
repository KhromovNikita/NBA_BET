from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nba_live_totals.application.ingestion.models import (
    BackfillRequest,
    BackfillResult,
    BackfillStatus,
    ClaimAction,
    ClaimDecision,
    ClaimLostError,
    IngestionResourceError,
    ResourceClaim,
    ResourceKey,
    ResourceType,
    StoredSourceDocument,
)
from nba_live_totals.domain import (
    GameId,
    JsonObject,
    SeasonId,
    SourceDocument,
    canonical_json_checksum,
)


def test_backfill_request_expands_an_inclusive_season_range() -> None:
    request = BackfillRequest.for_range(SeasonId("2023-24"), SeasonId("2025-26"))

    assert request.seasons == (
        SeasonId("2023-24"),
        SeasonId("2024-25"),
        SeasonId("2025-26"),
    )


def test_backfill_request_expands_ranges_across_a_century_rollover() -> None:
    request = BackfillRequest.for_range(SeasonId("2099-00"), SeasonId("2100-01"))

    assert request.seasons == (SeasonId("2099-00"), SeasonId("2100-01"))


def test_backfill_request_rejects_a_reverse_range_and_a_mismatched_game() -> None:
    with pytest.raises(ValueError, match="range"):
        BackfillRequest.for_range(SeasonId("2025-26"), SeasonId("2024-25"))
    with pytest.raises(ValueError, match="season"):
        BackfillRequest(
            seasons=(SeasonId("2024-25"), SeasonId("2025-26")),
            game_id=GameId("0022500123"),
        )


def test_targeted_backfill_request_requires_a_game_from_the_selected_season() -> None:
    matching_request = BackfillRequest(seasons=(SeasonId("2025-26"),), game_id=GameId("0022500123"))

    assert matching_request.game_id == GameId("0022500123")
    with pytest.raises(ValueError, match="season"):
        BackfillRequest(seasons=(SeasonId("2024-25"),), game_id=GameId("0022500123"))


def test_backfill_request_requires_at_least_one_season() -> None:
    with pytest.raises(ValueError, match="season"):
        BackfillRequest(seasons=())


def test_resource_key_factories_use_stable_resource_type_prefixes() -> None:
    season = SeasonId("2025-26")
    game_id = GameId("0022500123")

    assert ResourceKey.schedule(season) == ResourceKey(ResourceType.SCHEDULE, season)
    assert str(ResourceKey.schedule(season)) == "schedule:2025-26"
    assert str(ResourceKey.box_score(game_id)) == "box_score:0022500123"
    assert str(ResourceKey.play_by_play(game_id)) == "play_by_play:0022500123"


def test_resource_claim_requires_a_positive_fencing_version() -> None:
    with pytest.raises(ValueError, match="version"):
        ResourceClaim(ResourceKey.schedule(SeasonId("2025-26")), uuid4(), 0)


def test_claim_decision_enforces_the_action_matrix() -> None:
    claim = ResourceClaim(ResourceKey.schedule(SeasonId("2025-26")), uuid4(), 1)
    document = StoredSourceDocument(uuid4(), _source_document())

    with pytest.raises(ValueError, match="claim"):
        ClaimDecision(ClaimAction.FETCH)
    with pytest.raises(ValueError, match="stored document"):
        ClaimDecision(ClaimAction.REPARSE, claim=claim)
    with pytest.raises(ValueError, match="claim"):
        ClaimDecision(ClaimAction.SKIP, claim=claim)
    with pytest.raises(ValueError, match="stored document"):
        ClaimDecision(ClaimAction.SKIP)
    with pytest.raises(ValueError, match="stored document"):
        ClaimDecision(ClaimAction.BUSY, stored_document=document)

    assert ClaimDecision(ClaimAction.FETCH, claim=claim).claim == claim
    assert (
        ClaimDecision(ClaimAction.REPARSE, claim=claim, stored_document=document).stored_document
        == document
    )
    assert ClaimDecision(ClaimAction.SKIP, stored_document=document).stored_document == document
    assert ClaimDecision(ClaimAction.BUSY) == ClaimDecision(ClaimAction.BUSY)


def test_reparse_decision_requires_a_document_for_the_claimed_resource() -> None:
    claim = ResourceClaim(ResourceKey.box_score(GameId("0022500123")), uuid4(), 1)

    with pytest.raises(ValueError, match="resource key"):
        ClaimDecision(
            ClaimAction.REPARSE,
            claim=claim,
            stored_document=StoredSourceDocument(uuid4(), _source_document()),
        )
    with pytest.raises(ValueError, match="resource key"):
        ClaimDecision(
            ClaimAction.REPARSE,
            claim=claim,
            stored_document=StoredSourceDocument(
                uuid4(), _source_document(resource_key="box_score:0022500124")
            ),
        )

    matching_document = StoredSourceDocument(
        uuid4(), _source_document(resource_key="box_score:0022500123")
    )
    assert (
        ClaimDecision(
            ClaimAction.REPARSE, claim=claim, stored_document=matching_document
        ).stored_document
        == matching_document
    )


def test_models_are_frozen_and_errors_expose_only_safe_codes() -> None:
    result = BackfillResult(
        run_id=uuid4(),
        status=BackfillStatus.SUCCEEDED,
        discovered=1,
        succeeded=1,
        skipped=0,
        failed=0,
    )

    with pytest.raises(FrozenInstanceError):
        result.failed = 1  # type: ignore[misc]

    error = IngestionResourceError("schema_mismatch")
    assert error.code == "schema_mismatch"
    assert str(error) == "schema_mismatch"
    assert ClaimLostError().code == "claim_lost"


def _source_document(*, resource_key: str = "schedule:2025-26") -> SourceDocument:
    payload: JsonObject = {"game": {"gameId": "0022500123"}}
    return SourceDocument(
        source="nba.com",
        endpoint="scheduleleaguev2",
        resource_key=resource_key,
        requested_at=datetime.now(UTC),
        request_parameters={"LeagueID": "00"},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )
