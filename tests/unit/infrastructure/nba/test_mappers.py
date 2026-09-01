import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from nba_live_totals.domain import (
    GameId,
    GameStatus,
    JsonObject,
    JsonValue,
    PlayByPlayEvent,
    SeasonId,
    SeasonType,
    SourceDocument,
    Team,
    TraditionalStats,
    canonical_json_checksum,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers import (
    parse_box_score,
    parse_play_by_play,
    parse_schedule,
)
from nba_live_totals.infrastructure.nba.mappers.common import (
    parse_clock_milliseconds,
    parse_utc_datetime,
)

_FIXTURE_ROOT = Path(__file__).parents[3] / "fixtures" / "nba"


def _fixture(name: str) -> JsonObject:
    value = json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _document(
    payload: JsonObject,
    *,
    endpoint: str,
    resource_key: str,
    parameters: JsonObject | None = None,
    source: str = "nba.com",
) -> SourceDocument:
    return SourceDocument(
        source=source,
        endpoint=endpoint,
        resource_key=resource_key,
        requested_at=datetime(2026, 1, 2, 3, 0, tzinfo=UTC),
        request_parameters=parameters or {},
        payload=payload,
        checksum_sha256=canonical_json_checksum(payload),
    )


def _schedule_document(payload: JsonObject) -> SourceDocument:
    return _document(
        payload,
        endpoint="scheduleleaguev2",
        resource_key="schedule:2025-26",
        parameters={"LeagueID": "00", "Season": "2025-26"},
    )


def _first_schedule_game(payload: JsonObject) -> JsonObject:
    league_schedule = cast(JsonObject, payload["leagueSchedule"])
    game_dates = cast(list[JsonValue], league_schedule["gameDates"])
    game_date = cast(JsonObject, game_dates[0])
    games = cast(list[JsonValue], game_date["games"])
    return cast(JsonObject, games[0])


def _box_score_document(payload: JsonObject) -> SourceDocument:
    return _document(
        payload,
        endpoint="boxscoretraditionalv3",
        resource_key="box_score:0022500123",
        parameters={"GameID": "0022500123"},
    )


def _play_by_play_document(payload: JsonObject) -> SourceDocument:
    return _document(
        payload,
        endpoint="playbyplayv3",
        resource_key="play_by_play:0022500123",
        parameters={"GameID": "0022500123"},
    )


def test_schedule_maps_regular_and_playoff_games_and_deduplicates_teams() -> None:
    payload = _fixture("schedule_2025-26.json")
    league_schedule = cast(dict[str, object], payload["leagueSchedule"])
    game_dates = cast(list[dict[str, object]], league_schedule["gameDates"])
    games = cast(list[dict[str, object]], game_dates[0]["games"])
    playoff = deepcopy(games[0])
    playoff["gameId"] = "0042500124"
    games.append(playoff)
    before = deepcopy(payload)

    schedule = parse_schedule(_schedule_document(payload))

    assert schedule.season == SeasonId("2025-26")
    assert schedule.teams == (
        Team(10, "Denver", "Nuggets", "DEN", "nuggets"),
        Team(20, "Los Angeles", "Lakers", "LAL", "lakers"),
    )
    assert [game.game_id.value for game in schedule.games] == ["0022500123", "0042500124"]
    assert [game.season_type for game in schedule.games] == [
        SeasonType.REGULAR_SEASON,
        SeasonType.PLAYOFFS,
    ]
    assert payload == before


def test_schedule_preserves_final_scores_utc_arena_status_and_team_sides() -> None:
    schedule = parse_schedule(_schedule_document(_fixture("schedule_2025-26.json")))

    game = schedule.games[0]
    assert game.game_id == GameId("0022500123")
    assert game.game_date == date(2026, 1, 1)
    assert game.started_at == datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    assert game.home_team_id == 10
    assert game.away_team_id == 20
    assert game.home_score == 120
    assert game.away_score == 110
    assert game.status is GameStatus.FINAL
    assert (game.arena_name, game.arena_city, game.arena_state) == (
        "Ball Arena",
        "Denver",
        "CO",
    )


@pytest.mark.parametrize(
    ("provider_status", "status_text", "expected"),
    [
        (1, "7:00 pm ET", GameStatus.SCHEDULED),
        (2, "Q3 04:21", GameStatus.LIVE),
        (3, "Final/OT", GameStatus.FINAL),
        (7, "Game Postponed", GameStatus.POSTPONED),
        (7, "Game Cancelled", GameStatus.CANCELLED),
        (7, "TBD", GameStatus.UNKNOWN),
    ],
)
def test_schedule_maps_every_provider_status_branch(
    provider_status: int,
    status_text: str,
    expected: GameStatus,
) -> None:
    payload = _fixture("schedule_2025-26.json")
    game = _first_schedule_game(payload)
    game["gameStatus"] = provider_status
    game["gameStatusText"] = status_text
    if expected is not GameStatus.FINAL:
        cast(JsonObject, game["homeTeam"])["score"] = None
        cast(JsonObject, game["awayTeam"])["score"] = None

    schedule = parse_schedule(_schedule_document(payload))

    assert schedule.games[0].status is expected


def test_schedule_excludes_preseason_and_all_star_games_and_their_teams() -> None:
    payload = _fixture("schedule_2025-26.json")
    game_dates = cast(
        list[dict[str, object]],
        cast(dict[str, object], payload["leagueSchedule"])["gameDates"],
    )
    game = cast(dict[str, object], cast(list[object], game_dates[0]["games"])[0])
    preseason = deepcopy(game)
    preseason["gameId"] = "0012500123"
    all_star = deepcopy(game)
    all_star["gameId"] = "0032500124"
    game_dates[0]["games"] = [preseason, all_star]

    schedule = parse_schedule(_schedule_document(payload))

    assert schedule.teams == ()
    assert schedule.games == ()


def test_schedule_does_not_silently_exclude_a_malformed_game_id() -> None:
    payload = _fixture("schedule_2025-26.json")
    _first_schedule_game(payload)["gameId"] = "001-invalid"

    with pytest.raises(ProviderSchemaError) as caught:
        parse_schedule(_schedule_document(payload))

    assert caught.value.code == "schema_mismatch"


def test_schedule_rejects_non_ascii_digit_game_id_as_context_free_schema_mismatch() -> None:
    payload = _fixture("schedule_2025-26.json")
    provider_game_id = "٠٠٩٢٥٠٠١٢٣"
    _first_schedule_game(payload)["gameId"] = provider_game_id

    with pytest.raises(ProviderSchemaError) as caught:
        parse_schedule(_schedule_document(payload))

    assert caught.value.code == "schema_mismatch"
    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"
    assert provider_game_id not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_schedule_rejects_unknown_game_type_with_stable_safe_error() -> None:
    payload = _fixture("schedule_2025-26.json")
    game = _first_schedule_game(payload)
    game["gameId"] = "0092500123"
    game["secret"] = "must-not-leak"

    with pytest.raises(ProviderSchemaError) as caught:
        parse_schedule(_schedule_document(payload))

    assert caught.value.code == "unknown_game_type"
    assert str(caught.value) == "ProviderSchemaError(code=unknown_game_type)"
    assert "must-not-leak" not in repr(caught.value)


def test_box_score_maps_team_and_player_traditional_v3_fields_exactly() -> None:
    payload = _fixture("box_score_0022500123.json")
    before = deepcopy(payload)

    box_score = parse_box_score(_box_score_document(payload))

    assert box_score.game_id == GameId("0022500123")
    assert (box_score.home_team_id, box_score.away_team_id) == (10, 20)
    assert (box_score.home_score, box_score.away_score) == (120, 110)
    assert [row.team.team_id for row in box_score.team_rows] == [10, 20]
    assert box_score.team_rows[0].statistics == TraditionalStats(
        seconds_played=14_400,
        field_goals_made=45,
        field_goals_attempted=90,
        field_goals_percentage=Decimal("0.5"),
        three_pointers_made=15,
        three_pointers_attempted=40,
        three_pointers_percentage=Decimal("0.375"),
        free_throws_made=15,
        free_throws_attempted=18,
        free_throws_percentage=Decimal("0.833"),
        rebounds_offensive=10,
        rebounds_defensive=35,
        rebounds_total=45,
        assists=30,
        steals=8,
        blocks=6,
        turnovers=12,
        fouls_personal=18,
        points=120,
        plus_minus=Decimal("10"),
    )

    starter = box_score.player_rows[0]
    assert starter.player.player_id == 101
    assert (starter.player.first_name, starter.player.family_name, starter.player.full_name) == (
        "Nikola",
        "Jokic",
        "Nikola Jokic",
    )
    assert starter.team_id == 10
    assert starter.position == "C"
    assert starter.jersey_number == "15"
    assert starter.starter is True
    assert starter.played is True
    assert starter.comment is None
    assert starter.statistics == TraditionalStats(
        seconds_played=2_052,
        field_goals_made=10,
        field_goals_attempted=18,
        field_goals_percentage=Decimal("0.555"),
        three_pointers_made=2,
        three_pointers_attempted=5,
        three_pointers_percentage=Decimal("0.4"),
        free_throws_made=6,
        free_throws_attempted=7,
        free_throws_percentage=Decimal("0.857"),
        rebounds_offensive=3,
        rebounds_defensive=9,
        rebounds_total=12,
        assists=11,
        steals=2,
        blocks=1,
        turnovers=4,
        fouls_personal=2,
        points=28,
        plus_minus=Decimal("10"),
    )

    dnp = box_score.player_rows[1]
    assert dnp.player.player_id == 102
    assert dnp.position is None
    assert dnp.starter is False
    assert dnp.played is False
    assert dnp.comment == "DNP - Coach's Decision"
    assert dnp.statistics == TraditionalStats.empty()
    assert payload == before


def test_play_by_play_orders_actions_and_preserves_all_event_fields() -> None:
    payload = _fixture("play_by_play_0022500123.json")
    before = deepcopy(payload)

    events = parse_play_by_play(_play_by_play_document(payload))

    assert [event.action_number for event in events] == [1, 2, 3]
    assert events[0] == PlayByPlayEvent(
        game_id=GameId("0022500123"),
        action_number=1,
        action_id=10,
        period=1,
        clock="PT11M59.00S",
        seconds_remaining_period_ms=719_000,
        team_id=10,
        person_id=101,
        action_type="2pt",
        sub_type="Jump Shot",
        description="Jokic 27' Jump Shot (3 PTS)",
        home_score=3,
        away_score=0,
        shot_result="Made",
        shot_distance=27,
        legacy_x=-120,
        legacy_y=235,
        location="h",
        is_field_goal=True,
        points_total=3,
        video_available=True,
    )
    substitution = events[1]
    assert substitution.action_type == "substitution"
    assert substitution.sub_type == "out"
    assert substitution.description == "SUB: Cancar FOR Jokic"
    assert substitution.person_id == 102
    assert events[2].action_id is None
    assert events[2].team_id is None
    assert events[2].person_id is None
    assert events[2].home_score is None
    assert events[2].away_score is None
    assert events[2].seconds_remaining_period_ms == 0
    assert payload == before


def test_play_by_play_accepts_overtime_clock_without_box_score_player_validation() -> None:
    payload = _fixture("play_by_play_0022500123.json")
    game = cast(dict[str, object], payload["game"])
    actions = cast(list[dict[str, object]], game["actions"])
    overtime = deepcopy(actions[0])
    overtime["actionNumber"] = 4
    overtime["actionId"] = 40
    overtime["period"] = 5
    overtime["clock"] = "PT5M00.00S"
    overtime["personId"] = 999999
    actions.append(overtime)

    events = parse_play_by_play(_play_by_play_document(payload))

    assert events[-1].period == 5
    assert events[-1].seconds_remaining_period_ms == 300_000
    assert events[-1].person_id == 999999


@pytest.mark.parametrize("clock", ["-PT1M00S", "PT-1M00S", "11:59", "PT1M60S", "PT1.5M"])
def test_clock_parser_rejects_negative_or_malformed_provider_values(clock: str) -> None:
    with pytest.raises(ProviderSchemaError) as caught:
        parse_clock_milliseconds(clock)

    assert caught.value.code == "schema_mismatch"
    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"


def test_utc_parser_rejects_boundary_offset_overflow_without_exception_context() -> None:
    provider_timestamp = "0001-01-01T00:00:00+14:00"

    with pytest.raises(ProviderSchemaError) as caught:
        parse_utc_datetime(provider_timestamp)

    assert caught.value.code == "schema_mismatch"
    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"
    assert provider_timestamp not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_duration_parser_rejects_oversized_component_without_provider_leak() -> None:
    provider_duration = f"PT{'9' * 5_000}M00S"

    with pytest.raises(ProviderSchemaError) as caught:
        parse_clock_milliseconds(provider_duration)

    assert caught.value.code == "schema_mismatch"
    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"
    assert provider_duration not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("parser", "payload", "endpoint", "resource_key"),
    [
        (parse_schedule, {"secret": "schedule-secret"}, "scheduleleaguev2", "schedule:2025-26"),
        (
            parse_box_score,
            {"secret": "box-secret"},
            "boxscoretraditionalv3",
            "box_score:0022500123",
        ),
        (
            parse_play_by_play,
            {"secret": "pbp-secret"},
            "playbyplayv3",
            "play_by_play:0022500123",
        ),
    ],
)
def test_missing_required_nested_keys_raise_safe_schema_mismatch(
    parser: Callable[[SourceDocument], object],
    payload: JsonObject,
    endpoint: str,
    resource_key: str,
) -> None:
    document = _document(payload, endpoint=endpoint, resource_key=resource_key)

    with pytest.raises(ProviderSchemaError) as caught:
        parser(document)

    assert caught.value.code == "schema_mismatch"
    assert str(caught.value) == "ProviderSchemaError(code=schema_mismatch)"
    assert "secret" not in repr(caught.value)


