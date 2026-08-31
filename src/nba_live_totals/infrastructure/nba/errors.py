"""Safe error taxonomy for the NBA.com provider boundary."""

from nba_live_totals.application.ingestion import IngestionResourceError


class ProviderError(IngestionResourceError):
    """A provider failure whose string form contains only stable metadata."""

    def __str__(self) -> str:
        return f"{type(self).__name__}(code={self.code})"


class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its configured timeout."""

    def __init__(self) -> None:
        super().__init__("network_timeout")


class ProviderConnectionError(ProviderError):
    """The provider could not be reached."""

    def __init__(self) -> None:
        super().__init__("network_connection")


class UpstreamHttpError(ProviderError):
    """A retryable provider HTTP response classified without its body."""

    def __init__(self, status_code: int) -> None:
        if status_code == 429:
            code = "rate_limited"
        elif status_code >= 500:
            code = "upstream_server"
        else:
            msg = "upstream HTTP errors require status 429 or 5xx"
            raise ValueError(msg)
        super().__init__(code)


class EmptyProviderResponseError(ProviderError):
    """The provider response was not a non-empty JSON object."""

    def __init__(self) -> None:
        super().__init__("empty_response")


class InvalidProviderJsonError(ProviderError):
    """The provider response could not be decoded as JSON."""

    def __init__(self) -> None:
        super().__init__("invalid_json")


class ProviderSchemaError(ProviderError):
    """The provider payload or transport response violated its expected shape."""
