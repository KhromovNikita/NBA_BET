from collections.abc import Iterable
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from nba_live_totals.infrastructure.metadata import metadata
from nba_live_totals.infrastructure.postgres.schema import (
    games,
    ingestion_resources,
    ingestion_runs,
    play_by_play_events,
    player_box_scores,
    players,
    seasons,
    source_documents,
    team_box_scores,
    teams,
)

EXPECTED_COLUMNS = {
    "seasons": {
        "season",
        "season_type",
        "season_start_year",
        "created_at",
        "updated_at",
    },
    "teams": {"team_id", "city", "name", "tricode", "slug", "created_at", "updated_at"},
    "players": {
        "player_id",
        "first_name",
        "family_name",
        "full_name",
        "created_at",
        "updated_at",
    },
    "games": {
        "game_id",
        "season",
        "season_type",
        "game_date",
        "started_at",
        "home_team_id",
        "away_team_id",
        "home_score",
        "away_score",
        "status",
        "arena_name",
        "arena_city",
        "arena_state",
    },
    "team_box_scores": {
        "game_id",
        "team_id",
        "seconds_played",
        "field_goals_made",
        "field_goals_attempted",
        "field_goals_percentage",
        "three_pointers_made",
        "three_pointers_attempted",
        "three_pointers_percentage",
        "free_throws_made",
        "free_throws_attempted",
        "free_throws_percentage",
        "rebounds_offensive",
        "rebounds_defensive",
        "rebounds_total",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fouls_personal",
        "points",
        "plus_minus",
    },
    "player_box_scores": {
        "game_id",
        "player_id",
        "team_id",
        "position",
        "jersey_number",
        "starter",
        "played",
        "comment",
        "seconds_played",
        "field_goals_made",
        "field_goals_attempted",
        "field_goals_percentage",
        "three_pointers_made",
        "three_pointers_attempted",
        "three_pointers_percentage",
        "free_throws_made",
        "free_throws_attempted",
        "free_throws_percentage",
        "rebounds_offensive",
        "rebounds_defensive",
        "rebounds_total",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fouls_personal",
        "points",
        "plus_minus",
    },
    "play_by_play_events": {
        "game_id",
        "sequence_number",
        "action_number",
        "action_id",
        "period",
        "clock",
        "seconds_remaining_period_ms",
        "team_id",
        "person_id",
        "action_type",
        "sub_type",
        "description",
        "home_score",
        "away_score",
        "shot_result",
        "shot_distance",
        "legacy_x",
        "legacy_y",
        "location",
        "is_field_goal",
        "points_total",
        "video_available",
    },
    "source_documents": {
        "document_id",
        "source",
        "endpoint",
        "resource_key",
        "request_parameters",
        "payload",
        "checksum_sha256",
        "requested_at",
        "stored_at",
    },
    "ingestion_runs": {
        "run_id",
        "request_parameters",
        "started_at",
        "finished_at",
        "status",
        "discovered",
        "succeeded",
        "skipped",
        "failed",
    },
    "ingestion_resources": {
        "source",
        "resource_type",
        "resource_key",
        "status",
        "attempt_count",
        "latest_document_id",
        "applied_document_id",
        "last_run_id",
        "last_error_code",
        "last_error_type",
        "claim_version",
        "claimed_until",
        "created_at",
        "updated_at",
    },
}

EXPECTED_PRIMARY_KEYS = {
    "seasons": ("season", "season_type"),
    "teams": ("team_id",),
    "players": ("player_id",),
    "games": ("game_id",),
    "team_box_scores": ("game_id", "team_id"),
    "player_box_scores": ("game_id", "player_id"),
    "play_by_play_events": ("game_id", "sequence_number"),
    "source_documents": ("document_id",),
    "ingestion_runs": ("run_id",),
    "ingestion_resources": ("source", "resource_type", "resource_key"),
}

