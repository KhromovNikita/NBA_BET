"""Immutable provider-neutral NBA play-by-play event models."""

from collections.abc import Iterable
from dataclasses import dataclass

from nba_live_totals.domain.identifiers import GameId


def _require_positive_integer(value: int, *, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ValueError(msg)


def _require_nonnegative_integer(value: int | None, *, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        msg = f"{field_name} must be a nonnegative integer"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PlayByPlayEvent:
    """One unprocessed action from an NBA play-by-play feed."""

    game_id: GameId
    action_number: int
    action_id: int | None
    period: int
    clock: str
    seconds_remaining_period_ms: int
    team_id: int | None
    person_id: int | None
    action_type: str
    sub_type: str
    description: str
    home_score: int | None
    away_score: int | None
    shot_result: str | None
    shot_distance: int | None
    legacy_x: int | None
    legacy_y: int | None
    location: str | None
    is_field_goal: bool
    points_total: int
    video_available: bool

    def __post_init__(self) -> None:
        _require_positive_integer(self.action_number, field_name="action number")
        _require_positive_integer(self.period, field_name="period")
        _require_nonnegative_integer(
            self.seconds_remaining_period_ms, field_name="clock milliseconds"
        )
        _require_nonnegative_integer(self.home_score, field_name="scores")
        _require_nonnegative_integer(self.away_score, field_name="scores")
        _require_nonnegative_integer(self.shot_distance, field_name="shot distance")
        if self.team_id is not None:
            _require_positive_integer(self.team_id, field_name="team ID")
        if self.person_id is not None:
            _require_positive_integer(self.person_id, field_name="person ID")
        if type(self.is_field_goal) is not bool or type(self.video_available) is not bool:
            msg = "field-goal and video flags must be booleans"
            raise ValueError(msg)


def validate_play_by_play(
    events: Iterable[PlayByPlayEvent], *, completed: bool = True
) -> tuple[PlayByPlayEvent, ...]:
    """Validate an event sequence and return it ordered by action number."""
    ordered_events = tuple(sorted(events, key=lambda event: event.action_number))
    if completed and not ordered_events:
        msg = "completed game play-by-play cannot be empty"
        raise ValueError(msg)

    if not ordered_events:
        return ordered_events

    game_id = ordered_events[0].game_id
    action_numbers: set[int] = set()
    for event in ordered_events:
        if event.game_id != game_id:
            msg = "play-by-play events must belong to one game"
            raise ValueError(msg)
        if event.action_number in action_numbers:
            msg = "play-by-play contains duplicate action numbers"
            raise ValueError(msg)
        action_numbers.add(event.action_number)
    return ordered_events
