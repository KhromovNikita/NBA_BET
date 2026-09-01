"""Shared SQLAlchemy Core schema for historical NBA ingestion."""

from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from nba_live_totals.infrastructure.metadata import metadata

_timestamp_columns = (
    lambda: Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    lambda: Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)


seasons = Table(
    "seasons",
    metadata,
    Column("season", String(7), primary_key=True),
    Column("season_type", String(32), primary_key=True),
    Column("season_start_year", Integer, nullable=False),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    CheckConstraint("season ~ '^[0-9]{4}-[0-9]{2}$'", name="season_format"),
    CheckConstraint("season_start_year > 0", name="positive_start_year"),
    CheckConstraint(
        "season_type IN ('regular_season', 'playoffs')",
        name="season_type_allowed",
    ),
)

teams = Table(
    "teams",
    metadata,
    Column("team_id", BigInteger, primary_key=True, autoincrement=False),
    Column("city", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("tricode", String(3), nullable=False),
    Column("slug", Text, nullable=False),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    CheckConstraint("team_id > 0", name="positive_team_id"),
)
Index("ix_teams_tricode", teams.c.tricode)

players = Table(
    "players",
    metadata,
    Column("player_id", BigInteger, primary_key=True, autoincrement=False),
    Column("first_name", Text, nullable=False),
    Column("family_name", Text, nullable=False),
    Column("full_name", Text, nullable=False),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    CheckConstraint("player_id > 0", name="positive_player_id"),
)

games = Table(
    "games",
    metadata,
    Column("game_id", String(10), primary_key=True),
    Column("season", String(7), nullable=False),
    Column("season_type", String(32), nullable=False),
    Column("game_date", Date, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("home_team_id", BigInteger, ForeignKey("teams.team_id"), nullable=False),
    Column("away_team_id", BigInteger, ForeignKey("teams.team_id"), nullable=False),
    Column("home_score", Integer),
    Column("away_score", Integer),
    Column("status", String(16), nullable=False),
    Column("arena_name", Text),
    Column("arena_city", Text),
    Column("arena_state", Text),
    ForeignKeyConstraint(
        ("season", "season_type"),
        ("seasons.season", "seasons.season_type"),
    ),
    CheckConstraint("game_id ~ '^[0-9]{10}$'", name="game_id_format"),
    CheckConstraint("home_team_id > 0", name="positive_home_team_id"),
    CheckConstraint("away_team_id > 0", name="positive_away_team_id"),
    CheckConstraint("home_team_id <> away_team_id", name="distinct_teams"),
    CheckConstraint("home_score IS NULL OR home_score >= 0", name="nonnegative_home_score"),
    CheckConstraint("away_score IS NULL OR away_score >= 0", name="nonnegative_away_score"),
    CheckConstraint(
        "status <> 'final' OR (home_score IS NOT NULL AND away_score IS NOT NULL)",
        name="final_scores_present",
    ),
    CheckConstraint(
        "status IN ('scheduled', 'live', 'final', 'postponed', 'cancelled', 'unknown')",
        name="status_allowed",
    ),
)
Index(
    "ix_games_season_season_type_game_date",
    games.c.season,
    games.c.season_type,
    games.c.game_date,
)
Index("ix_games_game_date", games.c.game_date)


def _traditional_stat_columns() -> tuple[Column[Any], ...]:
    return (
        Column("seconds_played", Integer),
        Column("field_goals_made", Integer),
        Column("field_goals_attempted", Integer),
        Column("field_goals_percentage", Numeric(8, 6)),
        Column("three_pointers_made", Integer),
        Column("three_pointers_attempted", Integer),
        Column("three_pointers_percentage", Numeric(8, 6)),
        Column("free_throws_made", Integer),
        Column("free_throws_attempted", Integer),
        Column("free_throws_percentage", Numeric(8, 6)),
        Column("rebounds_offensive", Integer),
        Column("rebounds_defensive", Integer),
        Column("rebounds_total", Integer),
        Column("assists", Integer),
        Column("steals", Integer),
        Column("blocks", Integer),
        Column("turnovers", Integer),
        Column("fouls_personal", Integer),
        Column("points", Integer),
        Column("plus_minus", Numeric(8, 2)),
    )


def _traditional_stat_checks() -> tuple[CheckConstraint, ...]:
    nonnegative_columns = (
        "seconds_played",
        "field_goals_made",
        "field_goals_attempted",
        "three_pointers_made",
        "three_pointers_attempted",
        "free_throws_made",
        "free_throws_attempted",
        "rebounds_offensive",
        "rebounds_defensive",
        "rebounds_total",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fouls_personal",
        "points",
    )
    checks = [
        CheckConstraint(f"{name} IS NULL OR {name} >= 0", name=f"nonnegative_{name}")
        for name in nonnegative_columns
    ]
    checks.extend(
        CheckConstraint(
            f"{name} IS NULL OR ({name} >= 0 AND {name} <= 1)",
            name=f"valid_{name}",
        )
        for name in (
            "field_goals_percentage",
            "three_pointers_percentage",
            "free_throws_percentage",
        )
    )
    checks.extend(
        (
            CheckConstraint(
                "field_goals_made IS NULL OR field_goals_attempted IS NULL "
                "OR field_goals_made <= field_goals_attempted",
                name="field_goals_made_within_attempts",
            ),
            CheckConstraint(
                "three_pointers_made IS NULL OR three_pointers_attempted IS NULL "
                "OR three_pointers_made <= three_pointers_attempted",
                name="three_pointers_made_within_attempts",
            ),
            CheckConstraint(
                "free_throws_made IS NULL OR free_throws_attempted IS NULL "
                "OR free_throws_made <= free_throws_attempted",
                name="free_throws_made_within_attempts",
            ),
        )
    )
    return tuple(checks)


team_box_scores = Table(
    "team_box_scores",
    metadata,
    Column("game_id", String(10), ForeignKey("games.game_id"), primary_key=True),
    Column("team_id", BigInteger, ForeignKey("teams.team_id"), primary_key=True),
    *_traditional_stat_columns(),
    CheckConstraint("game_id ~ '^[0-9]{10}$'", name="game_id_format"),
    CheckConstraint("team_id > 0", name="positive_team_id"),
    *_traditional_stat_checks(),
)
Index("ix_team_box_scores_team_id_game_id", team_box_scores.c.team_id, team_box_scores.c.game_id)

player_box_scores = Table(
    "player_box_scores",
    metadata,
    Column("game_id", String(10), ForeignKey("games.game_id"), primary_key=True),
    Column("player_id", BigInteger, ForeignKey("players.player_id"), primary_key=True),
    Column("team_id", BigInteger, ForeignKey("teams.team_id"), nullable=False),
    Column("position", Text),
    Column("jersey_number", Text, nullable=False),
    Column("starter", Boolean, nullable=False),
    Column("played", Boolean, nullable=False),
    Column("comment", Text),
    *_traditional_stat_columns(),
    CheckConstraint("game_id ~ '^[0-9]{10}$'", name="game_id_format"),
    CheckConstraint("player_id > 0", name="positive_player_id"),
    CheckConstraint("team_id > 0", name="positive_team_id"),
    *_traditional_stat_checks(),
)
Index(
    "ix_player_box_scores_player_id_game_id",
    player_box_scores.c.player_id,
    player_box_scores.c.game_id,
)
Index(
    "ix_player_box_scores_game_id_team_id",
    player_box_scores.c.game_id,
    player_box_scores.c.team_id,
)

play_by_play_events = Table(
    "play_by_play_events",
    metadata,
    Column("game_id", String(10), ForeignKey("games.game_id"), primary_key=True),
    Column("action_number", Integer, primary_key=True),
    Column("action_id", Integer),
    Column("period", Integer, nullable=False),
    Column("clock", Text, nullable=False),
    Column("seconds_remaining_period_ms", Integer, nullable=False),
    Column("team_id", BigInteger, ForeignKey("teams.team_id")),
    Column("person_id", BigInteger),
    Column("action_type", Text, nullable=False),
    Column("sub_type", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("home_score", Integer),
    Column("away_score", Integer),
    Column("shot_result", Text),
    Column("shot_distance", Integer),
    Column("legacy_x", Integer),
    Column("legacy_y", Integer),
    Column("location", Text),
    Column("is_field_goal", Boolean, nullable=False),
    Column("points_total", Integer, nullable=False),
    Column("video_available", Boolean, nullable=False),
    CheckConstraint("game_id ~ '^[0-9]{10}$'", name="game_id_format"),
    CheckConstraint("action_number > 0", name="positive_action_number"),
    CheckConstraint("period > 0", name="positive_period"),
    CheckConstraint(
        "seconds_remaining_period_ms >= 0",
        name="nonnegative_clock_ms",
    ),
    CheckConstraint("team_id IS NULL OR team_id > 0", name="positive_team_id"),
    CheckConstraint("person_id IS NULL OR person_id > 0", name="positive_person_id"),
    CheckConstraint("home_score IS NULL OR home_score >= 0", name="nonnegative_home_score"),
    CheckConstraint("away_score IS NULL OR away_score >= 0", name="nonnegative_away_score"),
    CheckConstraint(
        "shot_distance IS NULL OR shot_distance >= 0",
        name="nonnegative_shot_distance",
    ),
    CheckConstraint("points_total >= 0", name="nonnegative_points_total"),
)
Index(
    "ix_play_by_play_events_game_id_period_action_number",
    play_by_play_events.c.game_id,
    play_by_play_events.c.period,
    play_by_play_events.c.action_number,
)

source_documents = Table(
    "source_documents",
    metadata,
    Column("document_id", UUID(as_uuid=True), primary_key=True),
    Column("source", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("resource_key", Text, nullable=False),
    Column("request_parameters", JSONB, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("checksum_sha256", CHAR(64), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "source",
        "endpoint",
        "resource_key",
        "checksum_sha256",
        name="uq_source_documents_resource_checksum",
    ),
    CheckConstraint(
        "checksum_sha256 ~ '^[0-9a-f]{64}$'",
        name="checksum_sha256_format",
    ),
)
Index(
    "ix_source_documents_source_endpoint_resource_key_stored_at",
    source_documents.c.source,
    source_documents.c.endpoint,
    source_documents.c.resource_key,
    source_documents.c.stored_at.desc(),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("request_parameters", JSONB, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", String(16), nullable=False),
    Column("discovered", Integer, nullable=False, server_default="0"),
    Column("succeeded", Integer, nullable=False, server_default="0"),
    Column("skipped", Integer, nullable=False, server_default="0"),
    Column("failed", Integer, nullable=False, server_default="0"),
    CheckConstraint(
        "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
        name="status_allowed",
    ),
    CheckConstraint("discovered >= 0", name="nonnegative_discovered"),
    CheckConstraint("succeeded >= 0", name="nonnegative_succeeded"),
    CheckConstraint("skipped >= 0", name="nonnegative_skipped"),
    CheckConstraint("failed >= 0", name="nonnegative_failed"),
)

ingestion_resources = Table(
    "ingestion_resources",
    metadata,
    Column("source", Text, primary_key=True),
    Column("resource_type", String(16), primary_key=True),
    Column("resource_key", Text, primary_key=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column(
        "latest_document_id",
        UUID(as_uuid=True),
        ForeignKey(
            "source_documents.document_id",
            name="fk_ingestion_resources_latest_document",
        ),
    ),
    Column(
        "applied_document_id",
        UUID(as_uuid=True),
        ForeignKey(
            "source_documents.document_id",
            name="fk_ingestion_resources_applied_document",
        ),
    ),
    Column(
        "last_run_id",
        UUID(as_uuid=True),
        ForeignKey("ingestion_runs.run_id", name="fk_ingestion_resources_last_run"),
    ),
    Column("last_error_code", Text),
    Column("last_error_type", Text),
    Column("claim_version", BigInteger, nullable=False, server_default="0"),
    Column("claimed_until", DateTime(timezone=True)),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    CheckConstraint(
        "resource_type IN ('schedule', 'box_score', 'play_by_play')",
        name="resource_type_allowed",
    ),
    CheckConstraint(
        "status IN ('pending', 'running', 'succeeded', 'failed')",
        name="status_allowed",
    ),
    CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
    CheckConstraint("claim_version >= 0", name="nonnegative_claim_version"),
)

__all__ = [
    "games",
    "ingestion_resources",
    "ingestion_runs",
    "play_by_play_events",
    "player_box_scores",
    "players",
    "seasons",
    "source_documents",
    "team_box_scores",
    "teams",
]
