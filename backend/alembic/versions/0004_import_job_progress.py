"""import_jobs.current_stage

Adds one nullable column so a running import job can report real,
incrementally-updated progress ("inserting payments (250,000 / 500,000
rows so far)") instead of the UI having nothing to show between
IMPORTING and READY/FAILED for a large job.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("current_stage", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("import_jobs", "current_stage")
