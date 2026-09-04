"""Ground truth database access — ISOLATED BY DESIGN.

PROJECT_SPEC.md section 5: "Ground Truth ... must remain isolated from
normal agent execution" and "The agent must never be able to query ground
truth during normal processing."

Enforcement has two layers:

1. Database level: ground truth tables live in their own Postgres schema
   (settings.ground_truth_schema), created explicitly by the initial
   migration, not the default "public" schema the rest of the app uses.
2. Import level: this module, and app.models.groundtruth, must ONLY be
   imported by the synthetic data generator and the evaluation harness.
   Do NOT import this module from the deterministic matcher, verifier,
   divergence tracer, narration extractor, root cause investigator,
   orchestrator, or any API route that serves live case processing.

There is no automated import-linter wired up yet (nothing in the agent
runtime exists as of this milestone). When the orchestrator/tools packages
are added, add a static check (e.g. an import-boundary test) that fails if
any of those modules import this one.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.core.config import settings

groundtruth_engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
GroundTruthSessionLocal = sessionmaker(bind=groundtruth_engine, autoflush=False, autocommit=False, future=True)

GroundTruthBase = declarative_base()


def get_groundtruth_db() -> Generator[Session, None, None]:
    db = GroundTruthSessionLocal()
    try:
        yield db
    finally:
        db.close()
