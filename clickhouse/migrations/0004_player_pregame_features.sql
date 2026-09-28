CREATE TABLE IF NOT EXISTS nba.player_pregame_features_v1
(
    snapshot_id UUID,
    insert_version UInt64,
    feature_revision_id FixedString(64),
    feature_input_token_sha256 FixedString(64),
    dependency_input_sha256 FixedString(64),
    dependency_revision_sha256 FixedString(64),
    feature_formula_version LowCardinality(String),
    output_schema_version LowCardinality(String),
    player_output_sha256 FixedString(64),
    feature_output_sha256 FixedString(64),

    game_id FixedString(10),
    season FixedString(7),
    season_start_year UInt16,
    season_type Enum8('regular_season' = 1, 'playoffs' = 2),
    game_date Date,
    started_at DateTime64(3, 'UTC'),
    player_id UInt64,
    team_id UInt64,
    is_home UInt8,
    history_cutoff_started_at DateTime64(3, 'UTC'),
    games_used UInt8,
    seconds_played UInt32,
    estimated_possessions Nullable(Int64),

    minutes_avg Nullable(Int64),
    points_per_100 Nullable(Int64),
    off_rating Nullable(Int64),
    def_rating Nullable(Int64),
    usage_rate Nullable(Int64),
    assist_rate Nullable(Int64),
    turnover_rate Nullable(Int64),
    three_point_attempt_rate Nullable(Int64),
    three_point_percentage Nullable(Int64),
    free_throw_rate Nullable(Int64),
    rebound_rate Nullable(Int64),
    pace_context Nullable(Int64)
)
ENGINE = ReplacingMergeTree(insert_version)
PARTITION BY season_start_year
ORDER BY (game_id, snapshot_id, player_id);
