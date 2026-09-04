"""Real application authentication (app/api/routes_auth.py,
app/api/auth_service.py): registration, login, demo login, session
check, logout invalidation, and that the rest of the API is actually
protected now (401 without a valid token) - not the earlier demo-only
in-memory version of this module."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.main import app


@pytest.fixture()
def client(db_session):
    def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _token(resp) -> str:
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# --- registration -----------------------------------------------------------------


def test_register_creates_a_real_user_and_returns_a_session(client):
    resp = client.post("/api/auth/register", json={"email": "new.operator@example.com", "password": "correcthorse"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["email"] == "new.operator@example.com"
    assert body["is_demo"] is False


def test_register_rejects_duplicate_email(client):
    client.post("/api/auth/register", json={"email": "dup@example.com", "password": "correcthorse"})
    resp = client.post("/api/auth/register", json={"email": "dup@example.com", "password": "different1"})
    assert resp.status_code == 409


def test_register_rejects_short_password(client):
    resp = client.post("/api/auth/register", json={"email": "short@example.com", "password": "abc"})
    assert resp.status_code == 422


def test_register_rejects_malformed_email(client):
    resp = client.post("/api/auth/register", json={"email": "not-an-email", "password": "correcthorse"})
    assert resp.status_code == 422


def test_registered_password_is_hashed_not_stored_in_plaintext(client, db_session):
    client.post("/api/auth/register", json={"email": "hash.check@example.com", "password": "correcthorse"})
    from app.models.auth import User
    user = db_session.query(User).filter_by(email="hash.check@example.com").first()
    assert user is not None
    assert user.password_hash != "correcthorse"
    assert user.password_hash.startswith("$2b$")  # bcrypt


# --- login success/failure ---------------------------------------------------------


def test_login_success_after_registration(client):
    client.post("/api/auth/register", json={"email": "loginme@example.com", "password": "correcthorse"})
    resp = client.post("/api/auth/login", json={"email": "loginme@example.com", "password": "correcthorse"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "loginme@example.com"


def test_login_failure_wrong_password(client):
    client.post("/api/auth/register", json={"email": "wrongpw@example.com", "password": "correcthorse"})
    resp = client.post("/api/auth/login", json={"email": "wrongpw@example.com", "password": "incorrect"})
    assert resp.status_code == 401


def test_login_failure_unknown_email(client):
    resp = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "whatever1"})
    assert resp.status_code == 401


# --- demo login (judge never has to register) --------------------------------------


def test_demo_login_never_requires_registration(client):
    resp = client.post("/api/auth/demo-login")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["email"] == settings.demo_email
    assert body["is_demo"] is True


def test_demo_login_is_idempotent_same_account_every_time(client, db_session):
    client.post("/api/auth/demo-login")
    client.post("/api/auth/demo-login")
    from app.models.auth import User
    demo_users = db_session.query(User).filter_by(email=settings.demo_email).all()
    assert len(demo_users) == 1  # not re-created on every click


# --- session / logout ---------------------------------------------------------------


def test_session_valid_after_login(client):
    token = _token(client.post("/api/auth/demo-login"))
    resp = client.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["email"] == settings.demo_email


def test_session_invalid_without_a_real_token(client):
    assert client.get("/api/auth/session", headers={"Authorization": "Bearer not-a-real-token"}).json()["valid"] is False
    assert client.get("/api/auth/session").json()["valid"] is False


def test_logout_invalidates_the_session(client):
    token = _token(client.post("/api/auth/demo-login"))
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    resp = client.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["valid"] is False


# --- protected API -------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/overview"),
    ("GET", "/api/batches"),
    ("GET", "/api/cases?batch_id=x"),
    ("GET", "/api/exceptions?batch_id=x"),
    ("POST", "/api/runs"),
])
def test_protected_endpoints_reject_unauthenticated_access(client, method, path):
    resp = client.request(method, path, json={"dataset_version": "x"} if method == "POST" else None)
    assert resp.status_code == 401


def test_protected_endpoint_rejects_a_garbage_token(client):
    resp = client.get("/api/batches", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_protected_endpoint_accepts_a_real_token(client):
    token = _token(client.post("/api/auth/demo-login"))
    resp = client.get("/api/batches", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
