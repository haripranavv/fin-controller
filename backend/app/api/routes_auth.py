"""Real application authentication - persisted in Postgres
(app.models.auth.User/UserSession), password hashing + session issuance
in app/api/auth_service.py. Deliberately scoped per instruction: no
OAuth, no MFA, no email verification, no password-reset flow, no
roles/RBAC - registration, login, bcrypt hashing, a server-side session
token, logout invalidation, and nothing else.

get_current_user is the dependency every other protected router uses
(see main.py) - unauthenticated access to the rest of /api/* now returns
401, not a silent pass-through like the earlier demo-only version of this
module.

A single seeded "demo" user (settings.demo_email/demo_password) exists so
a judge is never required to register - POST /api/auth/demo-login logs
straight into it. The demo account only ever sees synthetic data: no
uploaded-by-someone-else batch is visible to it any more than to any
other user (app.api.routes_import/routes_batches/routes_cases/
routes_overview/routes_exceptions/routes_runs's ownership checks apply
identically), and it starts with no batches of its own until synthetic
data is generated/imported into it.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api import auth_service
from app.core.config import settings
from app.db.session import get_db
from app.models.auth import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        if not _EMAIL_RE.match(v.strip()):
            raise ValueError("not a valid email address")
        return v

    @field_validator("password")
    @classmethod
    def _password_length(cls, v: str) -> str:
        if len(v) < auth_service.MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {auth_service.MIN_PASSWORD_LENGTH} characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    email: str
    display_name: str | None
    is_demo: bool


class SessionResponse(BaseModel):
    valid: bool
    email: str | None = None
    is_demo: bool = False


def get_current_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> User:
    # The browser's native EventSource (used for the live Agent Activity
    # stream) cannot set custom headers, so it has no way to carry a
    # Bearer token - the Authorization header is checked first (every
    # normal fetch call uses it), falling back to a `?token=` query
    # param only the stream endpoint's URL actually sets. Same session
    # validation either way - a query-string token still has to resolve
    # to a real, unexpired UserSession row.
    bearer = (authorization or "").removeprefix("Bearer ").strip()
    resolved_token = bearer or (token or "").strip()
    user = auth_service.resolve_session(session, resolved_token) if resolved_token else None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest, session: Session = Depends(get_db)) -> AuthResponse:
    if auth_service.get_user_by_email(session, req.email) is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = auth_service.create_user(session, req.email, req.password, display_name=req.display_name)
    session.commit()
    token = auth_service.issue_session(session, user)
    return AuthResponse(token=token, email=user.email, display_name=user.display_name, is_demo=user.is_demo)


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest, session: Session = Depends(get_db)) -> AuthResponse:
    user = auth_service.get_user_by_email(session, req.email)
    if user is None or not auth_service.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth_service.issue_session(session, user)
    return AuthResponse(token=token, email=user.email, display_name=user.display_name, is_demo=user.is_demo)


@router.post("/demo-login", response_model=AuthResponse)
def demo_login(session: Session = Depends(get_db)) -> AuthResponse:
    user = auth_service.ensure_demo_user(session, settings.demo_email, settings.demo_password)
    session.commit()
    token = auth_service.issue_session(session, user)
    return AuthResponse(token=token, email=user.email, display_name=user.display_name, is_demo=user.is_demo)


@router.get("/session", response_model=SessionResponse)
def get_session(authorization: str | None = Header(default=None), session: Session = Depends(get_db)) -> SessionResponse:
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = auth_service.resolve_session(session, token) if token else None
    if user is None:
        return SessionResponse(valid=False)
    return SessionResponse(valid=True, email=user.email, is_demo=user.is_demo)


@router.post("/logout")
def logout(authorization: str | None = Header(default=None), session: Session = Depends(get_db)) -> dict:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token:
        auth_service.invalidate_session(session, token)
    return {"status": "ok"}
