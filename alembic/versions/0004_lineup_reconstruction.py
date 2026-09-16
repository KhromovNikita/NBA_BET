"""Add PostgreSQL control tables for lineup reconstruction."""

from collections.abc import Sequence

from alembic import op
from nba_live_totals.infrastructure.postgres.schema import (
    lineup_rebuild_resources,
    lineup_rebuild_runs,
    lineup_snapshot_attempts,
)

revision: str = "0004_lineup_reconstruction"
down_revision: str | None = "0003_possession_reconstruction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        """
        CREATE FUNCTION lineup_issue_codes_unique(issue_codes jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT count(*) = count(DISTINCT (
                item->>'team_id' || E'\\x1f' || item->>'reason' || E'\\x1f' ||
                item->>'sequence_number' || E'\\x1f' || item->>'boundary_side'
            ))
            FROM jsonb_array_elements(issue_codes) AS item
        $$
        """
    )
    lineup_rebuild_runs.create(bind, checkfirst=False)
    lineup_snapshot_attempts.create(bind, checkfirst=False)
    lineup_rebuild_resources.create(bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    lineup_rebuild_resources.drop(bind, checkfirst=False)
    lineup_snapshot_attempts.drop(bind, checkfirst=False)
    lineup_rebuild_runs.drop(bind, checkfirst=False)
    op.execute("DROP FUNCTION lineup_issue_codes_unique(jsonb)")
