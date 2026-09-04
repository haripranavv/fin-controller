"""Real application authentication - User + UserSession, persisted in
Postgres. Deliberately scoped (per instruction): no OAuth, no MFA, no
email verification, no password-reset flow, no roles/RBAC - one flat
User table, one token-based Session table, nothing else. Password
hashing is bcrypt (app/api/auth_service.py), never stored in plaintext or
reversibly.
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func

from app.db.session import Base
from app.db.types import BIGINT_PK


class User(Base):
    __tablename__ = "users"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    # The single seeded synthetic demo account ("Use demo account" on the
    # login screen) - not a role/permission flag, purely a UI/README
    # labeling aid.
    is_demo = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserSession(Base):
    """A logged-in session. Deleted on logout ("logout invalidation") -
    row presence IS validity, no separate revoked flag needed."""

    __tablename__ = "user_sessions"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    token = Column(String(128), unique=True, nullable=False, index=True)
    user_id = Column(BIGINT_PK, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
