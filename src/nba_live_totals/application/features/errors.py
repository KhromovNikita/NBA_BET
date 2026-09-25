"""Safe errors exposed by feature application boundaries."""

from __future__ import annotations

_FEATURE_NORMALIZATION_ERROR_CODES = frozenset(
    {
        "input_document_checksum_mismatch",
        "input_document_identity_mismatch",
        "input_resource_not_applied",
        "raw_canonical_cardinality_mismatch",
        "raw_canonical_event_mismatch",
        "raw_canonical_team_mismatch",
        "unsupported_document_schema",
    }
)


class FeatureNormalizationError(RuntimeError):
    """A structural input failure represented only by a bounded safe code."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _FEATURE_NORMALIZATION_ERROR_CODES:
            raise ValueError("feature normalization error code must be allowlisted")
        self.code = code
        super().__init__(code)


__all__ = ["FeatureNormalizationError"]
