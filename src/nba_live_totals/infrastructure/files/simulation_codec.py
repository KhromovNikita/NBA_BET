"""Strict typed projections for canonical simulation manifests and individual rows."""

from collections.abc import Iterable

from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulationCounters,
    SimulationManifest,
    SimulationModelIdentity,
    SimulationSourceSelection,
)
from nba_live_totals.domain.baselines import BaselineConfig
from nba_live_totals.domain.canonical import (
    CanonicalObject,
    CanonicalValue,
    canonical_json_bytes,
    canonical_json_sha256,
)
from nba_live_totals.domain.features import FeatureStateKind, FeatureVersions
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.lineups import LineupVersions
from nba_live_totals.domain.model_features import (
    FeatureColumn,
    FeatureProjectionDescriptor,
    model_feature_descriptor,
)
from nba_live_totals.domain.model_predictions import PredictedRate
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary, PossessionVersions
from nba_live_totals.domain.simulation import (
    SIMULATION_ENGINE_VERSION,
    SIMULATION_POLICY_VERSION,
    SIMULATION_SEED_POLICY_VERSION,
    SIMULATION_SUMMARY_POLICY_VERSION,
    Histogram,
    SimulationConfig,
    SimulationFrame,
    SimulationOutcome,
    SimulationQuery,
    SimulationRuntime,
    SimulationStatus,
)
from nba_live_totals.domain.simulation_hashing import (
    SIMULATION_SCHEMA_VERSION,
    simulation_config_projection,
    simulation_config_sha256,
    simulation_frame_projection,
    simulation_query_projection,
    simulation_query_sha256,
    simulation_revision_sha256,
    simulation_run_revision,
    simulation_runtime_projection,
    state_seed_sha256,
)
from nba_live_totals.domain.simulation_pmf import SCORE_PMF_POLICY_VERSION, ScorePmf
from nba_live_totals.domain.simulation_summary import (
    summarize_histogram,
    summarize_lines,
    validate_simulation_outcome,
)
from nba_live_totals.infrastructure.files.training_dataset import (
    model_source_projection,
    model_source_semantic_projection,
    parse_model_source,
    versions_projection,
)


def _checked(record: CanonicalObject, *, manifest: bool = False) -> CanonicalObject:
    def visit(value: CanonicalValue, path: tuple[str, ...]) -> None:
        if type(value) is int:
            lower, upper = (
                (0, 2**64 - 1) if manifest and path == ("config", "seed") else (-(2**63), 2**63 - 1)
            )
            if not lower <= value <= upper:
                raise ValueError(
                    "simulation numeric value exceeds Int64 schema (except UInt64 seed)"
                )
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*path, key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, (*path, "[]"))

    visit(record, ())
    return record


def _object(value: CanonicalValue, keys: Iterable[str]) -> CanonicalObject:
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("simulation object has missing, unknown or invalid fields")
    return value


def _array(value: CanonicalValue) -> list[CanonicalValue]:
    if type(value) is not list:
        raise ValueError("simulation requires an array")
    return value


def _text(value: CanonicalValue) -> str:
    if type(value) is not str:
        raise ValueError("simulation requires exact text")
    return value


def _int(value: CanonicalValue) -> int:
    if type(value) is not int:
        raise ValueError("simulation requires exact integer")
    return value


def _bool(value: CanonicalValue) -> bool:
    if type(value) is not bool:
        raise ValueError("simulation requires exact boolean")
    return value


def _optional_int(value: CanonicalValue) -> int | None:
    return None if value is None else _int(value)


def _optional_text(value: CanonicalValue) -> str | None:
    return None if value is None else _text(value)


def _same(record: CanonicalObject, expected: CanonicalObject) -> None:
    try:
        matches = canonical_json_bytes(record) == canonical_json_bytes(expected)
    except TypeError:
        raise ValueError("simulation projection contains unsupported scalar types") from None
    if not matches:
        raise ValueError("simulation projection contradicts derived values")


def parse_simulation_config(value: CanonicalValue) -> SimulationConfig:
    r = _object(
        value,
        (
            "policy_version",
            "simulations",
            "seed",
            "batch_size",
            "max_overtimes",
            "max_possessions_per_horizon",
            "score_prior_weights",
        ),
    )
    return SimulationConfig(
        _int(r["simulations"]),
        _int(r["seed"]),
        _int(r["batch_size"]),
        _int(r["max_overtimes"]),
        _int(r["max_possessions_per_horizon"]),
        tuple(_int(x) for x in _array(r["score_prior_weights"])),
        _text(r["policy_version"]),
    )