EXPECTED_NULLABLE_COLUMNS = {
    "seasons": set(),
    "teams": set(),
    "players": set(),
    "games": {"home_score", "away_score", "arena_name", "arena_city", "arena_state"},
    "team_box_scores": {
        "seconds_played",
        "field_goals_made",
        "field_goals_attempted",
        "field_goals_percentage",
        "three_pointers_made",
        "three_pointers_attempted",
        "three_pointers_percentage",
        "free_throws_made",
        "free_throws_attempted",
        "free_throws_percentage",
        "rebounds_offensive",
        "rebounds_defensive",
        "rebounds_total",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fouls_personal",
        "points",
        "plus_minus",
    },
    "player_box_scores": {
        "position",
        "comment",
        "seconds_played",
        "field_goals_made",
        "field_goals_attempted",
        "field_goals_percentage",
        "three_pointers_made",
        "three_pointers_attempted",
        "three_pointers_percentage",
        "free_throws_made",
        "free_throws_attempted",
        "free_throws_percentage",
        "rebounds_offensive",
        "rebounds_defensive",
        "rebounds_total",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fouls_personal",
        "points",
        "plus_minus",
    },
    "play_by_play_events": {
        "action_id",
        "team_id",
        "person_id",
        "home_score",
        "away_score",
        "shot_result",
        "shot_distance",
        "legacy_x",
        "legacy_y",
        "location",
    },
    "source_documents": set(),
    "ingestion_runs": {"finished_at"},
    "ingestion_resources": {
        "latest_document_id",
        "applied_document_id",
        "last_run_id",
        "last_error_code",
        "last_error_type",
        "claimed_until",
    },
}

EXPECTED_SERVER_DEFAULT_COLUMNS = {
    "seasons": {"created_at", "updated_at"},
    "teams": {"created_at", "updated_at"},
    "players": {"created_at", "updated_at"},
    "games": set(),
    "team_box_scores": set(),
    "player_box_scores": set(),
    "play_by_play_events": set(),
    "source_documents": {"stored_at"},
    "ingestion_runs": {"started_at", "discovered", "succeeded", "skipped", "failed"},
    "ingestion_resources": {
        "status",
        "attempt_count",
        "claim_version",
        "created_at",
        "updated_at",
    },
}

EXPECTED_INDEXES = {
    "teams": {"ix_teams_tricode": ("tricode",)},
    "games": {
        "ix_games_season_season_type_game_date": ("season", "season_type", "game_date"),
        "ix_games_game_date": ("game_date",),
    },
    "team_box_scores": {"ix_team_box_scores_team_id_game_id": ("team_id", "game_id")},
    "player_box_scores": {
        "ix_player_box_scores_player_id_game_id": ("player_id", "game_id"),
        "ix_player_box_scores_game_id_team_id": ("game_id", "team_id"),
    },
    "play_by_play_events": {
        "ix_play_by_play_events_game_id_period_sequence_number": (
            "game_id",
            "period",
            "sequence_number",
        )
    },
    "source_documents": {
        "ix_source_documents_source_endpoint_resource_key_stored_at": (
            "source",
            "endpoint",
            "resource_key",
            "stored_at",
        )
    },
}

TRADITIONAL_CHECK_SUFFIXES = {
    "nonnegative_seconds_played",
    "nonnegative_field_goals_made",
    "nonnegative_field_goals_attempted",
    "nonnegative_three_pointers_made",
    "nonnegative_three_pointers_attempted",
    "nonnegative_free_throws_made",
    "nonnegative_free_throws_attempted",
    "nonnegative_rebounds_offensive",
    "nonnegative_rebounds_defensive",
    "nonnegative_rebounds_total",
    "nonnegative_assists",
    "nonnegative_steals",
    "nonnegative_blocks",
    "nonnegative_turnovers",
    "nonnegative_fouls_personal",
    "nonnegative_points",
    "valid_field_goals_percentage",
    "valid_three_pointers_percentage",
    "valid_free_throws_percentage",
    "field_goals_made_within_attempts",
    "three_pointers_made_within_attempts",
    "free_throws_made_within_attempts",
}

