"""initial schema

Bootstraps every table from the current SQLAlchemy model metadata (see
app/models/) instead of a hand-written sequence of op.create_table calls.

Why: this migration was authored without a reachable Postgres instance to
run `alembic revision --autogenerate` against (no Docker/Postgres available
in the dev sandbox at the time). Driving create_all() off the actual model
metadata guarantees the migration can't drift from the models it was written
alongside — a hand-transcribed CREATE TABLE sequence could not offer that
guarantee. This has NOT been executed against a real Postgres instance yet;
run `alembic upgrade head` after `docker compose up -d` to verify it, then
report back if anything fails.

All *subsequent* migrations should go back to the normal
`alembic revision --autogenerate` workflow now that a target DB exists.

Revision ID: 0001
Revises:
Create Date: 2026-08-31
"""
from alembic import op
from sqlalchemy import text

from app.core.config import settings
from app.db.groundtruth_session import GroundTruthBase
from app.db.session import Base

# Import model modules so they register on their Base's metadata before we
# create_all from it.
from app.models import financial, groundtruth, operational  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    bind.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.ground_truth_schema}"))
    GroundTruthBase.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    GroundTruthBase.metadata.drop_all(bind=bind)
    bind.execute(text(f"DROP SCHEMA IF EXISTS {settings.ground_truth_schema} CASCADE"))
    Base.metadata.drop_all(bind=bind)