def _query(value: CanonicalValue) -> SimulationQuery:
    r = _object(value, ("policy_version", "lines_scaled", "quantiles_scaled"))
    return SimulationQuery(
        tuple(_int(x) for x in _array(r["lines_scaled"])),
        tuple(_int(x) for x in _array(r["quantiles_scaled"])),
        _text(r["policy_version"]),
    )


def _runtime(value: CanonicalValue) -> SimulationRuntime:
    keys = ("python_version", "numpy_version", "system", "machine", "engine_version")
    r = _object(value, keys)
    return SimulationRuntime(*(_text(r[key]) for key in keys))


def _feature_versions(value: CanonicalValue) -> FeatureVersions:
    keys = (
        "join_version",
        "formula_version",
        "event_normalizer_version",
        "output_schema_version",
        "player_output_schema_version",
    )
    r = _object(value, keys)
    return FeatureVersions(*(_text(r[key]) for key in keys))


def _possession_versions(value: CanonicalValue) -> PossessionVersions:
    keys = ("normalizer_version", "parser_version", "rules_version", "output_schema_version")
    r = _object(value, keys)
    return PossessionVersions(*(_text(r[key]) for key in keys))


def _lineup_versions(value: CanonicalValue) -> LineupVersions:
    keys = ("normalizer_version", "reconstructor_version", "rules_version", "output_schema_version")
    r = _object(value, keys)
    return LineupVersions(*(_text(r[key]) for key in keys))


def _descriptor(value: CanonicalValue) -> FeatureProjectionDescriptor:
    r = _object(value, ("version", "columns", "baseline_config_sha256"))
    columns: list[FeatureColumn] = []
    for item in _array(r["columns"]):
        column = _object(item, ("name", "divisor"))
        columns.append(FeatureColumn(_text(column["name"]), _int(column["divisor"])))
    result = FeatureProjectionDescriptor(
        _text(r["version"]), tuple(columns), _text(r["baseline_config_sha256"])
    )
    if result.columns != model_feature_descriptor(BaselineConfig()).columns:
        raise ValueError("simulation descriptor contradicts Phase7 fixed projection")
    return result


def simulation_selectors_projection(
    seasons: tuple[SeasonId, ...], game_id: GameId | None, state_number: int | None
) -> CanonicalObject:
    return {
        "seasons": [s.value for s in seasons],
        "game_id": None if game_id is None else game_id.value,
        "state_number": state_number,
    }


def simulation_sources_projection(
    sources: tuple[SimulationSourceSelection, ...], *, semantic: bool = False
) -> list[CanonicalValue]:
    result: list[CanonicalValue] = []
    for item in sources:
        result.append(
            {
                "source": model_source_semantic_projection(item.source)
                if semantic
                else model_source_projection(item.source),
                "selection": {
                    "total_states": item.selection.total_states,
                    "state_number": item.selection.state_number,
                },
            }
        )
    return result


def _reason_counts(counts: tuple[tuple[str, int], ...]) -> list[CanonicalValue]:
    return [{"reason": reason, "count": count} for reason, count in counts]


def _parse_reason_counts(value: CanonicalValue) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for item in _array(value):
        r = _object(item, ("reason", "count"))
        result.append((_text(r["reason"]), _int(r["count"])))
    return tuple(result)


def _model_projection(model: SimulationModelIdentity) -> CanonicalObject:
    return {
        "recipe_sha256": model.recipe_sha256,
        "artifact_sha256": model.artifact_sha256,
        "descriptor": model.descriptor.projection(),
        "descriptor_sha256": model.descriptor.sha256,
        "feature_versions": versions_projection(model.feature_versions),
        "possession_versions": versions_projection(model.possession_versions),
        "lineup_versions": versions_projection(model.lineup_versions),
    }


