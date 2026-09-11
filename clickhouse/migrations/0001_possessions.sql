CREATE DATABASE IF NOT EXISTS nba;

-- migration-statement-break
CREATE TABLE IF NOT EXISTS nba.possessions_v1
(
    snapshot_id UUID,
    insert_version UInt64,
    parse_revision_id FixedString(64),
    normalizer_version LowCardinality(String),
    parser_version LowCardinality(String),
    rules_version LowCardinality(String),
    input_pbp_document_id UUID,
    input_pbp_sha256 FixedString(64),
    input_event_sha256 FixedString(64),
    game_context_sha256 FixedString(64),
    output_schema_version LowCardinality(String),
    output_sha256 FixedString(64),

    game_id FixedString(10),
    season FixedString(7),
    season_start_year UInt16,
    season_type Enum8('regular_season' = 1, 'playoffs' = 2),
    game_date Date,
    home_team_id UInt64,
    away_team_id UInt64,

    possession_number UInt16,
    offense_possession_number UInt16,
    period UInt16,
    offense_team_id UInt64,
    defense_team_id UInt64,

    start_sequence_number UInt32,
    start_boundary_side Enum8('before' = 1, 'after' = 2),
    end_sequence_number UInt32,
    end_boundary_side Enum8('before' = 1, 'after' = 2),
    start_seconds_remaining_period_ms UInt32,
    end_seconds_remaining_period_ms UInt32,
    seconds_remaining_game_ms UInt32,

    home_score_before UInt16,
    away_score_before UInt16,
    home_score UInt16,
    away_score UInt16,
    score_diff Int16,
    offense_points UInt16,
    defense_points UInt16,
    home_possessions UInt16,
    away_possessions UInt16,

    end_reason Enum8(
        'made_field_goal' = 1,
        'defensive_rebound' = 2,
        'turnover' = 3,
        'final_free_throw' = 4,
        'period_end' = 5,
        'jump_ball_change' = 6,
        'control_award' = 7
    ),
    source_event_count UInt16
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, possession_number);
