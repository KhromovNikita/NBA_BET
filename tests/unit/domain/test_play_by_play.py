import pytest

from nba_live_totals.domain.identifiers import GameId
from nba_live_totals.domain.play_by_play import PlayByPlayEvent, validate_play_by_play


def make_event(action_number: int, **overrides: object) -> PlayByPlayEvent:
    values: dict[str, object] = {
        "game_id": GameId("0022500123"),
        "sequence_number": 1,
        "action_number": action_number,
        "action_id": None,
        "period": 1,
        "clock": "PT11M59.00S",
        "seconds_remaining_period_ms": 719_000,
        "team_id": 10,
        "person_id": 1,
        "action_type": "2pt",
        "sub_type": "Jump Shot",
        "description": "Test Player makes 2-pt jump shot",
        "home_score": 2,
        "away_score": 0,
        "shot_result": "Made",
        "shot_distance": 15,
        "legacy_x": 12,
        "legacy_y": 8,
        "location": "Home",
        "is_field_goal": True,
        "points_total": 2,
        "video_available": False,
    }
    values.update(overrides)
    return PlayByPlayEvent(**values)  # type: ignore[arg-type]


def test_play_by_play_orders_by_unique_sequence_and_preserves_duplicate_actions() -> None:
    events = (
        make_event(7, sequence_number=3),
        make_event(3, sequence_number=1),
        make_event(3, sequence_number=2, action_id=None),
    )
    ordered = validate_play_by_play(events)

    assert tuple(event.sequence_number for event in ordered) == (1, 2, 3)
    assert tuple(event.action_number for event in ordered) == (3, 3, 7)
    assert tuple(event.action_id for event in ordered) == (None, None, None)

    with pytest.raises(ValueError, match="duplicate sequence"):
        validate_play_by_play((make_event(3), make_event(4)))

    assert validate_play_by_play((make_event(0),))[0].action_number == 0


def test_completed_game_cannot_have_empty_play_by_play() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_play_by_play((), completed=True)


def test_play_by_play_allows_substitution_overtime_and_nullable_person() -> None:
    substitution = make_event(
        1,
        period=5,
        person_id=None,
        action_type="substitution",
        sub_type="in",
        description="Substitution: Test Player enters the game",
        is_field_goal=False,
        points_total=0,
    )

    assert validate_play_by_play((substitution,)) == (substitution,)


@pytest.mark.parametrize("milliseconds", [0, 720_000])
def test_play_by_play_accepts_clock_millisecond_boundaries(milliseconds: int) -> None:
    event = make_event(1, seconds_remaining_period_ms=milliseconds)

    assert event.seconds_remaining_period_ms == milliseconds


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"action_number": True}, "action number"),
        ({"action_id": True}, "action ID"),
        ({"sequence_number": 0}, "sequence number"),
        ({"period": 0}, "period"),
        ({"seconds_remaining_period_ms": -1}, "clock"),
        ({"seconds_remaining_period_ms": None}, "clock"),
        ({"home_score": -1}, "scores"),
        ({"shot_distance": -1}, "shot distance"),
        ({"legacy_x": True}, "legacy x"),
        ({"points_total": True}, "points total"),
    ],
)
def test_play_by_play_rejects_invalid_numeric_values(
    overrides: dict[str, object], message: str
) -> None:
    action_number = overrides.get("action_number", 1)
    remaining_overrides = {key: value for key, value in overrides.items() if key != "action_number"}
    with pytest.raises(ValueError, match=message):
        make_event(action_number, **remaining_overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"game_id": "0022500123"}, "game ID"),
        ({"clock": None}, "clock"),
        ({"action_type": None}, "action type"),
        ({"sub_type": None}, "sub type"),
        ({"description": None}, "description"),
        ({"shot_result": 1}, "shot result"),
        ({"location": 1}, "location"),
    ],
)
def test_play_by_play_rejects_invalid_identity_and_string_types(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_event(1, **overrides)


def test_play_by_play_rejects_mixed_game_ids() -> None:
    with pytest.raises(ValueError, match="one game"):
        validate_play_by_play((make_event(1), make_event(2, game_id=GameId("0022500124"))))