def simulation_manifest_projection(manifest: SimulationManifest) -> CanonicalObject:
    counts = manifest.counts
    return _checked(
        {
            "record_type": "manifest",
            "schema_version": manifest.schema_version,
            "policy_versions": {
                "simulation": SIMULATION_POLICY_VERSION,
                "score_pmf": SCORE_PMF_POLICY_VERSION,
                "engine": SIMULATION_ENGINE_VERSION,
                "seed": SIMULATION_SEED_POLICY_VERSION,
                "summary": SIMULATION_SUMMARY_POLICY_VERSION,
                "jsonl": SIMULATION_SCHEMA_VERSION,
            },
            "selectors": simulation_selectors_projection(
                manifest.seasons, manifest.game_id, manifest.state_number
            ),
            "config": simulation_config_projection(manifest.config),
            "config_sha256": simulation_config_sha256(manifest.config),
            "query": simulation_query_projection(manifest.query),
            "query_sha256": simulation_query_sha256(manifest.query),
            "model": _model_projection(manifest.model),
            "runtime": simulation_runtime_projection(manifest.runtime),
            "sources": simulation_sources_projection(manifest.sources),
            "counts": {
                "games": counts.games,
                "states": counts.states,
                "complete": counts.complete,
                "incomplete": counts.incomplete,
                "unavailable": counts.unavailable,
                "unresolved": counts.unresolved,
                "unresolved_counts": _reason_counts(counts.unresolved_counts),
            },
            "source_sha256": manifest.source_sha256,
            "body_sha256": manifest.body_sha256,
            "semantic_body_sha256": manifest.semantic_body_sha256,
            "run_semantic_revision": manifest.run_semantic_revision,
        },
        manifest=True,
    )


def parse_simulation_manifest(record: CanonicalObject) -> SimulationManifest:
    _checked(record, manifest=True)
    r = _object(
        record,
        (
            "record_type",
            "schema_version",
            "policy_versions",
            "selectors",
            "config",
            "config_sha256",
            "query",
            "query_sha256",
            "model",
            "runtime",
            "sources",
            "counts",
            "source_sha256",
            "body_sha256",
            "semantic_body_sha256",
            "run_semantic_revision",
        ),
    )
    selectors = _object(r["selectors"], ("seasons", "game_id", "state_number"))
    seasons = tuple(SeasonId(_text(x)) for x in _array(selectors["seasons"]))
    game = None if selectors["game_id"] is None else GameId(_text(selectors["game_id"]))
    state = _optional_int(selectors["state_number"])
    model = _object(
        r["model"],
        (
            "recipe_sha256",
            "artifact_sha256",
            "descriptor",
            "descriptor_sha256",
            "feature_versions",
            "possession_versions",
            "lineup_versions",
        ),
    )
    identity = SimulationModelIdentity(
        _text(model["recipe_sha256"]),
        _text(model["artifact_sha256"]),
        _descriptor(model["descriptor"]),
        _feature_versions(model["feature_versions"]),
        _possession_versions(model["possession_versions"]),
        _lineup_versions(model["lineup_versions"]),
    )
    sources: list[SimulationSourceSelection] = []
    for item in _array(r["sources"]):
        source = _object(item, ("source", "selection"))
        selection = _object(source["selection"], ("total_states", "state_number"))
        sources.append(
            SimulationSourceSelection(
                parse_model_source(source["source"]),
                SelectedSimulationStates(
                    _int(selection["total_states"]), _optional_int(selection["state_number"])
                ),
            )
        )
    counts = _object(
        r["counts"],
        (
            "games",
            "states",
            "complete",
            "incomplete",
            "unavailable",
            "unresolved",
            "unresolved_counts",
        ),
    )
    counters = SimulationCounters(
        _int(counts["games"]),
        _int(counts["states"]),
        _int(counts["complete"]),
        _int(counts["incomplete"]),
        _int(counts["unavailable"]),
        _int(counts["unresolved"]),
        _parse_reason_counts(counts["unresolved_counts"]),
    )
    manifest = SimulationManifest(
        seasons,
        game,
        state,
        parse_simulation_config(r["config"]),
        _query(r["query"]),
        identity,
        _runtime(r["runtime"]),
        tuple(sources),
        counters,
        _text(r["source_sha256"]),
        _text(r["body_sha256"]),
        _text(r["semantic_body_sha256"]),
        _text(r["run_semantic_revision"]),
        _text(r["schema_version"]),
    )
    if manifest.source_sha256 != canonical_json_sha256(
        simulation_sources_projection(manifest.sources)
    ):
        raise ValueError("simulation source hash mismatch")
    semantic = tuple(
        _object(x, ("source", "selection"))
        for x in simulation_sources_projection(manifest.sources, semantic=True)
    )
    revision = simulation_run_revision(
        selectors,
        manifest.config,
        manifest.query,
        manifest.runtime,
        semantic,
        manifest.semantic_body_sha256,
    )
    if manifest.run_semantic_revision != revision:
        raise ValueError("simulation run semantic revision mismatch")
    _same(record, simulation_manifest_projection(manifest))
    return manifest


