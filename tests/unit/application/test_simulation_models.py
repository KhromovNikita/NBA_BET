"""Selectors and alias guards protect checked input artifacts before simulation starts."""

from dataclasses import replace
from pathlib import Path

import pytest

from nba_live_totals.application.simulation.errors import SimulationError
from nba_live_totals.application.simulation.models import (
    SelectedSimulationStates,
    SimulateTotalsResult,
    SimulationCounters,
    SimulationSourceSelection,
    ValidatedSimulationExport,
    ensure_simulation_paths_distinct,
)
from nba_live_totals.domain.identifiers import GameId, SeasonId
from tests.unit.application.simulation_fakes import (
    make_simulation_manifest,
    make_simulation_request,
)


def test_selection_stays_bounded_for_all_states() -> None:
    selection = SelectedSimulationStates(1000000)
    assert selection.count == 1000000 and selection.numbers == range(1000000)
    assert SelectedSimulationStates(5, 3).numbers == (3,)
    assert SelectedSimulationStates(5, 3).count == 1
    for total, state in ((0, None), (5, 5), (5, -1), (True, None), (5, True)):
        with pytest.raises(ValueError):
            SelectedSimulationStates(total, state)


def test_state_selector_requires_exact_game_and_single_season(tmp_path: Path) -> None:
    request = make_simulation_request(tmp_path / "out.jsonl")
    with pytest.raises(ValueError):
        replace(request, state_number=0)
    with pytest.raises(ValueError):
        replace(
            request,
            seasons=(SeasonId("2024-25"), SeasonId("2025-26")),
            game_id=GameId("0022500001"),
        )
    selected = replace(request, game_id=GameId("0022500001"), state_number=0)
    assert selected.state_number == 0 and selected.model_path.is_absolute()
    for seasons in ((), (SeasonId("2025-26"),) * 2):
        with pytest.raises(ValueError):
            replace(request, seasons=seasons)


@pytest.mark.parametrize("input_kind", ["model", "config"])
@pytest.mark.parametrize("alias", ["same", "resolved", "hardlink", "symlink"])
def test_output_alias_never_overwrites_model_or_config(
    tmp_path: Path, input_kind: str, alias: str
) -> None:
    request = make_simulation_request(tmp_path / "out.jsonl")
    original = tmp_path / input_kind
    original.write_bytes(b"original checked input")
    output = original
    if alias == "resolved":
        output = tmp_path / "sub" / ".." / input_kind
    elif alias in {"hardlink", "symlink"}:
        output = tmp_path / "alias"
        if alias == "hardlink":
            output.hardlink_to(original)
        else:
            try:
                output.symlink_to(original)
            except OSError:
                pytest.skip("filesystem symlink creation unavailable on this platform")
    changes = {
        "output_path": output,
        "model_path" if input_kind == "model" else "config_path": original,
    }
    with pytest.raises(ValueError):
        replace(request, **changes)  # type: ignore[arg-type]
    assert original.read_bytes() == b"original checked input"


def test_existing_hardlink_is_rechecked_before_publication(tmp_path: Path) -> None:
    request = make_simulation_request(tmp_path / "out.jsonl")
    request.model_path.write_bytes(b"model")
    request.output_path.hardlink_to(request.model_path)
    with pytest.raises(ValueError):
        ensure_simulation_paths_distinct(request)
    assert request.model_path.read_bytes() == b"model"


def test_counters_and_exit_codes_preserve_partial_mass() -> None:
    complete = SimulationCounters(1, 3, 3, 0, 0, 0, ())
    partial = SimulationCounters(1, 3, 1, 1, 1, 2, (("overtime_limit", 2),))
    assert SimulateTotalsResult(complete, "a" * 64, "b" * 64).exit_code == 0
    assert SimulateTotalsResult(partial, "a" * 64, "b" * 64).exit_code == 1
    for changes in (
        {"states": 4},
        {"unresolved": 3},
        {"unresolved_counts": (("other", 2),)},
        {"incomplete": 0},
        {"games": True},
    ):
        with pytest.raises(ValueError):
            replace(partial, **changes)


def test_manifest_checks_selected_sources_counts_versions_and_hashes(tmp_path: Path) -> None:
    manifest = make_simulation_manifest()
    (selection,) = manifest.sources
    assert manifest.counts.games == 1 and manifest.counts.states == 8
    artifact = ValidatedSimulationExport(tmp_path / "out.jsonl", manifest, "a" * 64)
    assert artifact.path.is_absolute()
    with pytest.raises(ValueError):
        SimulationSourceSelection(selection.source, SelectedSimulationStates(9))
    for changes in (
        {"sources": (selection, selection)},
        {"sources": ()},
        {"state_number": 0},
        {"schema_version": "unknown"},
        {"source_sha256": "bad"},
    ):
        with pytest.raises(ValueError):
            replace(manifest, **changes)
    one = SimulationSourceSelection(selection.source, SelectedSimulationStates(8, 2))
    with pytest.raises(ValueError):
        replace(manifest, sources=(one,))
    with pytest.raises(ValueError):
        replace(artifact, artifact_sha256="bad")


def test_error_codes_cannot_expose_credentials_or_arbitrary_messages() -> None:
    assert str(SimulationError("simulation_native_error")) == "simulation_native_error"
    for code in ("password:secret", "some_new_code", "postgresql://private@server/db"):
        with pytest.raises(ValueError):
            SimulationError(code)


def test_manifest_retains_no_mutable_selector_alias() -> None:
    manifest = make_simulation_manifest()
    seasons = [SeasonId("2025-26")]
    normalized = replace(manifest, seasons=seasons)  # type: ignore[arg-type]
    seasons.clear()
    assert normalized.seasons == (SeasonId("2025-26"),)
