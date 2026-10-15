"""Allowlisted public diagnostics and an internal cooperative-stop signal."""

_ERROR_CODES = frozenset(
    {
        "simulation_invalid_input",
        "simulation_empty_selection",
        "simulation_source_order",
        "simulation_source_mismatch",
        "simulation_feature_versions_incompatible",
        "simulation_model_mismatch",
        "simulation_missing_state",
        "simulation_duplicate_state",
        "simulation_native_error",
        "simulation_numeric_overflow",
        "simulation_histogram_limit",
        "simulation_artifact_invalid",
        "simulation_io_error",
        "simulation_executor_busy",
        "ml_dependencies_missing",
    }
)


class SimulationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("invalid simulation error code")
        self.code = code
        super().__init__(code)


class SimulationStopped(RuntimeError):
    """Internal worker signal; cancellation joins the worker before returning."""