def _pmf_projection(pmf: ScorePmf | None) -> CanonicalValue:
    return (
        None
        if pmf is None
        else {
            "policy_version": pmf.policy_version,
            "counts": list(pmf.counts),
            "target_scaled": pmf.target_scaled,
        }
    )


def _pmf(value: CanonicalValue) -> ScorePmf | None:
    if value is None:
        return None
    r = _object(value, ("policy_version", "counts", "target_scaled"))
    return ScorePmf(
        tuple(_int(x) for x in _array(r["counts"])),
        _int(r["target_scaled"]),
        _text(r["policy_version"]),
    )


def _histogram(value: CanonicalValue) -> Histogram | None:
    if value is None:
        return None
    result: list[tuple[int, int]] = []
    for pair in _array(value):
        row = _array(pair)
        if len(row) != 2:
            raise ValueError("histogram requires score/count pairs")
        result.append((_int(row[0]), _int(row[1])))
    return tuple(result)


def _histogram_projection(histogram: Histogram | None) -> CanonicalValue:
    return None if histogram is None else [[score, count] for score, count in histogram]


def _source_row_projection(source: ModelSourceReference) -> CanonicalObject:
    ref = source.reference
    return {
        "game_id": source.game.game_id.value,
        "feature_snapshot_id": str(ref.attempt.snapshot_id),
        "input_token_sha256": ref.attempt.input_token.sha256,
        "state_output_sha256": ref.state_output_sha256,
        "player_output_sha256": ref.player_output_sha256,
    }


def simulation_row_projection(
    source: ModelSourceReference, outcome: SimulationOutcome, query: SimulationQuery
) -> CanonicalObject:
    if (
        source.game.game_id != outcome.frame.game_id
        or outcome.frame.state_number >= source.reference.state_row_count
    ):
        raise ValueError("simulation row/source identity mismatch")
    summaries: CanonicalObject | None = None
    if outcome.status is SimulationStatus.COMPLETE:
        summaries = {}
        for name, histogram in (
            ("home", outcome.home_histogram),
            ("away", outcome.away_histogram),
            ("total", outcome.total_histogram),
        ):
            if histogram is None:
                raise ValueError("complete outcome requires a histogram")
            summary = summarize_histogram(histogram, query.quantiles_scaled)
            summaries[name] = {
                "mean_scaled": summary.mean_scaled,
                "stdev_scaled": summary.stdev_scaled,
                "quantiles": [[q, score] for q, score in summary.quantiles],
            }
    lines: list[CanonicalValue] = []
    for row in summarize_lines(outcome, query):
        lines.append(
            {
                "line_scaled": row.line_scaled,
                "count_under": row.count_under,
                "count_equal": row.count_equal,
                "count_over": row.count_over,
                "count_unknown": row.count_unknown,
                "prob_under_scaled": row.prob_under_scaled,
                "prob_equal_scaled": row.prob_equal_scaled,
                "prob_over_scaled": row.prob_over_scaled,
            }
        )
    return _checked(
        {
            "record_type": "simulation",
            "source": _source_row_projection(source),
            "frame": simulation_frame_projection(outcome.frame),
            "population": {
                "status": outcome.status.value,
                "requested": outcome.requested,
                "executed": outcome.executed,
                "resolved": outcome.resolved,
                "unresolved": outcome.unresolved,
                "unavailable_reason": outcome.unavailable_reason,
                "unresolved_counts": _reason_counts(outcome.unresolved_counts),
                "new_ot_counts": list(outcome.new_ot_counts),
                "home_pmf": _pmf_projection(outcome.home_pmf),
                "away_pmf": _pmf_projection(outcome.away_pmf),
                "seed_sha256": outcome.seed_sha256,
                "simulation_revision_sha256": outcome.simulation_revision_sha256,
                "home_histogram": _histogram_projection(outcome.home_histogram),
                "away_histogram": _histogram_projection(outcome.away_histogram),
                "total_histogram": _histogram_projection(outcome.total_histogram),
            },
            "summaries": summaries,
            "lines": lines,
        }
    )


def simulation_row_semantic_projection(
    source: ModelSourceReference, outcome: SimulationOutcome, query: SimulationQuery
) -> CanonicalObject:
    record = simulation_row_projection(source, outcome, query)
    record["source"] = model_source_semantic_projection(source)
    return _checked(record)


