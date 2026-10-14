"""Canonical streaming datasets with readback and an owned atomic publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

from nba_live_totals.application.features import (
    CurrentFeatureSnapshotReference,
    FeatureAttemptState,
    FeatureInputToken,
    FeatureSnapshotAttempt,
)
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import (
    BuildTrainingDatasetRequest,
    HeadCounts,
    HeadRows,
    ModelSourceReference,
    PartitionCounts,
    TrainingDatasetSummary,
    ValidatedTrainingDataset,
)
from nba_live_totals.application.modeling.worker_models import MAX_JSON_BYTES
from nba_live_totals.domain.analytical_lineage import PbpLineage
from nba_live_totals.domain.baseline_hashing import (
    baseline_config_projection,
    baseline_config_sha256,
)
from nba_live_totals.domain.baselines import (
    BaselineConfig,
    BaselineMethod,
    BaselineMetric,
    BaselineUnavailableReason,
)
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    CanonicalValue,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.features import FeatureResultStatus, FeatureVersions
from nba_live_totals.domain.games import Game, GameStatus, SeasonType
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupStatus, LineupVersions
from nba_live_totals.domain.model_features import (
    FeatureColumn,
    FeatureProjectionDescriptor,
    ModelFeatureVector,
    model_feature_descriptor,
)
from nba_live_totals.domain.model_targets import HeadTarget, ModelHead, RemainingTargets
from nba_live_totals.domain.model_training import (
    HeadEligibility,
    TemporalPartition,
    TemporalSplitConfig,
    TrainingBaseline,
    TrainingRow,
    classify_game,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, PossessionVersions


def _object(value: CanonicalValue, keys: Iterable[str] | None = None) -> CanonicalObject:
    if type(value) is not dict:
        raise ValueError("expected a canonical object")
    if keys is not None and set(value) != set(keys):
        raise ValueError("canonical object has missing or extra fields")
    return value


def _list(value: CanonicalValue) -> list[CanonicalValue]:
    if type(value) is not list:
        raise ValueError("expected a canonical array")
    return value


def _str(value: CanonicalValue) -> str:
    if type(value) is not str:
        raise ValueError("expected exact text")
    return value


def _int(value: CanonicalValue) -> int:
    if type(value) is not int:
        raise ValueError("expected exact integer")
    return value


def _bool(value: CanonicalValue) -> bool:
    if type(value) is not bool:
        raise ValueError("expected exact boolean")
    return value


def _optional_int(value: CanonicalValue) -> int | None:
    return None if value is None else _int(value)


def _optional_str(value: CanonicalValue) -> str | None:
    return None if value is None else _str(value)


def _datetime(value: CanonicalValue) -> datetime:
    result = datetime.fromisoformat(_str(value))
    if result.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return result


def versions_projection(
    value: FeatureVersions | PossessionVersions | LineupVersions,
) -> CanonicalObject:
    keys: tuple[str, ...]
    if isinstance(value, FeatureVersions):
        keys = (
            "join_version",
            "formula_version",
            "event_normalizer_version",
            "output_schema_version",
            "player_output_schema_version",
        )
    elif isinstance(value, PossessionVersions):
        keys = ("normalizer_version", "parser_version", "rules_version", "output_schema_version")
    else:
        keys = (
            "normalizer_version",
            "reconstructor_version",
            "rules_version",
            "output_schema_version",
        )
    return {key: cast(str, getattr(value, key)) for key in keys}


def _feature_versions(value: CanonicalValue) -> FeatureVersions:
    keys = (
        "join_version",
        "formula_version",
        "event_normalizer_version",
        "output_schema_version",
        "player_output_schema_version",
    )
    record = _object(value, keys)
    return FeatureVersions(*(_str(record[key]) for key in keys))


def _possession_versions(value: CanonicalValue) -> PossessionVersions:
    keys = ("normalizer_version", "parser_version", "rules_version", "output_schema_version")
    record = _object(value, keys)
    return PossessionVersions(*(_str(record[key]) for key in keys))


def _lineup_versions(value: CanonicalValue) -> LineupVersions:
    keys = ("normalizer_version", "reconstructor_version", "rules_version", "output_schema_version")
    record = _object(value, keys)
    return LineupVersions(*(_str(record[key]) for key in keys))


def split_projection(split: TemporalSplitConfig) -> CanonicalObject:
    return {
        "validation_from": split.validation_from.isoformat(),
        "test_from": split.test_from.isoformat(),
        "embargo_days": split.embargo_days,
    }


def _parse_split(value: CanonicalValue) -> TemporalSplitConfig:
    record = _object(value, ("validation_from", "test_from", "embargo_days"))
    return TemporalSplitConfig(
        date.fromisoformat(_str(record["validation_from"])),
        date.fromisoformat(_str(record["test_from"])),
        _int(record["embargo_days"]),
    )


def _game_projection(game: Game) -> CanonicalObject:
    return {
        "game_id": game.game_id.value,
        "season": game.season.value,
        "season_type": game.season_type.value,
        "game_date": game.game_date.isoformat(),
        "started_at": game.started_at.isoformat(),
        "home_team_id": game.home_team_id,
        "away_team_id": game.away_team_id,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "status": game.status.value,
        "arena_name": game.arena_name,
        "arena_city": game.arena_city,
        "arena_state": game.arena_state,
    }


def _parse_game(record: CanonicalObject) -> Game:
    game = Game(
        GameId(_str(record["game_id"])),
        SeasonId(_str(record["season"])),
        SeasonType(_str(record["season_type"])),
        date.fromisoformat(_str(record["game_date"])),
        _datetime(record["started_at"]),
        _int(record["home_team_id"]),
        _int(record["away_team_id"]),
        _optional_int(record["home_score"]),
        _optional_int(record["away_score"]),
        GameStatus(_str(record["status"])),
        _optional_str(record["arena_name"]),
        _optional_str(record["arena_city"]),
        _optional_str(record["arena_state"]),
    )
    if _game_projection(game) != record:
        raise ValueError("noncanonical game metadata")
    return game


_TOKEN_DIGESTS = (
    "game_context_sha256",
    "parse_revision_id",
    "possession_input_token_sha256",
    "possession_output_sha256",
    "lineup_revision_id",
    "lineup_input_token_sha256",
    "lineup_output_sha256",
    "dependency_input_sha256",
    "dependency_revision_sha256",
)


def model_source_projection(source: ModelSourceReference) -> CanonicalObject:
    ref, pbp = source.reference, source.pbp_lineage
    attempt, token = ref.attempt, ref.attempt.input_token
    token_record: CanonicalObject = {
        "game_id": token.game_id.value,
        "target_started_at": token.target_started_at.isoformat(),
        "possession_snapshot_id": str(token.possession_snapshot_id),
        "lineup_snapshot_id": str(token.lineup_snapshot_id),
        "lineup_result_status": token.lineup_result_status.value,
        "versions": versions_projection(token.versions),
        "sha256": token.sha256,
        "feature_revision_id": token.feature_revision_id,
    }
    token_record.update({key: cast(str, getattr(token, key)) for key in _TOKEN_DIGESTS})
    return {
        "game": _game_projection(source.game),
        "input_token": token_record,
        "snapshot_id": str(attempt.snapshot_id),
        "generation": attempt.generation,
        "feature_revision_id": attempt.feature_revision_id,
        "created_by_run_id": str(attempt.created_by_run_id),
        "created_by_claim_version": attempt.created_by_claim_version,
        "created_at": attempt.created_at.isoformat(),
        "insert_started_at": None
        if attempt.insert_started_at is None
        else attempt.insert_started_at.isoformat(),
        "verified_at": None if attempt.verified_at is None else attempt.verified_at.isoformat(),
        "published_at": None if attempt.published_at is None else attempt.published_at.isoformat(),
        "state_row_count": ref.state_row_count,
        "state_output_sha256": ref.state_output_sha256,
        "player_row_count": ref.player_row_count,
        "player_output_sha256": ref.player_output_sha256,
        "feature_output_sha256": ref.feature_output_sha256,
        "result_status": ref.result_status.value,
        "issue_codes": list(ref.issues),
        "pbp": {
            "document_id": str(pbp.input_pbp_document_id),
            "document_sha256": pbp.input_pbp_sha256,
            "canonical_event_sha256": pbp.canonical_event_sha256,
            "game_context_sha256": pbp.game_context_sha256,
        },
        "possession_versions": versions_projection(source.possession_versions),
        "lineup_versions": versions_projection(source.lineup_versions),
    }


def model_source_semantic_projection(source: ModelSourceReference) -> CanonicalObject:
    ref, token, pbp = source.reference, source.reference.attempt.input_token, source.pbp_lineage
    return {
        "game_id": source.game.game_id.value,
        "season": source.game.season.value,
        "started_at": source.game.started_at.isoformat(),
        "game_context_sha256": pbp.game_context_sha256,
        "pbp_sha256": pbp.input_pbp_sha256,
        "canonical_event_sha256": pbp.canonical_event_sha256,
        "parse_revision_id": token.parse_revision_id,
        "possession_output_sha256": token.possession_output_sha256,
        "possession_versions": versions_projection(source.possession_versions),
        "lineup_revision_id": token.lineup_revision_id,
        "lineup_output_sha256": token.lineup_output_sha256,
        "lineup_result_status": token.lineup_result_status.value,
        "lineup_versions": versions_projection(source.lineup_versions),
        "feature_revision_id": ref.attempt.feature_revision_id,
        "feature_versions": versions_projection(token.versions),
        "state_row_count": ref.state_row_count,
        "state_output_sha256": ref.state_output_sha256,
        "player_row_count": ref.player_row_count,
        "player_output_sha256": ref.player_output_sha256,
        "feature_output_sha256": ref.feature_output_sha256,
        "result_status": ref.result_status.value,
        "issue_codes": list(ref.issues),
    }


def parse_model_source(value: CanonicalValue) -> ModelSourceReference:
    record = _object(value)
    game = _parse_game(_object(record["game"]))
    t = _object(record["input_token"])
    token = FeatureInputToken(
        game.game_id,
        _datetime(t["target_started_at"]),
        _str(t["game_context_sha256"]),
        UUID(_str(t["possession_snapshot_id"])),
        _str(t["parse_revision_id"]),
        _str(t["possession_input_token_sha256"]),
        _str(t["possession_output_sha256"]),
        UUID(_str(t["lineup_snapshot_id"])),
        _str(t["lineup_revision_id"]),
        _str(t["lineup_input_token_sha256"]),
        _str(t["lineup_output_sha256"]),
        LineupStatus(_str(t["lineup_result_status"])),
        _str(t["dependency_input_sha256"]),
        _str(t["dependency_revision_sha256"]),
        _feature_versions(t["versions"]),
    )
    attempt = FeatureSnapshotAttempt(
        game,
        UUID(_str(record["snapshot_id"])),
        _int(record["generation"]),
        _str(record["feature_revision_id"]),
        token,
        FeatureAttemptState.PUBLISHED,
        UUID(_str(record["created_by_run_id"])),
        _int(record["created_by_claim_version"]),
        _datetime(record["created_at"]),
        _int(record["state_row_count"]),
        _str(record["state_output_sha256"]),
        _int(record["player_row_count"]),
        _str(record["player_output_sha256"]),
        _str(record["feature_output_sha256"]),
        tuple(_str(code) for code in _list(record["issue_codes"])),
        FeatureResultStatus(_str(record["result_status"])),
        _datetime(record["insert_started_at"]),
        _datetime(record["verified_at"]),
        _datetime(record["published_at"]),
    )
    ref = CurrentFeatureSnapshotReference(
        attempt,
        _int(record["state_row_count"]),
        _str(record["state_output_sha256"]),
        _int(record["player_row_count"]),
        _str(record["player_output_sha256"]),
        _str(record["feature_output_sha256"]),
    )
    p = _object(record["pbp"])
    source = ModelSourceReference(
        game,
        ref,
        PbpLineage(
            game.game_id,
            UUID(_str(p["document_id"])),
            _str(p["document_sha256"]),
            _str(p["canonical_event_sha256"]),
            _str(p["game_context_sha256"]),
        ),
        _possession_versions(record["possession_versions"]),
        _lineup_versions(record["lineup_versions"]),
    )
    if model_source_projection(source) != record:
        raise ValueError("source metadata or derived identity mismatch")
    return source


def _metric_projection(metric: BaselineMetric) -> CanonicalObject:
    return {
        "value": metric.value,
        "reason": None if metric.reason is None else metric.reason.value,
        "live_weight": metric.live_weight,
    }


def _parse_metric(value: CanonicalValue) -> BaselineMetric:
    m = _object(value, ("value", "reason", "live_weight"))
    return BaselineMetric(
        _optional_int(m["value"]),
        None if m["reason"] is None else BaselineUnavailableReason(_str(m["reason"])),
        _optional_int(m["live_weight"]),
    )


def training_row_projection(row: TrainingRow) -> CanonicalObject:
    targets: CanonicalObject = {}
    eligibility: CanonicalObject = {}
    for head, mask in zip(ModelHead, row.eligibility, strict=True):
        target = cast(HeadTarget, getattr(row.targets, head.value))
        targets[head.value] = {
            "value_scaled": target.value_scaled,
            "unavailable_reason": target.unavailable_reason,
        }
        eligibility[head.value] = {"eligible": mask.eligible, "reason": mask.reason}
    return {
        "record_type": "state",
        "game_id": row.game_id.value,
        "started_at": row.started_at.isoformat(),
        "state_number": row.state_number,
        "boundary": {
            "sequence_number": row.boundary.sequence_number,
            "side": row.boundary.side.value,
        },
        "period": row.period,
        "horizon_period": row.horizon_period,
        "is_valid": row.is_valid,
        "partition": row.partition.value,
        "remaining_time_ms": row.remaining_time_ms,
        "descriptor_sha256": row.vector.descriptor_sha256,
        "feature_values": row.vector.values,
        "targets": targets,
        "eligibility": eligibility,
        "future_home_possessions": row.future_home_possessions,
        "future_away_possessions": row.future_away_possessions,
        "future_home_points": row.future_home_points,
        "future_away_points": row.future_away_points,
        "baselines": [
            {
                "method": b.method.value,
                "pace": _metric_projection(b.pace),
                "home_ppp": _metric_projection(b.home_ppp),
                "away_ppp": _metric_projection(b.away_ppp),
            }
            for b in row.baselines
        ],
    }


def parse_training_row(record: CanonicalObject) -> TrainingRow:
    b = _object(record["boundary"], ("sequence_number", "side"))
    targets, masks = (
        _object(record["targets"], (h.value for h in ModelHead)),
        _object(record["eligibility"], (h.value for h in ModelHead)),
    )
    parsed_targets: list[HeadTarget] = []
    parsed_masks: list[HeadEligibility] = []
    for head in ModelHead:
        t = _object(targets[head.value], ("value_scaled", "unavailable_reason"))
        m = _object(masks[head.value], ("eligible", "reason"))
        parsed_targets.append(
            HeadTarget(_optional_int(t["value_scaled"]), _optional_str(t["unavailable_reason"]))
        )
        parsed_masks.append(HeadEligibility(_bool(m["eligible"]), _optional_str(m["reason"])))
    baselines = []
    for item in _list(record["baselines"]):
        m = _object(item, ("method", "pace", "home_ppp", "away_ppp"))
        baselines.append(
            TrainingBaseline(
                BaselineMethod(_str(m["method"])),
                _parse_metric(m["pace"]),
                _parse_metric(m["home_ppp"]),
                _parse_metric(m["away_ppp"]),
            )
        )
    row = TrainingRow(
        GameId(_str(record["game_id"])),
        _datetime(record["started_at"]),
        _int(record["state_number"]),
        EventBoundary(_int(b["sequence_number"]), BoundarySide(_str(b["side"]))),
        _int(record["period"]),
        _int(record["horizon_period"]),
        _bool(record["is_valid"]),
        TemporalPartition(_str(record["partition"])),
        _int(record["remaining_time_ms"]),
        ModelFeatureVector(
            _str(record["descriptor_sha256"]),
            tuple(_optional_int(v) for v in _list(record["feature_values"])),
        ),
        RemainingTargets(*parsed_targets),
        (parsed_masks[0], parsed_masks[1], parsed_masks[2]),
        _int(record["future_home_possessions"]),
        _int(record["future_away_possessions"]),
        _int(record["future_home_points"]),
        _int(record["future_away_points"]),
        tuple(baselines),
    )
    if canonical_json_bytes(training_row_projection(row)) != canonical_json_bytes(record):
        raise ValueError("training row schema mismatch")
    return row


def _counts_projection(counts: tuple[PartitionCounts, ...]) -> list[CanonicalValue]:
    return [
        {
            "partition": c.partition.value,
            "games": c.games,
            "states": c.states,
            "heads": [
                {
                    "head": h.head.value,
                    "eligible_rows": h.eligible_rows,
                    "eligible_games": h.eligible_games,
                    "has_target_variation": h.has_target_variation,
                    "excluded_counts": dict(h.excluded_counts),
                }
                for h in c.heads
            ],
        }
        for c in counts
    ]


def _parse_counts(value: CanonicalValue) -> tuple[PartitionCounts, ...]:
    counts = []
    for item in _list(value):
        c = _object(item, ("partition", "games", "states", "heads"))
        heads = []
        for h_value in _list(c["heads"]):
            h = _object(
                h_value,
                (
                    "head",
                    "eligible_rows",
                    "eligible_games",
                    "has_target_variation",
                    "excluded_counts",
                ),
            )
            exclusions = _object(h["excluded_counts"])
            heads.append(
                HeadCounts(
                    ModelHead(_str(h["head"])),
                    _int(h["eligible_rows"]),
                    _int(h["eligible_games"]),
                    _bool(h["has_target_variation"]),
                    tuple(sorted((key, _int(n)) for key, n in exclusions.items())),
                )
            )
        counts.append(
            PartitionCounts(
                TemporalPartition(_str(c["partition"])),
                _int(c["games"]),
                _int(c["states"]),
                tuple(heads),
            )
        )
    return tuple(counts)


def dataset_semantic_revision(
    request: BuildTrainingDatasetRequest | TrainingDatasetSummary,
    sources: tuple[ModelSourceReference, ...],
    body_sha256: str,
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": "training-dataset-jsonl-v1",
            "label_version": "remaining-horizon-labels-v1",
            "split_version": "game-temporal-split-v1",
            "selectors": {"seasons": [s.value for s in request.seasons]},
            "split": split_projection(request.split),
            "descriptor": model_feature_descriptor(request.baseline_config).projection(),
            "baseline_config": baseline_config_projection(request.baseline_config),
            "sources": [model_source_semantic_projection(s) for s in sources],
            "semantic_body_sha256": body_sha256,
        }
    )


def summary_projection(summary: TrainingDatasetSummary) -> CanonicalObject:
    return {
        "record_type": "manifest",
        "schema_version": summary.schema_version,
        "label_version": summary.label_version,
        "split_version": summary.split_version,
        "selectors": {"seasons": [s.value for s in summary.seasons]},
        "split": split_projection(summary.split),
        "descriptor": summary.descriptor.projection(),
        "descriptor_sha256": summary.descriptor.sha256,
        "baseline_config": baseline_config_projection(summary.baseline_config),
        "baseline_config_sha256": baseline_config_sha256(summary.baseline_config),
        "feature_versions": versions_projection(summary.feature_versions),
        "possession_versions": versions_projection(summary.possession_versions),
        "lineup_versions": versions_projection(summary.lineup_versions),
        "sources": [model_source_projection(s) for s in summary.sources],
        "counts": _counts_projection(summary.counts),
        "source_sha256": summary.source_sha256,
        "body_sha256": summary.body_sha256,
        "semantic_body_sha256": summary.semantic_body_sha256,
        "dataset_semantic_revision": summary.dataset_semantic_revision,
    }


def parse_summary(record: CanonicalObject, artifact_sha256: str) -> TrainingDatasetSummary:
    selectors, b = _object(record["selectors"], ("seasons",)), _object(record["baseline_config"])
    config = BaselineConfig(
        _int(b["history_window"]),
        _int(b["ppp_prior_possessions"]),
        _int(b["pace_prior_combined_possessions"]),
    )
    d = _object(record["descriptor"], ("version", "columns", "baseline_config_sha256"))
    columns = []
    for item in _list(d["columns"]):
        c = _object(item, ("name", "divisor"))
        columns.append(FeatureColumn(_str(c["name"]), _int(c["divisor"])))
    descriptor = FeatureProjectionDescriptor(
        _str(d["version"]), tuple(columns), _str(d["baseline_config_sha256"])
    )
    summary = TrainingDatasetSummary(
        tuple(SeasonId(_str(s)) for s in _list(selectors["seasons"])),
        descriptor,
        config,
        _parse_split(record["split"]),
        _feature_versions(record["feature_versions"]),
        _possession_versions(record["possession_versions"]),
        _lineup_versions(record["lineup_versions"]),
        tuple(parse_model_source(s) for s in _list(record["sources"])),
        _parse_counts(record["counts"]),
        _str(record["source_sha256"]),
        _str(record["body_sha256"]),
        _str(record["semantic_body_sha256"]),
        _str(record["dataset_semantic_revision"]),
        artifact_sha256,
        _str(record["schema_version"]),
        _str(record["label_version"]),
        _str(record["split_version"]),
    )
    if canonical_json_bytes(summary_projection(summary)) != canonical_json_bytes(record):
        raise ValueError("dataset manifest schema or derived hash mismatch")
    if (
        canonical_json_sha256([model_source_projection(s) for s in summary.sources])
        != summary.source_sha256
    ):
        raise ValueError("dataset source identity mismatch")
    if (
        summary.body_sha256 != summary.semantic_body_sha256
        or dataset_semantic_revision(summary, summary.sources, summary.semantic_body_sha256)
        != summary.dataset_semantic_revision
    ):
        raise ValueError("dataset semantic identity mismatch")
    return summary


@dataclass(slots=True)
class _HeadCounter:
    eligible_rows: int = 0
    games: set[GameId] = field(default_factory=set)
    first_target: int | None = None
    variation: bool = False
    excluded: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _PartitionCounter:
    states: int = 0
    games: set[GameId] = field(default_factory=set)
    heads: tuple[_HeadCounter, ...] = field(
        default_factory=lambda: tuple(_HeadCounter() for _ in ModelHead)
    )


class _Counters:
    def __init__(self) -> None:
        self.partitions = {partition: _PartitionCounter() for partition in TemporalPartition}

    def append(self, row: TrainingRow) -> None:
        part = self.partitions[row.partition]
        part.states += 1
        part.games.add(row.game_id)
        for head, mask, counter in zip(ModelHead, row.eligibility, part.heads, strict=True):
            if not mask.eligible:
                assert mask.reason is not None
                counter.excluded[mask.reason] += 1
                continue
            target = cast(HeadTarget, getattr(row.targets, head.value))
            assert target.value_scaled is not None
            counter.eligible_rows += 1
            counter.games.add(row.game_id)
            if counter.first_target is None:
                counter.first_target = target.value_scaled
            elif target.value_scaled != counter.first_target:
                counter.variation = True

    def snapshot(self) -> tuple[PartitionCounts, ...]:
        return tuple(
            PartitionCounts(
                partition,
                len(part.games),
                part.states,
                tuple(
                    HeadCounts(
                        head,
                        counter.eligible_rows,
                        len(counter.games),
                        counter.variation,
                        tuple(sorted(counter.excluded.items())),
                    )
                    for head, counter in zip(ModelHead, part.heads, strict=True)
                ),
            )
            for partition, part in self.partitions.items()
        )


class _RowValidator:
    def __init__(
        self,
        sources: tuple[ModelSourceReference, ...],
        split: TemporalSplitConfig,
        descriptor: FeatureProjectionDescriptor,
    ) -> None:
        self.sources, self.split, self.descriptor = sources, split, descriptor
        self.source_index = 0
        self.state_index = 0
        self.previous: TrainingRow | None = None
        self.endpoints: dict[int, tuple[int, int, int, int]] = {}
        self.has_invalid = False

    def _finish_game(self) -> None:
        source = self.sources[self.source_index]
        if self.state_index != source.reference.state_row_count or not self.endpoints:
            raise ValueError("missing published feature states")
        endpoint = self.endpoints[max(self.endpoints)]
        if endpoint[:2] != (source.game.home_score, source.game.away_score):
            raise ValueError("final training endpoint score mismatch")
        if self.has_invalid != (source.reference.result_status is FeatureResultStatus.INVALID):
            raise ValueError("training row quality contradicts source snapshot")

    def append(self, row: TrainingRow) -> None:
        if self.source_index >= len(self.sources):
            raise ValueError("extra game rows")
        source = self.sources[self.source_index]
        if self.state_index == source.reference.state_row_count:
            self._finish_game()
            self.source_index += 1
            if self.source_index >= len(self.sources):
                raise ValueError("extra game rows")
            source = self.sources[self.source_index]
            self.state_index, self.previous, self.has_invalid = 0, None, False
            self.endpoints = {}
        if (
            row.game_id != source.game.game_id
            or row.started_at != source.game.started_at
            or row.state_number != self.state_index
            or row.partition is not classify_game(row.started_at, self.split)
            or row.vector.descriptor_sha256 != self.descriptor.sha256
        ):
            raise ValueError("row source, order, partition or descriptor mismatch")
        values = row.vector.values
        p, remaining, period_clock, hs, aws, diff, hn, an = (_int(v) for v in values[:8])
        if (
            min(p, remaining, period_clock, hs, aws, hn, an) < 0
            or hs - aws != diff
            or hn + an != row.state_number
        ):
            raise ValueError("invalid projected state scalars")
        if row.state_number == 0:
            if (p, remaining, period_clock, hs, aws, hn, an, row.boundary.side) != (
                0,
                2_880_000,
                720_000,
                0,
                0,
                0,
                0,
                BoundarySide.BEFORE,
            ):
                raise ValueError("invalid pregame row")
        else:
            if (
                p < 1
                or period_clock > (720_000 if p <= 4 else 300_000)
                or remaining != period_clock + max(4 - p, 0) * 720_000
                or row.boundary.side is not BoundarySide.AFTER
            ):
                raise ValueError("invalid projected possession clocks")
            assert self.previous is not None
            previous = self.previous
            old = previous.vector.values
            ohs, oaws, ohn, oan = (_int(v) for v in (old[3], old[4], old[6], old[7]))
            if (
                row.boundary <= previous.boundary
                or p < previous.period
                or hs < ohs
                or aws < oaws
                or (hn - ohn, an - oan) not in ((1, 0), (0, 1))
            ):
                raise ValueError("noncausal projected state chain")
            if p == previous.period and period_clock > _int(old[2]):
                raise ValueError("projected clock advanced backwards")
        endpoint = (
            hs + row.future_home_points,
            aws + row.future_away_points,
            hn + row.future_home_possessions,
            an + row.future_away_possessions,
        )
        if row.horizon_period in self.endpoints and self.endpoints[row.horizon_period] != endpoint:
            raise ValueError("inconsistent labels within a known horizon")
        self.endpoints[row.horizon_period] = endpoint
        for method_index, baseline in enumerate(row.baselines):
            for head_index, head in enumerate(ModelHead):
                metric = cast(BaselineMetric, getattr(baseline, head.value))
                if metric.value != values[84 + method_index * 3 + head_index]:
                    raise ValueError("baseline output contradicts model projection")
                if method_index == 2 and metric.live_weight != values[93 + head_index]:
                    raise ValueError("baseline weight contradicts model projection")
                if (
                    not row.is_valid
                    and metric.reason is not BaselineUnavailableReason.INVALID_FEATURE_STATE
                ):
                    raise ValueError("invalid state has an available baseline")
        self.has_invalid |= not row.is_valid
        self.state_index += 1
        self.previous = row

    def finish(self) -> None:
        if self.source_index != len(self.sources) - 1:
            raise ValueError("missing source games")
        self._finish_game()


def _read_record(stream: BinaryIO) -> tuple[CanonicalObject, bytes] | None:
    # File objects stay owned by the caller. A line can never allocate unbounded memory.
    content = stream.readline(MAX_JSON_BYTES + 1)
    if not content:
        return None
    if len(content) > MAX_JSON_BYTES or not content.endswith(b"\n"):
        raise ValueError("oversized or unterminated canonical JSONL record")
    record = _object(cast(CanonicalValue, json.loads(content)))
    if canonical_json_bytes(record) + b"\n" != content:
        raise ValueError("noncanonical JSONL record")
    return record, content


class CanonicalTrainingDatasetReader:
    def validate(self, path: Path) -> ValidatedTrainingDataset:
        try:
            path = path.resolve()
            body_hash, artifact_hash, counters = hashlib.sha256(), hashlib.sha256(), _Counters()
            with path.open("rb") as stream:
                header = _read_record(stream)
                if header is None:
                    raise ValueError("empty training dataset")
                record, content = header
                summary = parse_summary(record, "0" * 64)
                artifact_hash.update(content)
                validator = _RowValidator(summary.sources, summary.split, summary.descriptor)
                while item := _read_record(stream):
                    record, content = item
                    row = parse_training_row(record)
                    validator.append(row)
                    counters.append(row)
                    body_hash.update(content)
                    artifact_hash.update(content)
                validator.finish()
            if (
                counters.snapshot() != summary.counts
                or body_hash.hexdigest() != summary.body_sha256
                or body_hash.hexdigest() != summary.semantic_body_sha256
            ):
                raise ValueError("dataset counts or body checksum mismatch")
            if (
                canonical_json_sha256([model_source_projection(s) for s in summary.sources])
                != summary.source_sha256
            ):
                raise ValueError("source manifest checksum mismatch")
            request = BuildTrainingDatasetRequest(
                summary.seasons, path, summary.split, summary.baseline_config
            )
            if (
                dataset_semantic_revision(request, summary.sources, body_hash.hexdigest())
                != summary.dataset_semantic_revision
            ):
                raise ValueError("dataset semantic revision mismatch")
            return ValidatedTrainingDataset(
                path, replace(summary, dataset_artifact_sha256=artifact_hash.hexdigest())
            )
        except OSError:
            raise ModelingError("training_dataset_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, OverflowError:
            raise ModelingError("training_dataset_invalid") from None

    def iter_rows(
        self, dataset: ValidatedTrainingDataset, partition: TemporalPartition | None = None
    ) -> Iterator[TrainingRow]:
        try:
            checksum = hashlib.sha256()
            with dataset.path.open("rb") as stream:
                header = _read_record(stream)
                if header is None or header[0] != summary_projection(dataset.summary):
                    raise ValueError("checked dataset header changed")
                checksum.update(header[1])
                while item := _read_record(stream):
                    row = parse_training_row(item[0])
                    checksum.update(item[1])
                    if partition is None or row.partition is partition:
                        yield row
            if checksum.hexdigest() != dataset.summary.dataset_artifact_sha256:
                raise ValueError("checked dataset bytes changed")
        except OSError:
            raise ModelingError("training_dataset_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, OverflowError:
            raise ModelingError("training_dataset_invalid") from None

    def head_rows(
        self, dataset: ValidatedTrainingDataset, partition: TemporalPartition, head: ModelHead
    ) -> HeadRows:
        if (
            not isinstance(partition, TemporalPartition)
            or partition is TemporalPartition.EMBARGO
            or not isinstance(head, ModelHead)
        ):
            raise ValueError("invalid head row selector")
        index = tuple(ModelHead).index(head)
        game_counts: Counter[GameId] = Counter()
        for row in self.iter_rows(dataset, partition):
            if row.eligibility[index].eligible:
                game_counts[row.game_id] += 1

        def iterate() -> Iterator[TrainingRow]:
            for row in self.iter_rows(dataset, partition):
                if row.eligibility[index].eligible:
                    yield row

        return HeadRows(partition, head, sum(game_counts.values()), game_counts, iterate)


class AtomicTrainingDatasetExporter:
    def __init__(self) -> None:
        self._request: BuildTrainingDatasetRequest | None = None
        self._body: Path | None = None
        self._ready: Path | None = None
        self._worker: asyncio.Task[None] | None = None
        self._sources: list[ModelSourceReference] = []
        self._counts = _Counters()
        self._body_hash = hashlib.sha256()
        self._summary: TrainingDatasetSummary | None = None
        self._published = False

    async def _io(self, operation: Callable[[], None]) -> None:
        if self._worker is not None:
            raise ModelingError("training_dataset_busy")
        worker = asyncio.create_task(asyncio.to_thread(operation))
        self._worker = worker
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        except ModelingError:
            raise
        except OSError:
            raise ModelingError("training_dataset_io_error") from None
        except ValueError, TypeError, KeyError, IndexError, OverflowError:
            raise ModelingError("training_dataset_invalid") from None
        finally:
            self._worker = None

    def _temp(self) -> Path:
        assert self._request is not None
        fd, name = tempfile.mkstemp(
            prefix=".training-", suffix=".tmp", dir=self._request.output_path.parent
        )
        os.close(fd)
        return Path(name)

    async def begin(self, request: BuildTrainingDatasetRequest) -> None:
        if self._request is not None:
            raise ModelingError("training_dataset_already_started")
        self._request = request
        await self._io(self._begin)

    def _begin(self) -> None:
        self._body = self._temp()
        self._ready = self._temp()

    async def append_game(self, source: ModelSourceReference, rows: Iterable[TrainingRow]) -> None:
        if (
            self._request is None
            or self._body is None
            or self._summary is not None
            or self._published
        ):
            raise ModelingError("training_dataset_not_writing")
        await self._io(lambda: self._append(source, rows))

    def _append(self, source: ModelSourceReference, rows: Iterable[TrainingRow]) -> None:
        assert self._request is not None and self._body is not None
        if source.game.season not in self._request.seasons:
            raise ValueError("source outside selected seasons")
        if self._sources and (source.game.started_at, source.game.game_id.value) <= (
            self._sources[-1].game.started_at,
            self._sources[-1].game.game_id.value,
        ):
            raise ValueError("nonchronological dataset source")
        if any(s.game.game_id == source.game.game_id for s in self._sources):
            raise ValueError("duplicate dataset source")
        descriptor = model_feature_descriptor(self._request.baseline_config)
        validator = _RowValidator((source,), self._request.split, descriptor)
        with self._body.open("ab") as output:
            for row in rows:
                validator.append(row)
                self._counts.append(row)
                content = canonical_json_bytes(training_row_projection(row)) + b"\n"
                output.write(content)
                self._body_hash.update(content)
            validator.finish()
        self._sources.append(source)

    async def prepare(self, sources: tuple[ModelSourceReference, ...]) -> TrainingDatasetSummary:
        if not sources or tuple(self._sources) != sources or self._summary is not None:
            raise ModelingError("training_dataset_source_mismatch")
        await self._io(self._prepare)
        assert self._summary is not None
        return self._summary

    def _prepare(self) -> None:
        assert self._request is not None and self._ready is not None and self._body is not None
        sources = tuple(self._sources)
        first = sources[0]
        summary = TrainingDatasetSummary(
            self._request.seasons,
            model_feature_descriptor(self._request.baseline_config),
            self._request.baseline_config,
            self._request.split,
            first.reference.attempt.input_token.versions,
            first.possession_versions,
            first.lineup_versions,
            sources,
            self._counts.snapshot(),
            canonical_json_sha256([model_source_projection(s) for s in sources]),
            self._body_hash.hexdigest(),
            self._body_hash.hexdigest(),
            dataset_semantic_revision(self._request, sources, self._body_hash.hexdigest()),
            "0" * 64,
        )
        with self._ready.open("wb") as output, self._body.open("rb") as body:
            header = canonical_json_bytes(summary_projection(summary)) + b"\n"
            if len(header) > MAX_JSON_BYTES:
                raise ValueError("source manifest exceeds bounded JSON record size")
            output.write(header)
            while block := body.read(1_048_576):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        checked = CanonicalTrainingDatasetReader().validate(self._ready)
        if replace(checked.summary, dataset_artifact_sha256="0" * 64) != summary:
            raise ValueError("prepared dataset readback mismatch")
        self._summary = checked.summary

    def publish(self) -> None:
        if self._request is None or self._ready is None or self._summary is None or self._published:
            raise ModelingError("training_dataset_not_prepared")
        try:
            os.replace(self._ready, self._request.output_path)
        except OSError:
            raise ModelingError("training_dataset_io_error") from None
        self._published = True

    async def abort(self) -> None:
        await self._io(self._cleanup)

    def _cleanup(self) -> None:
        for path in (self._body, self._ready):
            if path is not None:
                path.unlink(missing_ok=True)
