"""Actual offline Phase 2→7 CLI acceptance against owned stores and native CPU models."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.infrastructure.files.model_bundle import VerifiedModelBundleStore
from nba_live_totals.infrastructure.files.training_dataset import CanonicalTrainingDatasetReader
from tests.integration.modeling_harness import GAMES, SEASON, modeling_acceptance

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


async def test_offline_modeling_end_to_end_is_read_only(tmp_path: Path) -> None:
    pytest.importorskip("catboost", reason="optional ML extra required")
    async with modeling_acceptance() as harness:
        sources = await harness.seed_pipeline()
        before = await harness.storage_projection()
        artifacts = await harness.run_clis(tmp_path)
        dataset, bundle = artifacts.dataset, artifacts.bundle
        assert dataset.summary.trainable and dataset.summary.games == 5
        assert bundle.manifest.label_version == "remaining-horizon-labels-v1"
        assert bundle.manifest.dataset == dataset.summary
        assert (
            bundle.manifest.dataset.dataset_artifact_sha256
            == dataset.summary.dataset_artifact_sha256
        )
        assert tuple(head.head for head in bundle.manifest.heads) == tuple(ModelHead)
        assert all(head.tree_count >= 1 for head in bundle.manifest.heads)
        with ZipFile(artifacts.bundle_path) as archive:
            raw_metrics = archive.read("metrics.json")
            assert hashlib.sha256(raw_metrics).hexdigest() == bundle.manifest.metrics_sha256
            metrics = json.loads(raw_metrics)
        assert len(metrics["cohorts"]) == 18
        assert {c["partition"] for c in metrics["cohorts"]} == {"validation", "test"}
        for cohort in metrics["cohorts"]:
            for comparison in cohort["comparisons"]:
                assert comparison["baseline"]["row_count"] == comparison["model"]["row_count"]
                assert comparison["baseline"]["game_count"] == comparison["model"]["game_count"]
        assert (
            hashlib.sha256(artifacts.bundle_path.read_bytes()).hexdigest() == bundle.artifact_sha256
        )
        predictor = VerifiedModelBundleStore().load(artifacts.bundle_path).predictor
        features = await harness.source.read_features(sources[0])
        assert predictor.predict_state(features.states[0]) == bundle.predictor.predict_state(
            features.states[0]
        )
        duplicate = tmp_path / "again.jsonl"
        code, out, err = await harness.run_cli(
            "build_training_dataset", *harness.dataset_args(duplicate)
        )
        assert code == 0, (out, err)
        assert duplicate.read_bytes() == artifacts.dataset_path.read_bytes()
        rows = list(CanonicalTrainingDatasetReader().iter_rows(dataset))
        partitions = {
            gid.value: {r.partition.value for r in rows if r.game_id == gid} for gid in GAMES
        }
        assert list(partitions.values()) == [
            {"train"},
            {"train"},
            {"embargo"},
            {"validation"},
            {"test"},
        ]
        metadata, *predictions = [
            json.loads(line) for line in artifacts.predictions_path.read_bytes().splitlines()
        ]
        assert len(predictions) == sum(s.reference.state_row_count for s in sources)
        assert (
            metadata["body_sha256"]
            == hashlib.sha256(
                b"".join(artifacts.predictions_path.read_bytes().splitlines(keepends=True)[1:])
            ).hexdigest()
        )
        assert any(
            r["rates"]["pace"]["unavailable_reason"] == "no_future_time" for r in predictions
        )
        assert any(
            r["game_id"] == GAMES[0].value
            and r["state_number"] == 0
            and r["rates"]["pace"]["value_scaled"] is not None
            for r in predictions
        )
        second = tmp_path / "predictions-again.jsonl"
        code, out, err = await harness.run_cli(
            "predict_models",
            "--season",
            SEASON.value,
            "--model",
            str(artifacts.bundle_path),
            "--output",
            str(second),
        )
        assert code == 1, (out, err)
        assert second.read_bytes() == artifacts.predictions_path.read_bytes()
        assert await harness.storage_projection() == before


async def test_model_predictions_recover_only_with_explicit_fixture_reset(tmp_path: Path) -> None:
    pytest.importorskip("catboost", reason="optional ML extra required")
    async with modeling_acceptance(recovery=True) as harness:
        sources = await harness.seed_pipeline()
        target = await harness.source.read_features(sources[-1])
        invalid = [s.state_number for s in target.states if not s.is_valid]
        assert invalid and any(s.is_valid and s.state_number > max(invalid) for s in target.states)
        before = await harness.storage_projection()
        artifacts = await harness.run_clis(tmp_path)
        predictions = [
            json.loads(line) for line in artifacts.predictions_path.read_bytes().splitlines()[1:]
        ]
        target_rows = [r for r in predictions if r["game_id"] == GAMES[-1].value]
        assert all(
            r["rates"]["pace"]["value_scaled"] is None
            for r in target_rows
            if r["state_number"] in invalid
        )
        assert any(
            r["rates"]["pace"]["value_scaled"] is not None
            for r in target_rows
            if r["state_number"] > max(invalid)
        )
        assert await harness.storage_projection() == before