def _rate(value: CanonicalValue) -> PredictedRate:
    r = _object(value, ("value_scaled", "unavailable_reason", "was_clipped_to_zero"))
    return PredictedRate(
        _optional_int(r["value_scaled"]),
        _optional_text(r["unavailable_reason"]),
        _bool(r["was_clipped_to_zero"]),
    )


def _frame(value: CanonicalValue) -> SimulationFrame:
    keys = (
        "game_id",
        "state_number",
        "possession_number",
        "state_kind",
        "boundary",
        "period",
        "seconds_remaining_game_ms",
        "seconds_remaining_period_ms",
        "home_score",
        "away_score",
        "is_valid",
        "pace",
        "home_ppp",
        "away_ppp",
        "input_sha256",
        "model_revision_sha256",
        "model_artifact_sha256",
        "unavailable_reason",
    )
    r = _object(value, keys)
    boundary = _object(r["boundary"], ("sequence_number", "side"))
    return SimulationFrame(
        GameId(_text(r["game_id"])),
        _int(r["state_number"]),
        _int(r["possession_number"]),
        FeatureStateKind(_text(r["state_kind"])),
        EventBoundary(_int(boundary["sequence_number"]), BoundarySide(_text(boundary["side"]))),
        _int(r["period"]),
        _int(r["seconds_remaining_game_ms"]),
        _int(r["seconds_remaining_period_ms"]),
        _int(r["home_score"]),
        _int(r["away_score"]),
        _bool(r["is_valid"]),
        _rate(r["pace"]),
        _rate(r["home_ppp"]),
        _rate(r["away_ppp"]),
        _text(r["input_sha256"]),
        _text(r["model_revision_sha256"]),
        _text(r["model_artifact_sha256"]),
        _optional_text(r["unavailable_reason"]),
    )


def parse_simulation_row(
    record: CanonicalObject, manifest: SimulationManifest
) -> SimulationOutcome:
    _checked(record)
    r = _object(record, ("record_type", "source", "frame", "population", "summaries", "lines"))
    source_record = _object(
        r["source"],
        (
            "game_id",
            "feature_snapshot_id",
            "input_token_sha256",
            "state_output_sha256",
            "player_output_sha256",
        ),
    )
    game_id = GameId(_text(source_record["game_id"]))
    selected = next((s for s in manifest.sources if s.source.game.game_id == game_id), None)
    if selected is None:
        raise ValueError("simulation row references an unselected game")
    _same(source_record, _source_row_projection(selected.source))
    frame = _frame(r["frame"])
    if (
        frame.game_id != game_id
        or frame.state_number not in selected.selection.numbers
        or frame.model_revision_sha256 != manifest.model.recipe_sha256
        or frame.model_artifact_sha256 != manifest.model.artifact_sha256
    ):
        raise ValueError("simulation frame/source/model identity mismatch")
    p = _object(
        r["population"],
        (
            "status",
            "requested",
            "executed",
            "resolved",
            "unresolved",
            "unavailable_reason",
            "unresolved_counts",
            "new_ot_counts",
            "home_pmf",
            "away_pmf",
            "seed_sha256",
            "simulation_revision_sha256",
            "home_histogram",
            "away_histogram",
            "total_histogram",
        ),
    )
    outcome = SimulationOutcome(
        frame,
        SimulationStatus(_text(p["status"])),
        _int(p["requested"]),
        _int(p["executed"]),
        _int(p["resolved"]),
        _int(p["unresolved"]),
        _optional_text(p["unavailable_reason"]),
        _parse_reason_counts(p["unresolved_counts"]),
        tuple(_int(x) for x in _array(p["new_ot_counts"])),
        _pmf(p["home_pmf"]),
        _pmf(p["away_pmf"]),
        _text(p["seed_sha256"]),
        _text(p["simulation_revision_sha256"]),
        _histogram(p["home_histogram"]),
        _histogram(p["away_histogram"]),
        _histogram(p["total_histogram"]),
    )
    validate_simulation_outcome(outcome, manifest.config)
    if outcome.seed_sha256 != state_seed_sha256(
        frame, manifest.config, manifest.runtime
    ) or outcome.simulation_revision_sha256 != simulation_revision_sha256(
        frame, manifest.config, manifest.runtime
    ):
        raise ValueError("simulation state seed or population identity mismatch")
    _same(record, simulation_row_projection(selected.source, outcome, manifest.query))
    return outcome
