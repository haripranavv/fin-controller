"""run_batch: processes every order in a batch through run_case, updating
the Batch row's lifecycle (PROJECT_SPEC.md section 5: Batch "represents
one processing run").

Defensive: an unexpected exception in one case is caught, recorded as an
"ERROR" outcome (distinct from RESOLVED/ESCALATED), and processing
continues — one bad case must not silently abort the whole run, and must
not be hidden either (see BatchSummary.errors). Each case runs inside its
own SAVEPOINT (session.begin_nested()) so a failure rolls back only that
case's writes, never a previously-succeeded case still pending commit —
a plain session-wide rollback would have that bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.datagen.models import GeneratedBatch
from app.matcher.reconciler import MatcherRunResult, run_deterministic_matching
from app.models.operational import Batch
from app.orchestrator.case_runner import CaseSummary, run_case
from app.rootcause.client import RootCauseLLMClient


@dataclass
class BatchSummary:
    batch_id: str
    total: int
    resolved: int
    escalated: int
    errors: list[tuple[str, str]] = field(default_factory=list)  # (order_id, exception message)
    cases: list[CaseSummary] = field(default_factory=list)


def get_or_create_batch(session: Session, batch_id: str, dataset_version: str) -> Batch:
    existing = session.query(Batch).filter_by(batch_id=batch_id).first()
    if existing is not None:
        return existing
    batch_row = Batch(batch_id=batch_id, dataset_version=dataset_version, status="created")
    session.add(batch_row)
    session.flush()
    return batch_row


def run_batch(
    session: Session,
    batch: GeneratedBatch,
    ai_client: RootCauseLLMClient,
    *,
    tolerance_paisa: int = 0,
    matcher_result: MatcherRunResult | None = None,
) -> BatchSummary:
    batch_row = get_or_create_batch(session, batch.batch_id, batch.dataset_version)
    batch_row.status = "processing"
    session.commit()

    if matcher_result is None:
        matcher_result = run_deterministic_matching(
            batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions,
        )

    summary = BatchSummary(batch_id=batch.batch_id, total=0, resolved=0, escalated=0)

    for order in batch.orders:
        summary.total += 1
        try:
            with session.begin_nested():
                result = run_case(session, batch, matcher_result, order.order_id, ai_client, tolerance_paisa=tolerance_paisa)
            summary.cases.append(result)
            if result.outcome == "RESOLVED":
                summary.resolved += 1
            elif result.outcome == "ESCALATED":
                summary.escalated += 1
        except Exception as exc:  # noqa: BLE001 — one bad case must not abort the batch, or be hidden
            summary.errors.append((order.order_id, str(exc)))
        session.commit()

    batch_row.status = "completed"
    session.commit()

    return summary