EXPECTED_CHECK_NAMES = {
    "seasons": {
        "ck_seasons_season_format",
        "ck_seasons_positive_start_year",
        "ck_seasons_season_type_allowed",
    },
    "teams": {"ck_teams_positive_team_id"},
    "players": {"ck_players_positive_player_id"},
    "games": {
        "ck_games_game_id_format",
        "ck_games_positive_home_team_id",
        "ck_games_positive_away_team_id",
        "ck_games_distinct_teams",
        "ck_games_nonnegative_home_score",
        "ck_games_nonnegative_away_score",
        "ck_games_final_scores_present",
        "ck_games_status_allowed",
    },
    "team_box_scores": {
        "ck_team_box_scores_game_id_format",
        "ck_team_box_scores_positive_team_id",
        *(f"ck_team_box_scores_{suffix}" for suffix in TRADITIONAL_CHECK_SUFFIXES),
    },
    "player_box_scores": {
        "ck_player_box_scores_game_id_format",
        "ck_player_box_scores_positive_player_id",
        "ck_player_box_scores_positive_team_id",
        *(f"ck_player_box_scores_{suffix}" for suffix in TRADITIONAL_CHECK_SUFFIXES),
    },
    "play_by_play_events": {
        "ck_play_by_play_events_game_id_format",
        "ck_play_by_play_events_positive_sequence_number",
        "ck_play_by_play_events_positive_period",
        "ck_play_by_play_events_nonnegative_clock_ms",
        "ck_play_by_play_events_positive_team_id",
        "ck_play_by_play_events_positive_person_id",
        "ck_play_by_play_events_nonnegative_home_score",
        "ck_play_by_play_events_nonnegative_away_score",
        "ck_play_by_play_events_nonnegative_shot_distance",
        "ck_play_by_play_events_nonnegative_points_total",
    },
    "source_documents": {"ck_source_documents_checksum_sha256_format"},
    "ingestion_runs": {
        "ck_ingestion_runs_status_allowed",
        "ck_ingestion_runs_nonnegative_discovered",
        "ck_ingestion_runs_nonnegative_succeeded",
        "ck_ingestion_runs_nonnegative_skipped",
        "ck_ingestion_runs_nonnegative_failed",
    },
    "ingestion_resources": {
        "ck_ingestion_resources_resource_type_allowed",
        "ck_ingestion_resources_status_allowed",
        "ck_ingestion_resources_nonnegative_attempt_count",
        "ck_ingestion_resources_nonnegative_claim_version",
    },
}


def _column_names(columns: Iterable[Column[Any]]) -> tuple[str, ...]:
    return tuple(column.name for column in columns)


def test_shared_metadata_preserves_the_ten_phase_two_tables() -> None:
    assert set(metadata.tables) >= set(EXPECTED_COLUMNS)
    assert {table.name for table in metadata.sorted_tables} >= set(EXPECTED_COLUMNS)
    assert not set(metadata.tables) & {
        "possessions",
        "predictions",
        "redis_state",
        "clickhouse_possessions",
    }


def test_table_columns_and_primary_keys_match_the_contract() -> None:
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        table = metadata.tables[table_name]
        assert set(table.c.keys()) == expected_columns
        assert _column_names(table.primary_key.columns) == EXPECTED_PRIMARY_KEYS[table_name]
        assert table.primary_key.name == f"pk_{table_name}"
        assert {column.name for column in table.c if column.nullable} == (
            EXPECTED_NULLABLE_COLUMNS[table_name]
        )
        assert {column.name for column in table.c if column.server_default is not None} == (
            EXPECTED_SERVER_DEFAULT_COLUMNS[table_name]
        )


