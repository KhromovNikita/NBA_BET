"""NBA.com raw transport and source-document adapters."""

from nba_live_totals.infrastructure.nba.errors import (
    EmptyProviderResponseError,
    InvalidProviderJsonError,
    ProviderConnectionError,
    ProviderError,
    ProviderSchemaError,
    ProviderTimeoutError,
    UpstreamHttpError,
)
from nba_live_totals.infrastructure.nba.source import NBAApiDocumentFetcher
from nba_live_totals.infrastructure.nba.transport import NBAApiTransport

__all__ = [
    "EmptyProviderResponseError",
    "InvalidProviderJsonError",
    "NBAApiDocumentFetcher",
    "NBAApiTransport",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderSchemaError",
    "ProviderTimeoutError",
    "UpstreamHttpError",
]
