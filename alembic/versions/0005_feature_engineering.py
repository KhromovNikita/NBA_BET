"""Add PostgreSQL control tables for feature engineering."""

from collections.abc import Sequence

from alembic import op
from nba_live_totals.infrastructure.postgres.schema import (
    feature_build_resources,
    feature_build_runs,
    feature_snapshot_attempts,
    feature_snapshot_dependencies,
)

revision: str = "0005_feature_engineering"
down_revision: str | None = "0004_lineup_reconstruction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        r"""
        CREATE FUNCTION feature_issue_codes_valid(issue_codes jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
        BEGIN
            IF jsonb_typeof(issue_codes) <> 'array'
               OR jsonb_array_length(issue_codes) > 32 THEN
                RETURN false;
            END IF;
            RETURN NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(issue_codes) AS item
                WHERE jsonb_typeof(item) <> 'string'
                   OR NOT (item #>> '{}') ~ '^[a-z][a-z0-9_]{0,63}$'
            ) AND (
                SELECT count(*) = count(DISTINCT item)
                FROM jsonb_array_elements_text(issue_codes) AS item
            );
        END;
        $$
        """
    )
    feature_build_runs.create(bind, checkfirst=False)
    feature_snapshot_attempts.create(bind, checkfirst=False)
    feature_build_resources.create(bind, checkfirst=False)
    feature_snapshot_dependencies.create(bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    feature_snapshot_dependencies.drop(bind, checkfirst=False)
    feature_build_resources.drop(bind, checkfirst=False)
    feature_snapshot_attempts.drop(bind, checkfirst=False)
    feature_build_runs.drop(bind, checkfirst=False)
    op.execute("DROP FUNCTION feature_issue_codes_valid(jsonb)")
