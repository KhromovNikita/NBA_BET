CREATE TABLE IF NOT EXISTS nba.lineup_segments_v1
(
    snapshot_id UUID,
    insert_version UInt64,
    lineup_revision_id FixedString(64),
    normalizer_version LowCardinality(String),
    reconstructor_version LowCardinality(String),
    rules_version LowCardinality(String),
    input_box_score_document_id UUID,
    input_box_score_sha256 FixedString(64),
    input_pbp_document_id UUID,
    input_pbp_sha256 FixedString(64),
    canonical_player_box_sha256 FixedString(64),
    canonical_event_sha256 FixedString(64),
    game_context_sha256 FixedString(64),
    output_schema_version LowCardinality(String),
    output_sha256 FixedString(64),
    result_status Enum8('valid' = 1, 'invalid' = 2),

    game_id FixedString(10),
    season FixedString(7),
    season_start_year UInt16,
    season_type Enum8('regular_season' = 1, 'playoffs' = 2),
    game_date Date,
    home_team_id UInt64,
    away_team_id UInt64,

    segment_number UInt32,
    start_sequence_number UInt32,
    start_boundary_side Enum8('before' = 1, 'after' = 2),
    end_sequence_number UInt32,
    end_boundary_side Enum8('before' = 1, 'after' = 2),
    start_period UInt16,
    end_period UInt16,
    start_seconds_remaining_period_ms UInt32,
    end_seconds_remaining_period_ms UInt32,

    home_status Enum8('valid' = 1, 'invalid' = 2),
    home_player_ids Array(UInt64),
    home_invalid_reason LowCardinality(String),
    home_invalid_since_sequence_number UInt32,
    home_invalid_since_boundary_side Enum8('none' = 0, 'before' = 1, 'after' = 2),
    away_status Enum8('valid' = 1, 'invalid' = 2),
    away_player_ids Array(UInt64),
    away_invalid_reason LowCardinality(String),
    away_invalid_since_sequence_number UInt32,
    away_invalid_since_boundary_side Enum8('none' = 0, 'before' = 1, 'after' = 2),
    is_valid UInt8
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, segment_number);
