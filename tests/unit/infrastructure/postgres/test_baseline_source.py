from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nba_live_totals.application.baselines.errors import BaselineExportError
from nba_live_totals.application.features import (
    CurrentFeatureSnapshotResolution,
    FeaturePublicationState,
)
from nba_live_totals.domain.features import FeatureVersions
from nba_live_totals.infrastructure.postgres.baseline_source import PublishedFeatureBaselineSource
from tests.unit.application.feature_fakes import make_target
from tests.unit.application.test_feature_models import make_reference


def fixture() -> tuple[
    PublishedFeatureBaselineSource, AsyncMock, AsyncMock, AsyncMock, FeatureVersions
]:
    store, reader, snapshots = AsyncMock(), AsyncMock(), AsyncMock()
    store.resolve_target.return_value = make_target()[0]
    store.resolve_current.return_value = CurrentFeatureSnapshotResolution(
        FeaturePublicationState.CURRENT, make_reference()
    )
    reader.read_target.return_value = SimpleNamespace(target_player_ids=(101, 102))
    versions = FeatureVersions(join_version="possession-lineup-inclusive-v1")
    return (
        PublishedFeatureBaselineSource(store, reader, snapshots, versions=versions),
        store,
        reader,
        snapshots,
        versions,
    )


async def test_resolve_uses_exact_current_reference_and_never_claims() -> None:
    adapter, store, reader, _, versions = fixture()
    source = await adapter.resolve(make_target()[0].game.game_id)
    assert source.reference == make_reference()
    store.resolve_current.assert_awaited_once_with(make_target()[0], (101, 102), versions)
    reader.read_target.assert_awaited_once()
    store.claim_game.assert_not_called()
    store.start_run.assert_not_called()
    await adapter.recheck(source)


@pytest.mark.parametrize("status", [FeaturePublicationState.ABSENT, FeaturePublicationState.STALE])
async def test_missing_and_stale_sources_fail(status: FeaturePublicationState) -> None:
    adapter, store, _, _, _ = fixture()
    store.resolve_current.return_value = CurrentFeatureSnapshotResolution(status)
    with pytest.raises(BaselineExportError):
        await adapter.resolve(make_target()[0].game.game_id)


async def test_changed_reference_recheck_rejects() -> None:
    adapter, store, _, _, _ = fixture()
    source = await adapter.resolve(make_target()[0].game.game_id)
    from tests.unit.application.feature_fakes import uuid4_value

    reference = make_reference()
    changed = replace(reference, attempt=replace(reference.attempt, snapshot_id=uuid4_value(999)))
    store.resolve_current.return_value = CurrentFeatureSnapshotResolution(
        FeaturePublicationState.CURRENT, changed
    )
    with pytest.raises(BaselineExportError):
        await adapter.recheck(source)


async def test_snapshot_read_error_is_safe() -> None:
    from nba_live_totals.infrastructure.clickhouse.features import FeatureSnapshotStoreError

    adapter, _, _, snapshots, _ = fixture()
    source = await adapter.resolve(make_target()[0].game.game_id)
    snapshots.read_snapshot.side_effect = FeatureSnapshotStoreError("feature_snapshot_mismatch")
    with pytest.raises(BaselineExportError, match="baseline_source_mismatch"):
        await adapter.read(source)


async def test_selection_is_verify_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from nba_live_totals.application.baselines.models import BuildBaselinesRequest
    from nba_live_totals.application.features import FeatureBuildMode
    from nba_live_totals.domain.identifiers import SeasonId

    adapter, store, _, _, _ = fixture()
    store.select_games.return_value = ()
    await adapter.select_games(BuildBaselinesRequest((SeasonId("2025-26"),), tmp_path / "out"))
    assert store.select_games.call_args.args[0].mode is FeatureBuildMode.VERIFY
    store.start_run.assert_not_called()
