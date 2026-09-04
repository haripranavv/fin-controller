"""auth (User/UserSession) + Batch.user_id

Same create_all() pattern as 0001 (checkfirst=True is create_all's
default, so this only creates the tables that don't exist yet - "users",
"user_sessions" - and leaves every existing table, including "batches"
itself, untouched). The one real ALTER here is the new nullable
batches.user_id column for ownership/isolation.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05
"""
import sqlalchemy as sa
from alembic import op

from app.db.session import Base

# Import so the User/UserSession model classes register on Base.metadata
# before create_all() runs.
from app.models import auth  # noqa: F401

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[auth.User.__table__, auth.UserSession.__table__])  # only creates users/user_sessions
    op.add_column("batches", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_batches_user_id", "batches", ["user_id"])
    op.create_foreign_key("fk_batches_user_id_users", "batches", "users", ["user_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_batches_user_id_users", "batches", type_="foreignkey")
    op.drop_index("ix_batches_user_id", table_name="batches")
    op.drop_column("batches", "user_id")
    op.drop_table("user_sessions")
    op.drop_table("users")
