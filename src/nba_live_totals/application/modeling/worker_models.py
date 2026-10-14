"""Small immutable CPU-worker messages without process or database handles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from nba_live_totals.domain.baselines import bounded_integer
from nba_live_totals.domain.canonical import CanonicalObject, canonical_json_bytes
from nba_live_totals.domain.model_evaluation import EvaluationReport
from nba_live_totals.domain.model_features import ModelFeatureVector, require_sha256
from nba_live_totals.domain.model_targets import ModelHead
from nba_live_totals.domain.model_training import TrainingConfig

if TYPE_CHECKING:
    from nba_live_totals.application.modeling.models import ValidatedTrainingDataset

MAX_MODEL_BYTES = 128 * 1024**2
MAX_JSON_BYTES = 16 * 1024**2
MAX_BUNDLE_BYTES = 512 * 1024**2
CATBOOST_VERSION = "1.2.10"


@dataclass(frozen=True, slots=True)
class TrainingWorkerJob:
    dataset: ValidatedTrainingDataset
    work_dir: Path
    config: TrainingConfig

    def __post_init__(self) -> None:
        from nba_live_totals.application.modeling.models import ValidatedTrainingDataset

        if not isinstance(self.dataset, ValidatedTrainingDataset) or not isinstance(
            self.config, TrainingConfig
        ):
            raise ValueError("invalid training worker dataset or config")
        if not isinstance(self.work_dir, Path) or not self.work_dir.name:
            raise ValueError("invalid training workspace")
        object.__setattr__(self, "work_dir", self.work_dir.resolve())


@dataclass(frozen=True, slots=True)
class RuntimeVersions:
    python: str
    catboost: str
    numpy: str
    platform: str
    architecture: str

    def __post_init__(self) -> None:
        if type(self.python) is not str or re.fullmatch(r"3\.14\.[0-9]+", self.python) is None:
            raise ValueError("unsupported Python runtime")
        if self.catboost != CATBOOST_VERSION:
            raise ValueError("unsupported CatBoost runtime")
        if (
            type(self.numpy) is not str
            or re.fullmatch(r"2\.[0-9]+\.[0-9]+", self.numpy) is None
            or int(self.numpy.split(".")[1]) < 4
        ):
            raise ValueError("unsupported NumPy runtime")
        for value in (self.platform, self.architecture):
            if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", value) is None:
                raise ValueError("invalid runtime platform metadata")


@dataclass(frozen=True, slots=True)
class HeadModelFile:
    head: ModelHead
    path: Path
    sha256: str
    size: int
    best_iteration: int
    tree_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.head, ModelHead) or not isinstance(self.path, Path):
            raise ValueError("invalid native head identity or path")
        object.__setattr__(self, "path", self.path.resolve())
        require_sha256(self.sha256)
        bounded_integer(self.size, minimum=1, maximum=MAX_MODEL_BYTES)
        bounded_integer(self.tree_count, minimum=1, maximum=100_000)
        bounded_integer(self.best_iteration, maximum=self.tree_count - 1)


@dataclass(frozen=True, slots=True)
class ValidationProbe:
    head: ModelHead
    vectors: tuple[ModelFeatureVector, ...]
    values_scaled: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.head, ModelHead)
            or type(self.vectors) is not tuple
            or type(self.values_scaled) is not tuple
        ):
            raise ValueError("invalid validation probe")
        if not 1 <= len(self.vectors) <= 8 or len(self.vectors) != len(self.values_scaled):
            raise ValueError("validation probe requires one to eight paired values")
        if (
            any(not isinstance(v, ModelFeatureVector) for v in self.vectors)
            or len({v.descriptor_sha256 for v in self.vectors}) != 1
        ):
            raise ValueError("validation probe descriptor mismatch")
        for value in self.values_scaled:
            bounded_integer(value)


@dataclass(frozen=True, slots=True)
class TrainingWorkerResult:
    models: tuple[HeadModelFile, ...]
    evaluation: EvaluationReport
    runtime: RuntimeVersions
    resolved_parameters: Mapping[ModelHead, CanonicalObject]
    probes: tuple[ValidationProbe, ...]

    def __post_init__(self) -> None:
        if (
            type(self.models) is not tuple
            or any(not isinstance(m, HeadModelFile) for m in self.models)
            or tuple(m.head for m in self.models) != tuple(ModelHead)
        ):
            raise ValueError("worker requires all three ordered model files")
        if not isinstance(self.evaluation, EvaluationReport) or not isinstance(
            self.runtime, RuntimeVersions
        ):
            raise ValueError("invalid worker evaluation or runtime")
        if set(self.resolved_parameters) != set(ModelHead) or any(
            not isinstance(h, ModelHead) for h in self.resolved_parameters
        ):
            raise ValueError("worker parameters require all typed heads")
        parameters = deepcopy(dict(self.resolved_parameters))
        for value in parameters.values():
            if type(value) is not dict:
                raise ValueError("resolved parameters require canonical objects")
            canonical_json_bytes(value)
        object.__setattr__(self, "resolved_parameters", MappingProxyType(parameters))
        if (
            type(self.probes) is not tuple
            or any(not isinstance(p, ValidationProbe) for p in self.probes)
            or tuple(p.head for p in self.probes) != tuple(ModelHead)
        ):
            raise ValueError("worker requires all ordered validation probes")
