"""Signature and runtime protocol tests for Phase 5 boundaries."""

from __future__ import annotations

import inspect

from nba_live_totals.application.features import (
    FeatureCalculator,
    FeatureEventNormalizer,
    FeatureSnapshotStore,
    FeatureSourceReader,
    FeatureWorkStore,
)


def _parameters(protocol: type[object], method: str) -> tuple[str, ...]:
    return tuple(inspect.signature(getattr(protocol, method)).parameters)


def test_feature_pure_boundary_signatures_are_exact() -> None:
    assert _parameters(FeatureEventNormalizer, "normalize") == ("self", "snapshot")
    assert _parameters(FeatureSourceReader, "read_target") == ("self", "target")
    assert _parameters(FeatureSourceReader, "read_inputs") == ("self", "claim")
    assert _parameters(FeatureCalculator, "calculate") == ("self", "inputs", "events")


def test_feature_work_store_signatures_are_exact() -> None:
    expected = {
        "start_run": ("self", "request"),
        "select_games": ("self", "request"),
        "resolve_target": ("self", "game_id"),
        "claim_game": ("self", "run_id", "target", "target_player_ids", "versions", "rebuild"),
        "mark_writing": (
            "self",
            "claim",
            "expected_state_row_count",
            "expected_state_output_sha256",
            "expected_player_row_count",
            "expected_player_output_sha256",
            "expected_feature_output_sha256",
            "expected_issue_codes",
            "result_status",
        ),
        "mark_verified": ("self", "claim", "verification"),
        "publish_snapshot": ("self", "claim"),
        "poison_attempt": ("self", "claim", "error_code", "error_type"),
        "fail_game": ("self", "claim", "error_code", "error_type"),
        "record_unclaimed_failure": ("self", "run_id", "game_id", "error_code", "error_type"),
        "resolve_current": ("self", "target", "target_player_ids", "versions"),
        "finish_run": ("self", "run_id", "result"),
    }
    for method, parameters in expected.items():
        assert _parameters(FeatureWorkStore, method) == parameters


def test_feature_snapshot_store_signatures_are_exact() -> None:
    assert _parameters(FeatureSnapshotStore, "write_snapshot") == ("self", "claim", "dataset")
    assert _parameters(FeatureSnapshotStore, "verify_snapshot") == ("self", "attempt")
    assert _parameters(FeatureSnapshotStore, "read_snapshot") == ("self", "reference")


def test_protocols_are_runtime_checkable_and_reject_incomplete_objects() -> None:
    class Incomplete:
        pass

    value = Incomplete()
    assert not isinstance(value, FeatureEventNormalizer)
    assert not isinstance(value, FeatureSourceReader)
    assert not isinstance(value, FeatureCalculator)
    assert not isinstance(value, FeatureWorkStore)
    assert not isinstance(value, FeatureSnapshotStore)
