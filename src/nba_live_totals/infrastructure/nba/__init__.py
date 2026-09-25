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
from nba_live_totals.infrastructure.nba.feature_event_normalizer import (
    NBAPlayByPlayV3FeatureEventNormalizer,
)
from nba_live_totals.infrastructure.nba.lineup_normalizer import (
    NBAPlayByPlayV3LineupNormalizer,
)
from nba_live_totals.infrastructure.nba.possession_normalizer import (
    NBAPlayByPlayV3PossessionNormalizer,
)
from nba_live_totals.infrastructure.nba.source import (
    NBAApiDocumentFetcher,
    NBAApiHistoricalDataSource,
)
from nba_live_totals.infrastructure.nba.transport import NBAApiTransport

__all__ = [
    "EmptyProviderResponseError",
    "InvalidProviderJsonError",
    "NBAApiDocumentFetcher",
    "NBAApiHistoricalDataSource",
    "NBAApiTransport",
    "NBAPlayByPlayV3FeatureEventNormalizer",
    "NBAPlayByPlayV3LineupNormalizer",
    "NBAPlayByPlayV3PossessionNormalizer",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderSchemaError",
    "ProviderTimeoutError",
    "UpstreamHttpError",
]
