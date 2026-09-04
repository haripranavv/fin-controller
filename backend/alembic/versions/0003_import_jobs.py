"""import jobs (ImportJob/ImportJobFile)

Same create_all() pattern as 0001/0002 (checkfirst=True is create_all's
default, so this only creates the tables that don't exist yet -
"import_jobs", "import_job_files"). Depends on 0002: ImportJob.user_id is
a nullable FK to users.id, so the `auth` module must be imported here too
- not to create anything from it (0002 already did), only so that
Base.metadata has "users" registered for this FK to resolve.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05
"""
from alembic import op

from app.db.session import Base

# `auth` registers "users" (referenced by ImportJob.user_id's FK) on
# Base.metadata; `import_job` registers the two tables this migration
# actually creates.
from app.models import auth, import_job  # noqa: F401

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[import_job.ImportJob.__table__, import_job.ImportJobFile.__table__])


def downgrade() -> None:
    op.drop_table("import_job_files")
    op.drop_table("import_jobs")