def test_required_indexes_and_foreign_keys_match_the_contract() -> None:
    for table_name, expected_indexes in EXPECTED_INDEXES.items():
        actual_indexes = {
            index.name: _column_names(index.columns)
            for index in metadata.tables[table_name].indexes
        }
        assert actual_indexes == expected_indexes

    source_index = next(iter(source_documents.indexes))
    assert str(source_index.expressions[-1]) == "source_documents.stored_at DESC"

    assert {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
        )
        for constraint in games.foreign_key_constraints
    } == {
        (("season", "season_type"), "seasons"),
        (("home_team_id",), "teams"),
        (("away_team_id",), "teams"),
    }
    assert {foreign_key.column.table.name for foreign_key in team_box_scores.foreign_keys} == {
        "games",
        "teams",
    }
    assert {foreign_key.column.table.name for foreign_key in player_box_scores.foreign_keys} == {
        "games",
        "players",
        "teams",
    }
    assert {
        foreign_key.column.table.name for foreign_key in play_by_play_events.c.team_id.foreign_keys
    } == {"teams"}
    assert not play_by_play_events.c.person_id.foreign_keys
    assert ingestion_resources.c.latest_document_id.references(source_documents.c.document_id)
    assert ingestion_resources.c.applied_document_id.references(source_documents.c.document_id)
    assert ingestion_resources.c.last_run_id.references(ingestion_runs.c.run_id)


def test_postgresql_types_nullability_and_named_constraints_are_stable() -> None:
    for table in (teams, players):
        assert isinstance(table.primary_key.columns.values()[0].type, BigInteger)
        assert table.primary_key.columns.values()[0].autoincrement is False
    assert isinstance(play_by_play_events.c.person_id.type, BigInteger)

    for column in (
        source_documents.c.document_id,
        ingestion_runs.c.run_id,
        ingestion_resources.c.latest_document_id,
        ingestion_resources.c.applied_document_id,
        ingestion_resources.c.last_run_id,
    ):
        assert isinstance(column.type, UUID)

    for column in (
        source_documents.c.request_parameters,
        source_documents.c.payload,
        ingestion_runs.c.request_parameters,
    ):
        assert isinstance(column.type, JSONB)
        assert not column.nullable

    for table in metadata.tables.values():
        for column in table.c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone

    for table in (team_box_scores, player_box_scores):
        for name in (
            "field_goals_percentage",
            "three_pointers_percentage",
            "free_throws_percentage",
        ):
            column_type = table.c[name].type
            assert isinstance(column_type, Numeric)
            assert (column_type.precision, column_type.scale) == (8, 6)
        plus_minus_type = table.c.plus_minus.type
        assert isinstance(plus_minus_type, Numeric)
        assert (plus_minus_type.precision, plus_minus_type.scale) == (8, 2)

    assert games.c.home_score.nullable
    assert games.c.away_score.nullable
    assert player_box_scores.c.position.nullable
    assert player_box_scores.c.comment.nullable
    assert play_by_play_events.c.person_id.nullable
    assert ingestion_runs.c.finished_at.nullable
    assert ingestion_resources.c.claimed_until.nullable

    for table_name, expected_names in EXPECTED_CHECK_NAMES.items():
        assert {
            constraint.name
            for constraint in metadata.tables[table_name].constraints
            if isinstance(constraint, CheckConstraint)
        } == expected_names

    allowed_value_checks = {
        constraint.name: str(constraint.sqltext)
        for table in (seasons, games, ingestion_runs, ingestion_resources)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    assert allowed_value_checks["ck_seasons_season_type_allowed"] == (
        "season_type IN ('regular_season', 'playoffs')"
    )
    assert allowed_value_checks["ck_games_status_allowed"] == (
        "status IN ('scheduled', 'live', 'final', 'postponed', 'cancelled', 'unknown')"
    )
    assert allowed_value_checks["ck_ingestion_runs_status_allowed"] == (
        "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')"
    )
    assert allowed_value_checks["ck_ingestion_resources_resource_type_allowed"] == (
        "resource_type IN ('schedule', 'box_score', 'play_by_play')"
    )
    assert allowed_value_checks["ck_ingestion_resources_status_allowed"] == (
        "status IN ('pending', 'running', 'succeeded', 'failed')"
    )
    assert {
        _column_names(constraint.columns)
        for constraint in source_documents.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_source_documents_resource_checksum"
    } == {("source", "endpoint", "resource_key", "checksum_sha256")}


def test_exported_tables_are_the_shared_metadata_objects() -> None:
    exported = (
        seasons,
        teams,
        players,
        games,
        team_box_scores,
        player_box_scores,
        play_by_play_events,
        source_documents,
        ingestion_runs,
        ingestion_resources,
    )
    assert all(table.metadata is metadata for table in exported)
