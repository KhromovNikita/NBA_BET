"""Validated provider-neutral identifiers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nba_live_totals.domain.games import SeasonType


_SEASON_ID_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_GAME_ID_PATTERN = re.compile(r"^\d{10}$")


@dataclass(frozen=True, slots=True)
class SeasonId:
    """NBA season identifier in the ``YYYY-YY`` format."""

    value: str

    def __post_init__(self) -> None:
        if _SEASON_ID_PATTERN.fullmatch(self.value) is None:
            msg = "season ID must use the YYYY-YY format"
            raise ValueError(msg)
        if int(self.value[5:]) != (int(self.value[:4]) + 1) % 100:
            msg = "season ID end year must follow its start year"
            raise ValueError(msg)

    @property
    def start_year(self) -> int:
        """Calendar year in which the season begins."""
        return int(self.value[:4])


@dataclass(frozen=True, slots=True)
class GameId:
    """Ten-digit NBA game identifier."""

    value: str

    def __post_init__(self) -> None:
        if _GAME_ID_PATTERN.fullmatch(self.value) is None:
            msg = "game ID must contain exactly ten digits"
            raise ValueError(msg)
        if self.value[:3] not in {"001", "002", "003", "004", "005"}:
            msg = f"unknown game type prefix: {self.value[:3]}"
            raise ValueError(msg)

    @property
    def season_type(self) -> SeasonType:
        """Classify regular-season and playoff game IDs."""
        from nba_live_totals.domain.games import SeasonType

        match self.value[:3]:
            case "002":
                return SeasonType.REGULAR_SEASON
            case "004":
                return SeasonType.PLAYOFFS
            case _:
                msg = f"unknown game type prefix: {self.value[:3]}"
                raise ValueError(msg)

    @property
    def is_excluded(self) -> bool:
        """Whether the ID denotes a preseason, All-Star, or Play-In game."""
        return self.value.startswith(("001", "003", "005"))
