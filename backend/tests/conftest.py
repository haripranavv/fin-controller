import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base

# Import model modules so their tables register on Base.metadata.
from app.models import financial, operational  # noqa: F401
from app.models import auth  # noqa: F401
from app.models import import_job  # noqa: F401


@pytest.fixture()
def db_session(monkeypatch):
    """In-memory SQLite session for fast model/DDL smoke tests.

    This is NOT a substitute for verifying against real Postgres (see
    README) — it only checks that the models themselves are well-formed and
    behave correctly, using a portable subset of what Postgres supports.

    A couple of routes (app.api.routes_import, app.api.routes_runs) run
    inserts on a background thread against their own fresh session, built
    from a module-level `SessionLocal` they imported by reference - the
    request-scoped `get_db` override alone never reaches that thread. So
    those two modules' own `SessionLocal` names are repointed at THIS
    fixture's engine (same StaticPool connection db_session itself uses)
    for the duration of the test, restored afterwards - without this, a
    background-thread insert would silently land in the real app database
    instead of the test's in-memory one.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import app.api.routes_import as routes_import_module
    import app.api.routes_runs as routes_runs_module

    monkeypatch.setattr(routes_import_module, "SessionLocal", session_local)
    monkeypatch.setattr(routes_runs_module, "SessionLocal", session_local)

    session = session_local()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
