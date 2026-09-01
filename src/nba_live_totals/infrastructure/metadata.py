from sqlalchemy import MetaData

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

# Alembic imports this module for target_metadata. Importing the schema module
# registers every Phase 2 table on the shared metadata without changing the
# Phase 1 health-checker's public import path.
from nba_live_totals.infrastructure.postgres import schema as _historical_schema  # noqa: E402, F401
