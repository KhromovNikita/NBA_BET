"""Add PostgreSQL tables for historical NBA ingestion."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_historical_ingestion"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def _traditional_stat_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("seconds_played", sa.Integer()),
        sa.Column("field_goals_made", sa.Integer()),
        sa.Column("field_goals_attempted", sa.Integer()),
        sa.Column("field_goals_percentage", sa.Numeric(8, 6)),
        sa.Column("three_pointers_made", sa.Integer()),
        sa.Column("three_pointers_attempted", sa.Integer()),
        sa.Column("three_pointers_percentage", sa.Numeric(8, 6)),
        sa.Column("free_throws_made", sa.Integer()),
        sa.Column("free_throws_attempted", sa.Integer()),
        sa.Column("free_throws_percentage", sa.Numeric(8, 6)),
        sa.Column("rebounds_offensive", sa.Integer()),
        sa.Column("rebounds_defensive", sa.Integer()),
        sa.Column("rebounds_total", sa.Integer()),
        sa.Column("assists", sa.Integer()),
        sa.Column("steals", sa.Integer()),
        sa.Column("blocks", sa.Integer()),
        sa.Column("turnovers", sa.Integer()),
        sa.Column("fouls_personal", sa.Integer()),
        sa.Column("points", sa.Integer()),
        sa.Column("plus_minus", sa.Numeric(8, 2)),
    )


def _traditional_stat_checks(table_name: str) -> tuple[sa.CheckConstraint, ...]:
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
        sa.CheckConstraint(
            f"{name} IS NULL OR {name} >= 0",
            name=op.f(f"ck_{table_name}_nonnegative_{name}"),
        )
        for name in nonnegative_columns
    ]
    checks.extend(
        sa.CheckConstraint(
            f"{name} IS NULL OR ({name} >= 0 AND {name} <= 1)",
            name=op.f(f"ck_{table_name}_valid_{name}"),
        )
        for name in (
            "field_goals_percentage",
            "three_pointers_percentage",
            "free_throws_percentage",
        )
    )
    checks.extend(
        (
            sa.CheckConstraint(
                "field_goals_made IS NULL OR field_goals_attempted IS NULL "
                "OR field_goals_made <= field_goals_attempted",
                name=op.f(f"ck_{table_name}_field_goals_made_within_attempts"),
            ),
            sa.CheckConstraint(
                "three_pointers_made IS NULL OR three_pointers_attempted IS NULL "
                "OR three_pointers_made <= three_pointers_attempted",
                name=op.f(f"ck_{table_name}_three_pointers_made_within_attempts"),
            ),
            sa.CheckConstraint(
                "free_throws_made IS NULL OR free_throws_attempted IS NULL "
                "OR free_throws_made <= free_throws_attempted",
                name=op.f(f"ck_{table_name}_free_throws_made_within_attempts"),
            ),
        )
    )
    return tuple(checks)


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("season", sa.String(length=7), nullable=False),
        sa.Column("season_type", sa.String(length=32), nullable=False),
        sa.Column("season_start_year", sa.Integer(), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "season ~ '^[0-9]{4}-[0-9]{2}$'",
            name=op.f("ck_seasons_season_format"),
        ),
        sa.CheckConstraint(
            "season_start_year > 0",
            name=op.f("ck_seasons_positive_start_year"),
        ),
        sa.CheckConstraint(
            "season_type IN ('regular_season', 'playoffs')",
            name=op.f("ck_seasons_season_type_allowed"),
        ),
        sa.PrimaryKeyConstraint("season", "season_type", name="pk_seasons"),
    )
    op.create_table(
        "teams",
        sa.Column("team_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tricode", sa.String(length=3), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("team_id > 0", name=op.f("ck_teams_positive_team_id")),
        sa.PrimaryKeyConstraint("team_id", name="pk_teams"),
    )
    op.create_index("ix_teams_tricode", "teams", ["tricode"], unique=False)
    op.create_table(
        "players",
        sa.Column("player_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("family_name", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("player_id > 0", name=op.f("ck_players_positive_player_id")),
        sa.PrimaryKeyConstraint("player_id", name="pk_players"),
    )
    op.create_table(
        "games",
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column("season", sa.String(length=7), nullable=False),
        sa.Column("season_type", sa.String(length=32), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("home_team_id", sa.BigInteger(), nullable=False),
        sa.Column("away_team_id", sa.BigInteger(), nullable=False),
        sa.Column("home_score", sa.Integer()),
        sa.Column("away_score", sa.Integer()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("arena_name", sa.Text()),
        sa.Column("arena_city", sa.Text()),
        sa.Column("arena_state", sa.Text()),
        sa.CheckConstraint(
            "game_id ~ '^[0-9]{10}$'",
            name=op.f("ck_games_game_id_format"),
        ),
        sa.CheckConstraint(
            "home_team_id > 0",
            name=op.f("ck_games_positive_home_team_id"),
        ),
        sa.CheckConstraint(
            "away_team_id > 0",
            name=op.f("ck_games_positive_away_team_id"),
        ),
        sa.CheckConstraint(
            "home_team_id <> away_team_id",
            name=op.f("ck_games_distinct_teams"),
        ),
        sa.CheckConstraint(
            "home_score IS NULL OR home_score >= 0",
            name=op.f("ck_games_nonnegative_home_score"),
        ),
        sa.CheckConstraint(
            "away_score IS NULL OR away_score >= 0",
            name=op.f("ck_games_nonnegative_away_score"),
        ),
        sa.CheckConstraint(
            "status <> 'final' OR (home_score IS NOT NULL AND away_score IS NOT NULL)",
            name=op.f("ck_games_final_scores_present"),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'live', 'final', 'postponed', 'cancelled', 'unknown')",
            name=op.f("ck_games_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("season", "season_type"),
            ("seasons.season", "seasons.season_type"),
            name="fk_games_season_seasons",
        ),
        sa.ForeignKeyConstraint(
            ("home_team_id",),
            ("teams.team_id",),
            name="fk_games_home_team_id_teams",
        ),
        sa.ForeignKeyConstraint(
            ("away_team_id",),
            ("teams.team_id",),
            name="fk_games_away_team_id_teams",
        ),
        sa.PrimaryKeyConstraint("game_id", name="pk_games"),
    )
    op.create_index(
        "ix_games_season_season_type_game_date",
        "games",
        ["season", "season_type", "game_date"],
        unique=False,
    )
    op.create_index("ix_games_game_date", "games", ["game_date"], unique=False)
    op.create_table(
        "team_box_scores",
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        *_traditional_stat_columns(),
        sa.CheckConstraint(
            "game_id ~ '^[0-9]{10}$'",
            name=op.f("ck_team_box_scores_game_id_format"),
        ),
        sa.CheckConstraint(
            "team_id > 0",
            name=op.f("ck_team_box_scores_positive_team_id"),
        ),
        *_traditional_stat_checks("team_box_scores"),
        sa.ForeignKeyConstraint(
            ("game_id",),
            ("games.game_id",),
            name="fk_team_box_scores_game_id_games",
        ),
        sa.ForeignKeyConstraint(
            ("team_id",),
            ("teams.team_id",),
            name="fk_team_box_scores_team_id_teams",
        ),
        sa.PrimaryKeyConstraint("game_id", "team_id", name="pk_team_box_scores"),
    )
    op.create_index(
        "ix_team_box_scores_team_id_game_id",
        "team_box_scores",
        ["team_id", "game_id"],
        unique=False,
    )
    op.create_table(
        "player_box_scores",
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Text()),
        sa.Column("jersey_number", sa.Text(), nullable=False),
        sa.Column("starter", sa.Boolean(), nullable=False),
        sa.Column("played", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text()),
        *_traditional_stat_columns(),
        sa.CheckConstraint(
            "game_id ~ '^[0-9]{10}$'",
            name=op.f("ck_player_box_scores_game_id_format"),
        ),
        sa.CheckConstraint(
            "player_id > 0",
            name=op.f("ck_player_box_scores_positive_player_id"),
        ),
        sa.CheckConstraint(
            "team_id > 0",
            name=op.f("ck_player_box_scores_positive_team_id"),
        ),
        *_traditional_stat_checks("player_box_scores"),
        sa.ForeignKeyConstraint(
            ("game_id",),
            ("games.game_id",),
            name="fk_player_box_scores_game_id_games",
        ),
        sa.ForeignKeyConstraint(
            ("player_id",),
            ("players.player_id",),
            name="fk_player_box_scores_player_id_players",
        ),
        sa.ForeignKeyConstraint(
            ("team_id",),
            ("teams.team_id",),
            name="fk_player_box_scores_team_id_teams",
        ),
        sa.PrimaryKeyConstraint(
            "game_id",
            "player_id",
            name="pk_player_box_scores",
        ),
    )
    op.create_index(
        "ix_player_box_scores_player_id_game_id",
        "player_box_scores",
        ["player_id", "game_id"],
        unique=False,
    )
    op.create_index(
        "ix_player_box_scores_game_id_team_id",
        "player_box_scores",
        ["game_id", "team_id"],
        unique=False,
    )
    op.create_table(
        "play_by_play_events",
        sa.Column("game_id", sa.String(length=10), nullable=False),
        sa.Column("action_number", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer()),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("clock", sa.Text(), nullable=False),
        sa.Column("seconds_remaining_period_ms", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.BigInteger()),
        sa.Column("person_id", sa.BigInteger()),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("sub_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("home_score", sa.Integer()),
        sa.Column("away_score", sa.Integer()),
        sa.Column("shot_result", sa.Text()),
        sa.Column("shot_distance", sa.Integer()),
        sa.Column("legacy_x", sa.Integer()),
        sa.Column("legacy_y", sa.Integer()),
        sa.Column("location", sa.Text()),
        sa.Column("is_field_goal", sa.Boolean(), nullable=False),
        sa.Column("points_total", sa.Integer(), nullable=False),
        sa.Column("video_available", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "game_id ~ '^[0-9]{10}$'",
            name=op.f("ck_play_by_play_events_game_id_format"),
        ),
        sa.CheckConstraint(
            "action_number > 0",
            name=op.f("ck_play_by_play_events_positive_action_number"),
        ),
        sa.CheckConstraint(
            "period > 0",
            name=op.f("ck_play_by_play_events_positive_period"),
        ),
        sa.CheckConstraint(
            "seconds_remaining_period_ms >= 0",
            name=op.f("ck_play_by_play_events_nonnegative_clock_ms"),
        ),
        sa.CheckConstraint(
            "team_id IS NULL OR team_id > 0",
            name=op.f("ck_play_by_play_events_positive_team_id"),
        ),
        sa.CheckConstraint(
            "person_id IS NULL OR person_id > 0",
            name=op.f("ck_play_by_play_events_positive_person_id"),
        ),
        sa.CheckConstraint(
            "home_score IS NULL OR home_score >= 0",
            name=op.f("ck_play_by_play_events_nonnegative_home_score"),
        ),
        sa.CheckConstraint(
            "away_score IS NULL OR away_score >= 0",
            name=op.f("ck_play_by_play_events_nonnegative_away_score"),
        ),
        sa.CheckConstraint(
            "shot_distance IS NULL OR shot_distance >= 0",
            name=op.f("ck_play_by_play_events_nonnegative_shot_distance"),
        ),
        sa.CheckConstraint(
            "points_total >= 0",
            name=op.f("ck_play_by_play_events_nonnegative_points_total"),
        ),
        sa.ForeignKeyConstraint(
            ("game_id",),
            ("games.game_id",),
            name="fk_play_by_play_events_game_id_games",
        ),
        sa.ForeignKeyConstraint(
            ("team_id",),
            ("teams.team_id",),
            name="fk_play_by_play_events_team_id_teams",
        ),
        sa.PrimaryKeyConstraint(
            "game_id",
            "action_number",
            name="pk_play_by_play_events",
        ),
    )
    op.create_index(
        "ix_play_by_play_events_game_id_period_action_number",
        "play_by_play_events",
        ["game_id", "period", "action_number"],
        unique=False,
    )
    op.create_table(
        "source_documents",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("request_parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_source_documents_checksum_sha256_format"),
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_source_documents"),
        sa.UniqueConstraint(
            "source",
            "endpoint",
            "resource_key",
            "checksum_sha256",
            name="uq_source_documents_resource_checksum",
        ),
    )
    op.create_index(
        "ix_source_documents_source_endpoint_resource_key_stored_at",
        "source_documents",
        ["source", "endpoint", "resource_key", sa.literal_column("stored_at DESC")],
        unique=False,
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("discovered", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("succeeded", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
            name=op.f("ck_ingestion_runs_status_allowed"),
        ),
        sa.CheckConstraint(
            "discovered >= 0",
            name=op.f("ck_ingestion_runs_nonnegative_discovered"),
        ),
        sa.CheckConstraint(
            "succeeded >= 0",
            name=op.f("ck_ingestion_runs_nonnegative_succeeded"),
        ),
        sa.CheckConstraint(
            "skipped >= 0",
            name=op.f("ck_ingestion_runs_nonnegative_skipped"),
        ),
        sa.CheckConstraint(
            "failed >= 0",
            name=op.f("ck_ingestion_runs_nonnegative_failed"),
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_ingestion_runs"),
    )
    op.create_table(
        "ingestion_resources",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("latest_document_id", postgresql.UUID(as_uuid=True)),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_error_type", sa.Text()),
        sa.Column("claim_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("claimed_until", sa.DateTime(timezone=True)),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "resource_type IN ('schedule', 'box_score', 'play_by_play')",
            name=op.f("ck_ingestion_resources_resource_type_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name=op.f("ck_ingestion_resources_status_allowed"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_ingestion_resources_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "claim_version >= 0",
            name=op.f("ck_ingestion_resources_nonnegative_claim_version"),
        ),
        sa.ForeignKeyConstraint(
            ("latest_document_id",),
            ("source_documents.document_id",),
            name="fk_ingestion_resources_latest_document",
        ),
        sa.ForeignKeyConstraint(
            ("last_run_id",),
            ("ingestion_runs.run_id",),
            name="fk_ingestion_resources_last_run",
        ),
        sa.PrimaryKeyConstraint(
            "source",
            "resource_type",
            "resource_key",
            name="pk_ingestion_resources",
        ),
    )


def downgrade() -> None:
    op.drop_table("ingestion_resources")
    op.drop_table("ingestion_runs")
    op.drop_table("source_documents")
    op.drop_table("play_by_play_events")
    op.drop_table("player_box_scores")
    op.drop_table("team_box_scores")
    op.drop_table("games")
    op.drop_table("players")
    op.drop_table("teams")
    op.drop_table("seasons")
