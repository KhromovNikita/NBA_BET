import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from weakref import ReferenceType, ref

import pytest

from nba_live_totals.application.modeling.dataset_service import BuildTrainingDatasetService
from nba_live_totals.application.modeling.errors import ModelingError
from nba_live_totals.application.modeling.models import ModelSourceReference
from nba_live_totals.domain.identifiers import GameId, SeasonId
from nba_live_totals.domain.model_targets import TrainingGameInput
from nba_live_totals.infrastructure.files.training_dataset import AtomicTrainingDatasetExporter
from tests.unit.application.modeling_fakes import (
    MemoryModelSource,
    make_dataset_request,
    remap_training_input,
)
from tests.unit.domain.modeling_helpers import make_training_input


def make_dataset_service_with_final_race(path: Path) -> BuildTrainingDatasetService:
    source = MemoryModelSource()
    source.fail_final = True
    return BuildTrainingDatasetService(source, AtomicTrainingDatasetExporter())


async def test_final_source_race_preserves_previous_dataset(tmp_path: Path) -> None:
    path = tmp_path / "training.jsonl"
    path.write_bytes(b"old")
    with pytest.raises(ModelingError, match="model_source_changed"):
        await make_dataset_service_with_final_race(path).build(make_dataset_request(path))
    assert path.read_bytes() == b"old"
    assert await asyncio.to_thread(lambda: sorted(p.name for p in tmp_path.iterdir())) == [
        "training.jsonl"
    ]


async def test_service_releases_each_game_and_retains_every_state(tmp_path: Path) -> None:
    source = MemoryModelSource()
    path = tmp_path / "training.jsonl"
    result = await BuildTrainingDatasetService(source, AtomicTrainingDatasetExporter()).build(
        make_dataset_request(path)
    )
    assert result.summary.states == 8 and result.exit_code == 1
    assert source.events == ["select", "resolve", "read_training", "recheck", "recheck"]
    assert path.is_file()


async def test_empty_and_duplicate_selection_publish_nothing(tmp_path: Path) -> None:
    source = MemoryModelSource()
    source.empty = True
    path = tmp_path / "out"
    with pytest.raises(ModelingError, match="no_eligible_games"):
        await BuildTrainingDatasetService(source, AtomicTrainingDatasetExporter()).build(
            make_dataset_request(path)
        )
    assert not path.exists()


async def test_postcommit_cleanup_error_keeps_committed_result(tmp_path: Path) -> None:
    class CleanupError(AtomicTrainingDatasetExporter):
        async def abort(self) -> None:
            await super().abort()
            raise OSError("synthetic cleanup error")

    path = tmp_path / "training.jsonl"
    result = await BuildTrainingDatasetService(MemoryModelSource(), CleanupError()).build(
        make_dataset_request(path)
    )
    assert result.exit_code == 1 and path.is_file()


@pytest.mark.parametrize("duplicate", [True, False])
async def test_duplicate_or_reversed_game_selection_preserves_destination(
    tmp_path: Path, duplicate: bool
) -> None:
    first = make_training_input()
    second = remap_training_input(first, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC))

    class SelectionSource(MemoryModelSource):
        async def select_games(
            self, seasons: tuple[SeasonId, ...], game_id: GameId | None = None
        ) -> tuple[GameId, ...]:
            return (
                (first.game.game_id, first.game.game_id)
                if duplicate
                else (second.game.game_id, first.game.game_id)
            )

    path = tmp_path / "out"
    path.write_bytes(b"old")
    with pytest.raises(ModelingError, match=r"model_duplicate_games|model_source_order"):
        await BuildTrainingDatasetService(
            SelectionSource((first, second)), AtomicTrainingDatasetExporter()
        ).build(make_dataset_request(path))
    assert path.read_bytes() == b"old"


async def test_training_input_is_released_before_resolving_next_game(tmp_path: Path) -> None:
    @dataclass(frozen=True, slots=True, weakref_slot=True)
    class TrackedInput(TrainingGameInput):
        pass

    class TrackingSource(MemoryModelSource):
        previous: ReferenceType[TrackedInput] | None = None

        async def resolve(self, game_id: GameId) -> ModelSourceReference:
            assert self.previous is None or self.previous() is None
            return await super().resolve(game_id)

        async def read_training(self, source: ModelSourceReference) -> TrainingGameInput:
            original = await super().read_training(source)
            tracked = TrackedInput(
                original.game, original.features, original.possessions, original.period_ends
            )
            self.previous = ref(tracked)
            return tracked

    first = make_training_input()
    second = remap_training_input(first, GameId("0022500002"), datetime(2025, 10, 22, tzinfo=UTC))
    await BuildTrainingDatasetService(
        TrackingSource((first, second)), AtomicTrainingDatasetExporter()
    ).build(make_dataset_request(tmp_path / "out"))