def test_conversion_failure_discards_sensitive_provider_exception_context() -> None:
    payload = _fixture("schedule_2025-26.json")
    _first_schedule_game(payload)["gameDateTimeUTC"] = "must-not-leak"

    with pytest.raises(ProviderSchemaError) as caught:
        parse_schedule(_schedule_document(payload))

    assert caught.value.code == "schema_mismatch"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "must-not-leak" not in repr(caught.value)


@pytest.mark.parametrize(
    ("parser", "fixture_name", "endpoint", "resource_key"),
    [
        (
            parse_schedule,
            "schedule_2025-26.json",
            "scheduleleaguev2",
            "schedule:2024-25",
        ),
        (
            parse_box_score,
            "box_score_0022500123.json",
            "boxscoretraditionalv3",
            "box_score:0022500999",
        ),
        (
            parse_play_by_play,
            "play_by_play_0022500123.json",
            "playbyplayv3",
            "play_by_play:0022500999",
        ),
    ],
)
def test_mappers_reject_mismatched_resource_identity(
    parser: Callable[[SourceDocument], object],
    fixture_name: str,
    endpoint: str,
    resource_key: str,
) -> None:
    document = _document(_fixture(fixture_name), endpoint=endpoint, resource_key=resource_key)

    with pytest.raises(ProviderSchemaError) as caught:
        parser(document)

    assert caught.value.code == "schema_mismatch"


def test_mapper_rejects_wrong_endpoint_and_source_identity() -> None:
    payload = _fixture("schedule_2025-26.json")

    for document in (
        _document(payload, endpoint="playbyplayv3", resource_key="schedule:2025-26"),
        _document(
            payload,
            endpoint="scheduleleaguev2",
            resource_key="schedule:2025-26",
            source="other-provider",
        ),
    ):
        with pytest.raises(ProviderSchemaError) as caught:
            parse_schedule(document)
        assert caught.value.code == "schema_mismatch"
