"""Schema and derived-field checks reject plausible but inconsistent result rows."""

from copy import deepcopy
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.application.simulation.models import SimulationManifest
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_sha256
from nba_live_totals.domain.simulation import SimulationQuery, make_simulation_frame
from nba_live_totals.domain.simulation_hashing import simulation_run_revision
from nba_live_totals.domain.simulation_summary import make_unavailable_outcome
from nba_live_totals.infrastructure.files.simulation_codec import (
    parse_simulation_manifest,
    parse_simulation_row,
    simulation_manifest_projection,
    simulation_row_projection,
    simulation_row_semantic_projection,
)
from nba_live_totals.infrastructure.files.training_dataset import (
    model_source_projection,
    model_source_semantic_projection,
)
from tests.unit.application.modeling_fakes import make_model_source_reference
from tests.unit.application.simulation_fakes import make_simulation_manifest
from tests.unit.domain.modeling_helpers import make_training_input
from tests.unit.domain.simulation_helpers import make_simulation_outcome, make_simulation_prediction


def codec_manifest(*, query: SimulationQuery | None = None) -> SimulationManifest:
    base = make_simulation_manifest()
    if query is not None:
        base = replace(base, query=query)
    sources = [
        {
            "source": model_source_projection(s.source),
            "selection": {
                "total_states": s.selection.total_states,
                "state_number": s.selection.state_number,
            },
        }
        for s in base.sources
    ]
    semantic: tuple[CanonicalObject, ...] = tuple(
        {
            "source": model_source_semantic_projection(s.source),
            "selection": {
                "total_states": s.selection.total_states,
                "state_number": s.selection.state_number,
            },
        }
        for s in base.sources
    )
    selectors: CanonicalObject = {"seasons": ["2025-26"], "game_id": None, "state_number": None}
    return replace(
        base,
        source_sha256=canonical_json_sha256(sources),
        run_semantic_revision=simulation_run_revision(
            selectors,
            base.config,
            base.query,
            base.runtime,
            semantic,
            base.semantic_body_sha256,
        ),
    )


def test_manifest_roundtrip_pins_all_versions_and_derived_hashes() -> None:
    manifest = codec_manifest()
    record = simulation_manifest_projection(manifest)
    assert record["record_type"] == "manifest"
    assert record["policy_versions"] == {
        "simulation": "possession-simulation-v1",
        "score_pmf": "tilted-integer-score-pmf-v1",
        "engine": "numpy-poisson-multinomial-v1",
        "seed": "state-pcg64-seed-v1",
        "summary": "exact-simulation-summary-v1",
        "jsonl": "simulation-results-jsonl-v1",
    }
    assert parse_simulation_manifest(record) == manifest
    assert "output_path" not in record and "artifact_sha256" not in record
    assert "config_sha256" in record and "query_sha256" in record


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "schema",
        "unknown",
        "config_hash",
        "query_hash",
        "source_hash",
        "revision",
        "bool",
        "version",
        "descriptor",
    ],
)
def test_manifest_rejects_missing_unknown_and_contradictory_contracts(mutation: str) -> None:
    record = deepcopy(simulation_manifest_projection(codec_manifest()))
    if mutation == "empty":
        record = {}
    elif mutation == "schema":
        record["schema_version"] = "simulation-results-jsonl-v2"
    elif mutation == "unknown":
        record["other"] = None
    elif mutation in {"config_hash", "query_hash", "source_hash"}:
        record[mutation.replace("_hash", "_sha256")] = "a" * 64
    elif mutation == "revision":
        record["run_semantic_revision"] = "a" * 64
    elif mutation == "bool":
        cast(CanonicalObject, record["counts"])["games"] = True
    elif mutation == "version":
        cast(CanonicalObject, record["policy_versions"])["seed"] = "other"
    else:
        model = cast(CanonicalObject, record["model"])
        model["descriptor_sha256"] = "a" * 64
    with pytest.raises(ValueError):
        parse_simulation_manifest(record)


def test_complete_row_has_exact_full_statistics_and_line_counts() -> None:
    manifest = codec_manifest(query=SimulationQuery((3000000, 3500000)))
    outcome = make_simulation_outcome()
    source = manifest.sources[0].source
    record = simulation_row_projection(source, outcome, manifest.query)
    summaries = cast(CanonicalObject, record["summaries"])
    home = cast(CanonicalObject, summaries["home"])
    assert home["mean_scaled"] == 3250000 and home["stdev_scaled"] == 829156
    assert cast(list[CanonicalObject], record["lines"])[0] == {
        "line_scaled": 3000000,
        "count_under": 1,
        "count_equal": 1,
        "count_over": 2,
        "count_unknown": 0,
        "prob_under_scaled": 250000,
        "prob_equal_scaled": 250000,
        "prob_over_scaled": 500000,
    }
    assert parse_simulation_row(record, manifest) == outcome


