"""GET /api/batches — lets the console's batch/dataset picker enumerate
what's actually been run and persisted, instead of hardcoding dataset
names in the frontend."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.common import batch_visibility_filter
from app.api.routes_auth import get_current_user
from app.api.schemas import BatchSummary
from app.db.session import get_db
from app.models.auth import User
from app.models.enums import CaseState
from app.models.operational import Batch, ReconciliationCase

router = APIRouter(prefix="/api/batches", tags=["batches"])


@router.get("", response_model=list[BatchSummary])
def list_batches(session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[BatchSummary]:
    batches = session.query(Batch).filter(batch_visibility_filter(user)).order_by(Batch.created_at.desc()).all()
    out: list[BatchSummary] = []
    for b in batches:
        counts = dict(
            session.query(ReconciliationCase.state, func.count(ReconciliationCase.id))
            .filter(ReconciliationCase.batch_id == b.batch_id)
            .group_by(ReconciliationCase.state)
            .all()
        )
        resolved = counts.get(CaseState.RESOLVED, 0)
        escalated = counts.get(CaseState.ESCALATED, 0)
        total = sum(counts.values())
        out.append(BatchSummary(
            batch_id=b.batch_id, dataset_version=b.dataset_version, status=b.status, created_at=b.created_at,
            total_cases=total, resolved=resolved, escalated=escalated, in_progress=total - resolved - escalated,
        ))
    return out
