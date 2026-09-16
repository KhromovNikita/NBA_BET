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
    Column("sequence_number", Integer, primary_key=True),
    Column("action_number", Integer, nullable=False),
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
    CheckConstraint("sequence_number > 0", name="positive_sequence_number"),
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
    "ix_play_by_play_events_game_id_period_sequence_number",
    play_by_play_events.c.game_id,
    play_by_play_events.c.period,
    play_by_play_events.c.sequence_number,
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

_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_SHA256_PATTERN = "^[0-9a-f]{64}$"
_SAFE_IDENTIFIER_PATTERN = "^[a-z][a-z0-9_]{0,63}$"
_VERSION_PATTERN = "^[a-z][a-z0-9._-]{0,127}$"

possession_rebuild_runs = Table(
    "possession_rebuild_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("selectors", JSONB, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("forced_rebuild", Boolean, nullable=False),
    Column("status", String(16), nullable=False),
    Column("discovered", Integer, nullable=False, server_default="0"),
    Column("materialized", Integer, nullable=False, server_default="0"),
    Column("skipped_current", Integer, nullable=False, server_default="0"),
    Column("verified", Integer, nullable=False, server_default="0"),
    Column("failed", Integer, nullable=False, server_default="0"),
    Column("busy", Integer, nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("mode IN ('materialize', 'verify')", name="mode_allowed"),
    CheckConstraint(
        "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
        name="status_allowed",
    ),
    *(
        CheckConstraint(f"{name} >= 0", name=f"nonnegative_{name}")
        for name in (
            "discovered",
            "materialized",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ),
)

possession_snapshot_attempts = Table(
    "possession_snapshot_attempts",
    metadata,
    Column("snapshot_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "game_id",
        String(10),
        ForeignKey("games.game_id", name="fk_possession_snapshot_attempts_game"),
        nullable=False,
    ),
    Column("generation", BigInteger, nullable=False),
    Column("parse_revision_id", CHAR(64), nullable=False),
    Column("input_token_sha256", CHAR(64), nullable=False),
    Column(
        "input_pbp_document_id",
        UUID(as_uuid=True),
        ForeignKey(
            "source_documents.document_id",
            name="fk_possession_snapshot_attempts_input_pbp_document",
        ),
        nullable=False,
    ),
    Column("input_pbp_sha256", CHAR(64), nullable=False),
    Column("input_event_sha256", CHAR(64), nullable=False),
    Column("game_context_sha256", CHAR(64), nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("parser_version", Text, nullable=False),
    Column("rules_version", Text, nullable=False),
    Column("output_schema_version", Text, nullable=False),
    Column("expected_row_count", BigInteger),
    Column("expected_output_sha256", CHAR(64)),
    Column("state", String(16), nullable=False),
    Column(
        "created_by_run_id",
        UUID(as_uuid=True),
        ForeignKey(
            "possession_rebuild_runs.run_id",
            name="fk_possession_snapshot_attempts_created_by_run",
        ),
        nullable=False,
    ),
    Column("created_by_claim_version", BigInteger, nullable=False),
    Column("insert_started_at", DateTime(timezone=True)),
    Column("verified_at", DateTime(timezone=True)),
    Column("published_at", DateTime(timezone=True)),
    Column("safe_error_code", Text),
    Column("safe_error_type", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "game_id",
        "snapshot_id",
        name="uq_possession_snapshot_attempts_game_snapshot",
    ),
    UniqueConstraint(
        "game_id",
        "generation",
        name="uq_possession_snapshot_attempts_game_generation",
    ),
    CheckConstraint(
        "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')",
        name="state_allowed",
    ),
    CheckConstraint(
        f"generation BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="generation_bounds",
    ),
    CheckConstraint(
        f"expected_row_count IS NULL OR expected_row_count BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="expected_row_count_bounds",
    ),
    CheckConstraint(
        f"created_by_claim_version BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="created_by_claim_version_bounds",
    ),
    *(
        CheckConstraint(f"{name} ~ '{_SHA256_PATTERN}'", name=f"{name}_format")
        for name in (
            "parse_revision_id",
            "input_token_sha256",
            "input_pbp_sha256",
            "input_event_sha256",
            "game_context_sha256",
        )
    ),
    CheckConstraint(
        f"expected_output_sha256 IS NULL OR expected_output_sha256 ~ '{_SHA256_PATTERN}'",
        name="expected_output_sha256_format",
    ),
    *(
        CheckConstraint(f"{name} ~ '{_VERSION_PATTERN}'", name=f"{name}_format")
        for name in (
            "normalizer_version",
            "parser_version",
            "rules_version",
            "output_schema_version",
        )
    ),
    *(
        CheckConstraint(
            f"{name} IS NULL OR {name} ~ '{_SAFE_IDENTIFIER_PATTERN}'",
            name=f"{name}_format",
        )
        for name in ("safe_error_code", "safe_error_type")
    ),
    CheckConstraint(
        "(state = 'allocated' AND expected_row_count IS NULL "
        "AND expected_output_sha256 IS NULL AND insert_started_at IS NULL "
        "AND verified_at IS NULL AND published_at IS NULL) OR "
        "(state = 'writing' AND expected_row_count IS NOT NULL "
        "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
        "AND verified_at IS NULL AND published_at IS NULL) OR "
        "(state = 'verified' AND expected_row_count IS NOT NULL "
        "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
        "AND verified_at IS NOT NULL AND published_at IS NULL) OR "
        "(state = 'published' AND expected_row_count IS NOT NULL "
        "AND expected_output_sha256 IS NOT NULL AND insert_started_at IS NOT NULL "
        "AND verified_at IS NOT NULL AND published_at IS NOT NULL) OR "
        "(state IN ('poisoned', 'superseded') AND published_at IS NULL)",
        name="state_fields",
    ),
    CheckConstraint(
        "(state = 'poisoned' AND safe_error_code IS NOT NULL "
        "AND safe_error_type IS NOT NULL) OR "
        "(state <> 'poisoned' AND safe_error_code IS NULL AND safe_error_type IS NULL)",
        name="state_error_fields",
    ),
)

possession_rebuild_resources = Table(
    "possession_rebuild_resources",
    metadata,
    Column(
        "game_id",
        String(10),
        ForeignKey("games.game_id", name="fk_possession_rebuild_resources_game"),
        primary_key=True,
    ),
    Column("publication_state", String(16), nullable=False, server_default="absent"),
    Column("attempt_count", BigInteger, nullable=False, server_default="0"),
    Column("claim_version", BigInteger, nullable=False, server_default="0"),
    Column(
        "last_run_id",
        UUID(as_uuid=True),
        ForeignKey(
            "possession_rebuild_runs.run_id",
            name="fk_possession_rebuild_resources_last_run",
        ),
    ),
    Column("next_generation", BigInteger, nullable=False, server_default="1"),
    Column(
        "active_run_id",
        UUID(as_uuid=True),
        ForeignKey(
            "possession_rebuild_runs.run_id",
            name="fk_possession_rebuild_resources_active_run",
        ),
    ),
    Column("active_claim_version", BigInteger),
    Column("active_snapshot_id", UUID(as_uuid=True)),
    Column("active_parse_revision_id", CHAR(64)),
    Column("active_input_token_sha256", CHAR(64)),
    Column("claimed_until", DateTime(timezone=True)),
    Column("published_snapshot_id", UUID(as_uuid=True)),
    Column("published_parse_revision_id", CHAR(64)),
    Column("published_input_token_sha256", CHAR(64)),
    Column("published_at", DateTime(timezone=True)),
    Column("last_issue_codes", JSONB, nullable=False, server_default="'[]'::jsonb"),
    Column("last_error_code", Text),
    Column("last_error_type", Text),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    Column("completed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ("game_id", "active_snapshot_id"),
        (
            "possession_snapshot_attempts.game_id",
            "possession_snapshot_attempts.snapshot_id",
        ),
        name="fk_possession_rebuild_resources_active_snapshot",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ("game_id", "published_snapshot_id"),
        (
            "possession_snapshot_attempts.game_id",
            "possession_snapshot_attempts.snapshot_id",
        ),
        name="fk_possession_rebuild_resources_published_snapshot",
        deferrable=True,
        initially="DEFERRED",
    ),
    CheckConstraint(
        "publication_state IN ('absent', 'current', 'stale', 'invalid')",
        name="publication_state_allowed",
    ),
    CheckConstraint(
        f"attempt_count BETWEEN 0 AND {_MAX_SIGNED_BIGINT}",
        name="attempt_count_bounds",
    ),
    CheckConstraint(
        f"claim_version BETWEEN 0 AND {_MAX_SIGNED_BIGINT}",
        name="claim_version_bounds",
    ),
    CheckConstraint(
        f"next_generation BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="next_generation_bounds",
    ),
    *(
        CheckConstraint(
            f"{name} IS NULL OR {name} ~ '{_SHA256_PATTERN}'",
            name=constraint_name,
        )
        for name, constraint_name in (
            ("active_parse_revision_id", "active_parse_sha256_format"),
            ("active_input_token_sha256", "active_input_sha256_format"),
            ("published_parse_revision_id", "published_parse_sha256_format"),
            ("published_input_token_sha256", "published_input_sha256_format"),
        )
    ),
    *(
        CheckConstraint(
            f"{name} IS NULL OR {name} ~ '{_SAFE_IDENTIFIER_PATTERN}'",
            name=f"{name}_format",
        )
        for name in ("last_error_code", "last_error_type")
    ),
    CheckConstraint(
        "(active_run_id IS NULL AND active_claim_version IS NULL "
        "AND active_snapshot_id IS NULL AND active_parse_revision_id IS NULL "
        "AND active_input_token_sha256 IS NULL AND claimed_until IS NULL) OR "
        "(active_run_id IS NOT NULL AND active_claim_version IS NOT NULL "
        "AND active_snapshot_id IS NOT NULL AND active_parse_revision_id IS NOT NULL "
        "AND active_input_token_sha256 IS NOT NULL AND claimed_until IS NOT NULL)",
        name="active_claim_tuple",
    ),
    CheckConstraint(
        "active_claim_version IS NULL OR "
        f"(active_claim_version BETWEEN 1 AND {_MAX_SIGNED_BIGINT} "
        "AND active_claim_version = claim_version)",
        name="active_claim_fence",
    ),
    CheckConstraint(
        "(published_snapshot_id IS NULL AND published_parse_revision_id IS NULL "
        "AND published_input_token_sha256 IS NULL AND published_at IS NULL) OR "
        "(published_snapshot_id IS NOT NULL AND published_parse_revision_id IS NOT NULL "
        "AND published_input_token_sha256 IS NOT NULL AND published_at IS NOT NULL)",
        name="published_pointer_tuple",
    ),
    CheckConstraint(
        "(publication_state = 'current' AND published_snapshot_id IS NOT NULL) OR "
        "(publication_state = 'absent' AND published_snapshot_id IS NULL) OR "
        "publication_state IN ('stale', 'invalid')",
        name="publication_pointer_state",
    ),
    CheckConstraint(
        "jsonb_typeof(last_issue_codes) = 'array' "
        "AND jsonb_array_length(last_issue_codes) <= 32 "
        "AND NOT last_issue_codes @? '$[*] ? (@.type() != \"object\")' "
        "AND NOT last_issue_codes @? '$[*] ? (!exists(@.code) || "
        '@.code.type() != "string" || '
        '!(@.code like_regex "^[a-z][a-z0-9_]{0,63}$"))\' '
        'AND NOT last_issue_codes @? \'$[*].keyvalue() ? (@.key != "code" '
        '&& @.key != "sequence_number" && @.key != "related_sequence_number")\' '
        "AND NOT last_issue_codes @? '$[*] ? (exists(@.sequence_number) && "
        '(@.sequence_number.type() != "number" || @.sequence_number < 1 || '
        f"@.sequence_number > {_MAX_SIGNED_BIGINT} || "
        "@.sequence_number != @.sequence_number.floor()))' "
        "AND NOT last_issue_codes @? '$[*] ? (exists(@.related_sequence_number) && "
        '(@.related_sequence_number.type() != "number" || '
        "@.related_sequence_number < 1 || "
        f"@.related_sequence_number > {_MAX_SIGNED_BIGINT} || "
        "@.related_sequence_number != @.related_sequence_number.floor()))'",
        name="last_issue_codes_contract",
    ),
    CheckConstraint(
        "(last_error_code IS NULL AND last_error_type IS NULL) OR "
        "(last_error_code IS NOT NULL AND last_error_type IS NOT NULL)",
        name="last_error_tuple",
    ),
)

clickhouse_schema_migrations = Table(
    "clickhouse_schema_migrations",
    metadata,
    Column("migration_id", Text, primary_key=True),
    Column("checksum_sha256", CHAR(64), nullable=False),
    Column("expected_schema_fingerprint_sha256", CHAR(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("applied_at", DateTime(timezone=True)),
    CheckConstraint(
        f"checksum_sha256 ~ '{_SHA256_PATTERN}'",
        name="checksum_sha256_format",
    ),
    CheckConstraint(
        f"expected_schema_fingerprint_sha256 ~ '{_SHA256_PATTERN}'",
        name="fingerprint_sha256_format",
    ),
    CheckConstraint("state IN ('applying', 'applied')", name="state_allowed"),
    CheckConstraint(
        "(state = 'applying' AND applied_at IS NULL) OR "
        "(state = 'applied' AND applied_at IS NOT NULL)",
        name="applied_at_state",
    ),
)

# Phase 4 control plane.  Keep this adjacent to the possession tables: both
# rebuilders use the same durable fencing and publication semantics.
lineup_rebuild_runs = Table(
    "lineup_rebuild_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("selectors", JSONB, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("forced_rebuild", Boolean, nullable=False),
    Column("status", String(16), nullable=False),
    *(
        Column(name, Integer, nullable=False, server_default="0")
        for name in (
            "discovered",
            "materialized",
            "invalid",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("mode IN ('materialize', 'verify')", name="mode_allowed"),
    CheckConstraint(
        "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
        name="status_allowed",
    ),
    *(
        CheckConstraint(f"{name} >= 0", name=f"nonnegative_{name}")
        for name in (
            "discovered",
            "materialized",
            "invalid",
            "skipped_current",
            "verified",
            "failed",
            "busy",
        )
    ),
)

_lineup_metadata = "expected_row_count, expected_output_sha256, expected_issue_codes, result_status"
_lineup_metadata_null = (
    "expected_row_count IS NULL AND expected_output_sha256 IS NULL "
    "AND expected_issue_codes IS NULL AND result_status IS NULL"
)
_lineup_metadata_present = (
    "expected_row_count IS NOT NULL AND expected_output_sha256 IS NOT NULL "
    "AND expected_issue_codes IS NOT NULL AND result_status IS NOT NULL"
)
_lineup_expected_issues = (
    "jsonb_typeof(expected_issue_codes) = 'array' "
    "AND jsonb_array_length(expected_issue_codes) <= 32"
)
_lineup_issue_keys = (
    '"team_id" && @.key != "reason" && @.key != "sequence_number" && @.key != "boundary_side"'
)


def _lineup_issue_codes_contract(column_name: str, *, nullable: bool = False) -> str:
    predicate = (
        f"jsonb_typeof({column_name}) = 'array' "
        f"AND jsonb_array_length({column_name}) <= 32 "
        f"AND NOT {column_name} @? '$[*] ? (@.type() != \"object\")' "
        f"AND NOT {column_name} @? '$[*].keyvalue() ? (@.key != {_lineup_issue_keys})' "
        f"AND NOT {column_name} @? '$[*] ? (!exists(@.team_id) || "
        f'@.team_id.type() != "number" || @.team_id < 1 || '
        f"@.team_id > {_MAX_SIGNED_BIGINT} || @.team_id != @.team_id.floor())' "
        f"AND NOT {column_name} @? '$[*] ? (!exists(@.reason) || "
        f'@.reason.type() != "string" || '
        f'!(@.reason like_regex "{_SAFE_IDENTIFIER_PATTERN}"))\' '
        f"AND NOT {column_name} @? '$[*] ? (!exists(@.sequence_number) || "
        f'@.sequence_number.type() != "number" || @.sequence_number < 1 || '
        f"@.sequence_number > {_MAX_SIGNED_BIGINT} || "
        f"@.sequence_number != @.sequence_number.floor())' "
        f"AND NOT {column_name} @? '$[*] ? (!exists(@.boundary_side) || "
        f'@.boundary_side.type() != "string" || '
        f'(@.boundary_side != "before" && @.boundary_side != "after"))\''
    )
    return f"{column_name} IS NULL OR ({predicate})" if nullable else predicate


_lineup_state_fields = (
    "(state = 'allocated' AND expected_row_count IS NULL AND insert_started_at IS NULL "
    "AND verified_at IS NULL AND published_at IS NULL) OR "
    "(state = 'writing' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL "
    "AND verified_at IS NULL AND published_at IS NULL) OR "
    "(state = 'verified' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL "
    "AND verified_at IS NOT NULL AND published_at IS NULL) OR "
    "(state = 'published' AND expected_row_count IS NOT NULL AND insert_started_at IS NOT NULL "
    "AND verified_at IS NOT NULL AND published_at IS NOT NULL) OR "
    "(state IN ('poisoned', 'superseded') AND published_at IS NULL)"
)
_lineup_state_errors = (
    "(state = 'poisoned' AND safe_error_code IS NOT NULL AND safe_error_type IS NOT NULL) "
    "OR (state <> 'poisoned' AND safe_error_code IS NULL AND safe_error_type IS NULL)"
)
_lineup_active_tuple = (
    "(active_run_id IS NULL AND active_claim_version IS NULL AND active_snapshot_id IS NULL "
    "AND active_lineup_revision_id IS NULL AND active_input_token_sha256 IS NULL "
    "AND claimed_until IS NULL) OR "
    "(active_run_id IS NOT NULL AND active_claim_version IS NOT NULL "
    "AND active_snapshot_id IS NOT NULL "
    "AND active_lineup_revision_id IS NOT NULL AND active_input_token_sha256 IS NOT NULL "
    "AND claimed_until IS NOT NULL)"
)
_lineup_published_tuple = (
    "(published_snapshot_id IS NULL AND published_lineup_revision_id IS NULL "
    "AND published_input_token_sha256 IS NULL AND published_at IS NULL) OR "
    "(published_snapshot_id IS NOT NULL AND published_lineup_revision_id IS NOT NULL "
    "AND published_input_token_sha256 IS NOT NULL AND published_at IS NOT NULL)"
)
lineup_snapshot_attempts = Table(
    "lineup_snapshot_attempts",
    metadata,
    Column("snapshot_id", UUID(as_uuid=True), primary_key=True),
    Column("game_id", String(10), nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("lineup_revision_id", CHAR(64), nullable=False),
    Column("input_token_sha256", CHAR(64), nullable=False),
    Column("input_box_score_document_id", UUID(as_uuid=True), nullable=False),
    Column("input_box_score_sha256", CHAR(64), nullable=False),
    Column("input_pbp_document_id", UUID(as_uuid=True), nullable=False),
    Column("input_pbp_sha256", CHAR(64), nullable=False),
    Column("canonical_player_box_sha256", CHAR(64), nullable=False),
    Column("canonical_event_sha256", CHAR(64), nullable=False),
    Column("game_context_sha256", CHAR(64), nullable=False),
    Column("normalizer_version", Text, nullable=False),
    Column("reconstructor_version", Text, nullable=False),
    Column("rules_version", Text, nullable=False),
    Column("output_schema_version", Text, nullable=False),
    Column("expected_row_count", BigInteger),
    Column("expected_output_sha256", CHAR(64)),
    Column("expected_issue_codes", JSONB),
    Column("result_status", String(16)),
    Column("state", String(16), nullable=False),
    Column("created_by_run_id", UUID(as_uuid=True), nullable=False),
    Column("created_by_claim_version", BigInteger, nullable=False),
    Column("insert_started_at", DateTime(timezone=True)),
    Column("verified_at", DateTime(timezone=True)),
    Column("published_at", DateTime(timezone=True)),
    Column("safe_error_code", Text),
    Column("safe_error_type", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("game_id", "snapshot_id", name="uq_game_snapshot"),
    UniqueConstraint("game_id", "generation", name="uq_game_generation"),
    CheckConstraint(
        "state IN ('allocated', 'writing', 'verified', 'published', 'poisoned', 'superseded')",
        name="state_allowed",
    ),
    CheckConstraint(
        "result_status IS NULL OR result_status IN ('valid', 'invalid')",
        name="result_status_allowed",
    ),
    CheckConstraint(f"generation BETWEEN 1 AND {_MAX_SIGNED_BIGINT}", name="generation_bounds"),
    CheckConstraint(
        f"expected_row_count IS NULL OR expected_row_count BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="expected_row_count_bounds",
    ),
    CheckConstraint(
        f"created_by_claim_version BETWEEN 1 AND {_MAX_SIGNED_BIGINT}",
        name="created_by_claim_version_bounds",
    ),
    *(
        CheckConstraint(f"{n} ~ '{_SHA256_PATTERN}'", name=f"{n}_format")
        for n in (
            "lineup_revision_id",
            "input_token_sha256",
            "input_box_score_sha256",
            "input_pbp_sha256",
            "canonical_player_box_sha256",
            "canonical_event_sha256",
            "game_context_sha256",
        )
    ),
    CheckConstraint(
        f"expected_output_sha256 IS NULL OR expected_output_sha256 ~ '{_SHA256_PATTERN}'",
        name="expected_output_sha256_format",
    ),
    *(
        CheckConstraint(f"{n} ~ '{_VERSION_PATTERN}'", name=f"{n}_format")
        for n in (
            "normalizer_version",
            "reconstructor_version",
            "rules_version",
            "output_schema_version",
        )
    ),
    *(
        CheckConstraint(f"{n} IS NULL OR {n} ~ '{_SAFE_IDENTIFIER_PATTERN}'", name=f"{n}_format")
        for n in ("safe_error_code", "safe_error_type")
    ),
    CheckConstraint(
        f"({_lineup_metadata_null}) OR ({_lineup_metadata_present} AND "
        "((result_status = 'valid' AND expected_issue_codes = '[]'::jsonb) OR "
        "(result_status = 'invalid' AND jsonb_array_length(expected_issue_codes) >= 1)))",
        name="expected_metadata",
    ),
    CheckConstraint(
        _lineup_issue_codes_contract("expected_issue_codes", nullable=True),
        name="expected_issue_codes_contract",
    ),
    CheckConstraint(
        _lineup_state_fields,
        name="state_fields",
    ),
    CheckConstraint(
        _lineup_state_errors,
        name="state_error_fields",
    ),
    ForeignKeyConstraint(("game_id",), ("games.game_id",), name="fk_lineup_snapshot_attempts_game"),
    ForeignKeyConstraint(
        ("input_box_score_document_id",),
        ("source_documents.document_id",),
        name="fk_lineup_snapshot_attempts_input_box_score_document",
    ),
    ForeignKeyConstraint(
        ("input_pbp_document_id",),
        ("source_documents.document_id",),
        name="fk_lineup_snapshot_attempts_input_pbp_document",
    ),
    ForeignKeyConstraint(
        ("created_by_run_id",),
        ("lineup_rebuild_runs.run_id",),
        name="fk_lineup_snapshot_attempts_created_by_run",
    ),
)

lineup_rebuild_resources = Table(
    "lineup_rebuild_resources",
    metadata,
    Column("game_id", String(10), primary_key=True),
    Column("publication_state", String(16), nullable=False, server_default="absent"),
    Column("attempt_count", BigInteger, nullable=False, server_default="0"),
    Column("claim_version", BigInteger, nullable=False, server_default="0"),
    Column("last_run_id", UUID(as_uuid=True)),
    Column("next_generation", BigInteger, nullable=False, server_default="1"),
    Column("active_run_id", UUID(as_uuid=True)),
    Column("active_claim_version", BigInteger),
    Column("active_snapshot_id", UUID(as_uuid=True)),
    Column("active_lineup_revision_id", CHAR(64)),
    Column("active_input_token_sha256", CHAR(64)),
    Column("claimed_until", DateTime(timezone=True)),
    Column("published_snapshot_id", UUID(as_uuid=True)),
    Column("published_lineup_revision_id", CHAR(64)),
    Column("published_input_token_sha256", CHAR(64)),
    Column("published_at", DateTime(timezone=True)),
    Column("last_issue_codes", JSONB, nullable=False, server_default="'[]'::jsonb"),
    Column("last_error_code", Text),
    Column("last_error_type", Text),
    _timestamp_columns[0](),
    _timestamp_columns[1](),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "publication_state IN ('absent', 'current', 'stale', 'invalid')",
        name="publication_state_allowed",
    ),
    *(
        CheckConstraint(f"{n} BETWEEN {low} AND {_MAX_SIGNED_BIGINT}", name=f"{n}_bounds")
        for n, low in (("attempt_count", 0), ("claim_version", 0), ("next_generation", 1))
    ),
    *(
        CheckConstraint(f"{n} IS NULL OR {n} ~ '{_SHA256_PATTERN}'", name=f"{n}_format")
        for n in (
            "active_lineup_revision_id",
            "active_input_token_sha256",
            "published_lineup_revision_id",
            "published_input_token_sha256",
        )
    ),
    *(
        CheckConstraint(f"{n} IS NULL OR {n} ~ '{_SAFE_IDENTIFIER_PATTERN}'", name=f"{n}_format")
        for n in ("last_error_code", "last_error_type")
    ),
    CheckConstraint(
        _lineup_active_tuple,
        name="active_claim_tuple",
    ),
    CheckConstraint(
        f"active_claim_version IS NULL OR (active_claim_version BETWEEN 1 "
        f"AND {_MAX_SIGNED_BIGINT} AND active_claim_version = claim_version)",
        name="active_claim_fence",
    ),
    CheckConstraint(
        _lineup_published_tuple,
        name="published_pointer_tuple",
    ),
    CheckConstraint(
        "(publication_state = 'current' AND published_snapshot_id IS NOT NULL) OR "
        "(publication_state = 'absent' AND published_snapshot_id IS NULL) OR "
        "(publication_state IN ('stale', 'invalid') AND published_snapshot_id IS NOT NULL)",
        name="publication_pointer_state",
    ),
    CheckConstraint(
        _lineup_issue_codes_contract("last_issue_codes"),
        name="last_issue_codes_contract",
    ),
    CheckConstraint(
        "(publication_state = 'current' AND last_issue_codes = '[]'::jsonb) OR "
        "(publication_state = 'invalid' AND jsonb_array_length(last_issue_codes) >= 1) OR "
        "publication_state IN ('absent', 'stale')",
        name="publication_issue_codes_state",
    ),
    CheckConstraint(
        "(last_error_code IS NULL AND last_error_type IS NULL) OR "
        "(last_error_code IS NOT NULL AND last_error_type IS NOT NULL)",
        name="last_error_tuple",
    ),
    ForeignKeyConstraint(("game_id",), ("games.game_id",), name="fk_lineup_rebuild_resources_game"),
    ForeignKeyConstraint(
        ("last_run_id",),
        ("lineup_rebuild_runs.run_id",),
        name="fk_lineup_rebuild_resources_last_run",
    ),
    ForeignKeyConstraint(
        ("active_run_id",),
        ("lineup_rebuild_runs.run_id",),
        name="fk_lineup_rebuild_resources_active_run",
    ),
    ForeignKeyConstraint(
        ("game_id", "active_snapshot_id"),
        ("lineup_snapshot_attempts.game_id", "lineup_snapshot_attempts.snapshot_id"),
        name="fk_lineup_rebuild_resources_active_snapshot",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ("game_id", "published_snapshot_id"),
        ("lineup_snapshot_attempts.game_id", "lineup_snapshot_attempts.snapshot_id"),
        name="fk_lineup_rebuild_resources_published_snapshot",
        deferrable=True,
        initially="DEFERRED",
    ),
)

__all__ = [
    "clickhouse_schema_migrations",
    "games",
    "ingestion_resources",
    "ingestion_runs",
    "lineup_rebuild_resources",
    "lineup_rebuild_runs",
    "lineup_snapshot_attempts",
    "play_by_play_events",
    "player_box_scores",
    "players",
    "possession_rebuild_resources",
    "possession_rebuild_runs",
    "possession_snapshot_attempts",
    "seasons",
    "source_documents",
    "team_box_scores",
    "teams",
]
