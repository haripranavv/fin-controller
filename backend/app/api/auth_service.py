"""Password hashing + session issuance for real, Postgres-persisted
authentication. Pure functions/small helpers over app.models.auth's
User/UserSession - no FastAPI/route concerns here, kept separate so
app/api/routes_auth.py stays thin and this is independently testable.

Scope, deliberately: bcrypt password hashing, opaque random session
tokens stored in the user_sessions table (logout = delete the row). No
JWT, no refresh tokens, no expiry policy, no OAuth, no MFA - see
routes_auth.py's module docstring for why.
"""
from __future__ import annotations

import secrets

import bcrypt
from sqlalchemy.orm import Session

from app.models.auth import User, UserSession

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_user(session: Session, email: str, password: str, *, display_name: str | None = None, is_demo: bool = False) -> User:
    user = User(email=email.strip().lower(), password_hash=hash_password(password), display_name=display_name, is_demo=is_demo)
    session.add(user)
    session.flush()
    return user


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.query(User).filter_by(email=email.strip().lower()).first()


def issue_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(UserSession(token=token, user_id=user.id))
    session.commit()
    return token


def resolve_session(session: Session, token: str) -> User | None:
    if not token:
        return None
    row = session.query(UserSession).filter_by(token=token).first()
    if row is None:
        return None
    return session.query(User).filter_by(id=row.user_id).first()


def invalidate_session(session: Session, token: str) -> None:
    session.query(UserSession).filter_by(token=token).delete()
    session.commit()


def ensure_demo_user(session: Session, email: str, password: str) -> User:
    """Idempotent: the seeded synthetic demo account. Safe to call on
    every app startup - creates it once, leaves it alone afterward (never
    resets the password on an existing row, so a judge who already has a
    session isn't logged out by a redeploy)."""
    existing = get_user_by_email(session, email)
    if existing is not None:
        return existing
    return create_user(session, email, password, display_name="Demo Operator", is_demo=True)