@pytest.mark.parametrize("kind", ["partial", "all_unresolved", "unavailable"])
def test_incomplete_and_unavailable_rows_keep_null_summary_semantics(kind: str) -> None:
    manifest = codec_manifest(query=SimulationQuery((0,)))
    if kind == "unavailable":
        frame = make_simulation_frame(make_simulation_prediction(remaining_ms=0), manifest.config)
        outcome = make_unavailable_outcome(frame, manifest.config, manifest.runtime)
    else:
        outcome = make_simulation_outcome(
            ((2, 2),) if kind == "partial" else (), unresolved=2 if kind == "partial" else 4
        )
    record = simulation_row_projection(manifest.sources[0].source, outcome, manifest.query)
    assert record["summaries"] is None
    line = cast(list[CanonicalObject], record["lines"])[0]
    assert line["prob_under_scaled"] is None and line["prob_equal_scaled"] is None
    assert line["count_unknown"] == (None if kind == "unavailable" else outcome.unresolved)
    assert parse_simulation_row(record, manifest) == outcome


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "mean",
        "bool",
        "float",
        "histogram",
        "unknown_mass",
        "seed",
        "model",
        "clock",
        "source",
        "pmf",
        "null",
    ],
)
def test_row_rejects_derived_value_tampering_even_when_values_look_plausible(mutation: str) -> None:
    manifest = codec_manifest(query=SimulationQuery((3000000,)))
    record = deepcopy(
        simulation_row_projection(
            manifest.sources[0].source, make_simulation_outcome(), manifest.query
        )
    )
    population = cast(CanonicalObject, record["population"])
    if mutation == "unknown":
        record["other"] = None
    elif mutation == "mean":
        cast(CanonicalObject, cast(CanonicalObject, record["summaries"])["home"])["mean_scaled"] = (
            3250001
        )
    elif mutation in {"bool", "float"}:
        line = cast(list[CanonicalObject], record["lines"])[0]
        if mutation == "bool":
            line["count_equal"] = True
        else:
            line["prob_equal_scaled"] = 250000.0  # type: ignore[assignment]
    elif mutation == "histogram":
        population["total_histogram"] = [[2, 4]]
    elif mutation == "unknown_mass":
        population["unresolved"] = 1
    elif mutation == "seed":
        population["seed_sha256"] = "d" * 64
    elif mutation == "model":
        cast(CanonicalObject, record["frame"])["model_artifact_sha256"] = "d" * 64
    elif mutation == "clock":
        cast(CanonicalObject, record["frame"])["seconds_remaining_game_ms"] = 1
    elif mutation == "source":
        cast(CanonicalObject, record["source"])["state_output_sha256"] = "d" * 64
    elif mutation == "pmf":
        cast(CanonicalObject, population["home_pmf"])["policy_version"] = "other"
    else:
        record["summaries"] = None
    with pytest.raises(ValueError):
        parse_simulation_row(record, manifest)


def test_operational_source_uuid_changes_exact_row_but_not_semantics_or_samples() -> None:
    manifest = codec_manifest()
    source = manifest.sources[0].source
    changed: ModelSourceReference = replace(
        source,
        reference=replace(
            source.reference,
            attempt=replace(
                source.reference.attempt,
                snapshot_id=UUID("00000000-0000-4000-8000-000000000999"),
            ),
        ),
    )
    outcome = make_simulation_outcome()
    assert simulation_row_projection(source, outcome, manifest.query) != simulation_row_projection(
        changed, outcome, manifest.query
    )
    assert simulation_row_semantic_projection(
        source, outcome, manifest.query
    ) == simulation_row_semantic_projection(changed, outcome, manifest.query)


def test_query_changes_summary_without_changing_population_or_seed() -> None:
    manifest = codec_manifest()
    outcome = make_simulation_outcome()
    first = simulation_row_projection(manifest.sources[0].source, outcome, manifest.query)
    query = SimulationQuery((3500000,), (500000,))
    changed = codec_manifest(query=query)
    second = simulation_row_projection(changed.sources[0].source, outcome, query)
    assert first["frame"] == second["frame"] and first["population"] == second["population"]
    assert first["summaries"] != second["summaries"] and first["lines"] != second["lines"]
    assert manifest.run_semantic_revision != changed.run_semantic_revision
    with pytest.raises(ValueError):
        parse_simulation_row(first, changed)


def test_descriptor_order_cannot_change_under_the_same_projection_version() -> None:
    record = deepcopy(simulation_manifest_projection(codec_manifest()))
    model = cast(CanonicalObject, record["model"])
    descriptor = cast(CanonicalObject, model["descriptor"])
    columns = cast(list[CanonicalObject], descriptor["columns"])
    columns.reverse()
    model["descriptor_sha256"] = canonical_json_sha256(descriptor)
    with pytest.raises(ValueError):
        parse_simulation_manifest(record)


def test_semantic_body_retains_actual_sampled_histograms() -> None:
    manifest = codec_manifest()
    source = manifest.sources[0].source
    first = simulation_row_semantic_projection(source, make_simulation_outcome(), manifest.query)
    second = simulation_row_semantic_projection(
        source,
        make_simulation_outcome(((2, 2), (4, 2))),
        manifest.query,
    )
    assert cast(CanonicalObject, first["population"]).get("total_histogram") == [
        [2, 1],
        [3, 1],
        [4, 2],
    ]
    assert cast(CanonicalObject, second["population"]).get("total_histogram") == [[2, 2], [4, 2]]
    assert first != second


def test_source_metadata_cannot_escape_the_simulation_int64_schema() -> None:
    manifest = codec_manifest()
    inputs = make_training_input()
    source = make_model_source_reference(replace(inputs.game, home_team_id=2**63), inputs.features)
    changed = replace(manifest, sources=(replace(manifest.sources[0], source=source),))
    with pytest.raises(ValueError, match="Int64"):
        simulation_manifest_projection(changed)


def test_full_uint64_seed_is_the_only_unsigned_numeric_exception() -> None:
    from nba_live_totals.domain.simulation_hashing import simulation_config_projection
    from nba_live_totals.infrastructure.files.simulation_codec import parse_simulation_config

    config = replace(codec_manifest().config, seed=2**64 - 1)
    assert parse_simulation_config(simulation_config_projection(config)) == config
