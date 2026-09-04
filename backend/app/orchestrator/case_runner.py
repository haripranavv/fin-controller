"""run_case: PROJECT_SPEC.md section 6's bounded state machine for one
order's case, realized in full for the first time — wiring
app.matcher, app.verifier, app.divergence, app.pipeline.known_causes, and
app.rootcause together, all UNCHANGED. Persists a ReconciliationCase, its
Match/Investigation/ExceptionRecord rows, and one AgentEvent per state
transition.

    MATCH_ATTEMPT
      |- no accepted settlement match -> NO_MATCH -> ESCALATED
      |     (narration extraction is not part of this flow — rejected by
      |      a validation experiment; see docs/ARCHITECTURE_NOTES.md)
      `- MATCHED -> VERIFY
              |- PASS -> RESOLVED
              `- FAIL -> DIVERGENCE_TRACE
                     |- unresolved (missing evidence) -> ESCALATED
                     |- known cause (deterministic) -> VERIFY
                     |        |- PASS -> RESOLVED   `- FAIL -> ESCALATED
                     `- no known cause -> ROOT_CAUSE_INVESTIGATE (AI)
                              |- no usable proposal -> ESCALATED
                              `- proposal -> VERIFY
                                       |- PASS -> RESOLVED  `- FAIL -> ESCALATED

Every branch terminates in RESOLVED or ESCALATED (section 6: "No silent
drop. No forced match. No infinite retry."; section 21's Definition of
DONE: "every case reaches RESOLVED or ESCALATED"). There is no loop or
retry anywhere in this function — each case makes exactly one pass.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.datagen.models import GeneratedBatch
from app.divergence.tracer import trace_chain
from app.divergence.types import DivergenceTrace
from app.matcher.reconciler import MatcherRunResult
from app.models.enums import CaseState, DivergenceStage, MatchMethod, RecordType, RootCause, Severity
from app.models.operational import ExceptionRecord, Investigation, Match, ReconciliationCase
from app.orchestrator.events import emit_event
from app.pipeline.assemble import CaseInputs, assemble_case_inputs
from app.pipeline.known_causes import detect_known_cause
from app.rootcause.case import investigate_case
from app.rootcause.client import RootCauseLLMClient
from app.verifier.checks import verify_root_cause_proposal
from app.verifier.types import VerificationResult
from app.verifier.verifier import verify_match


@dataclass
class CaseSummary:
    case_id: str
    order_id: str
    outcome: str  # "RESOLVED" | "ESCALATED"
    reason: str
    final_state: CaseState


def run_case(
    session: Session,
    batch: GeneratedBatch,
    matcher_result: MatcherRunResult,
    order_id: str,
    ai_client: RootCauseLLMClient,
    *,
    tolerance_paisa: int = 0,
) -> CaseSummary:
    case_id = f"case_{order_id}"
    order = next(o for o in batch.orders if o.order_id == order_id)

    case = ReconciliationCase(
        case_id=case_id, batch_id=batch.batch_id, anchor_type=RecordType.ORDER,
        anchor_id=order_id, state=CaseState.INGESTED,
    )
    session.add(case)
    # Real bug found running against Postgres (not caught by the SQLite
    # test suite, which doesn't enforce FK constraints by default): none
    # of AgentEvent/Match/Investigation/ExceptionRecord have an ORM
    # relationship() to ReconciliationCase (only a raw FK column), so
    # SQLAlchemy's flush does not automatically order the case's INSERT
    # before rows that reference it — it can (and did) batch AgentEvent's
    # insert first, violating the FK. This explicit flush forces the case
    # row to exist before anything referencing case_id is added.
    session.flush()
    emit_event(session, case_id, None, CaseState.INGESTED, message="case created")

    def finish(outcome: str, reason: str, final_state: CaseState) -> CaseSummary:
        case.state = final_state
        if outcome == "ESCALATED":
            session.add(ExceptionRecord(
                case_id=case_id, reason=reason, severity=Severity.MEDIUM,
                amount_paisa=abs(order.amount_paisa), status="open",
            ))
        return CaseSummary(case_id=case_id, order_id=order_id, outcome=outcome, reason=reason, final_state=final_state)

    # --- MATCH_ATTEMPT -----------------------------------------------------
    emit_event(session, case_id, CaseState.INGESTED, CaseState.MATCH_ATTEMPT, tool="deterministic_matcher")
    inputs = assemble_case_inputs(batch, matcher_result, order_id)

    if inputs.settlement is None or inputs.settlement_match is None:
        emit_event(session, case_id, CaseState.MATCH_ATTEMPT, CaseState.NO_MATCH, tool="deterministic_matcher",
                    message="no confident settlement candidate found")
        reason = ("no_match: no confident settlement candidate found; narration-assisted re-match was "
                   "evaluated and rejected (see docs/ARCHITECTURE_NOTES.md), so this escalates directly")
        emit_event(session, case_id, CaseState.NO_MATCH, CaseState.ESCALATED, tool="escalate", message=reason)
        return finish("ESCALATED", reason, CaseState.ESCALATED)

    emit_event(
        session, case_id, CaseState.MATCH_ATTEMPT, CaseState.MATCHED, tool="deterministic_matcher",
        output_summary=f"settlement={inputs.settlement.settlement_id} score={inputs.settlement_match.score}",
    )
    _persist_case_matches(session, case_id, inputs)

    # --- VERIFY (first pass: does the matched chain reconcile?) -----------
    emit_event(session, case_id, CaseState.MATCHED, CaseState.VERIFY, tool="constraint_verifier")
    trace = trace_chain(
        inputs.order, inputs.payments, inputs.refunds, inputs.settlement, inputs.bank_txns,
        settlement_group_payments=inputs.settlement_group_payments or inputs.payments,
        settlement_group_refunds=inputs.settlement_group_refunds or inputs.refunds,
        tolerance_paisa=tolerance_paisa,
    )
    settlement_stage = next(s for s in trace.stages if s.stage == "settlement")
    settlement_verification = verify_match(
        inputs.settlement_match, settlement_stage.expected_paisa, settlement_stage.actual_paisa, tolerance_paisa=tolerance_paisa,
    )

    if trace.status == "clean" and settlement_verification.passed:
        emit_event(session, case_id, CaseState.VERIFY, CaseState.RESOLVED, tool="constraint_verifier",
                    message="chain reconciles exactly", verifier_result=_verification_dict(settlement_verification))
        return finish("RESOLVED", "resolved: full order->payment->refund->settlement->bank chain reconciles exactly", CaseState.RESOLVED)

    # --- DIVERGENCE_TRACE ----------------------------------------------------
    fd = trace.first_divergence
    emit_event(
        session, case_id, CaseState.VERIFY, CaseState.DIVERGENCE_TRACE, tool="divergence_tracer",
        message=f"verification failed at '{fd.stage}'" if fd else "verification failed",
        verifier_result=_verification_dict(settlement_verification),
    )

    if trace.status == "unresolved":
        _persist_investigation(session, case_id, trace, status="unresolved")
        reason = f"escalated: unresolved - missing evidence at '{fd.stage}' ({fd.note})"
        emit_event(session, case_id, CaseState.DIVERGENCE_TRACE, CaseState.ESCALATED, tool="escalate", message=reason)
        return finish("ESCALATED", reason, CaseState.ESCALATED)

    # --- known cause? -> root_cause_investigator if not --------------------
    group_refunds = inputs.settlement_group_refunds or inputs.refunds
    # investigate_case() (app.rootcause, unchanged) always calls the AI
    # whenever detect_known_cause returns None, regardless of whether the
    # AI's proposal ends up usable — but its `source` field only says "ai"
    # when the proposal actually clears the confidence gate, reporting
    # "none" for both "AI never attempted" and "AI attempted but declined/
    # errored". Changing that would break milestone 8's own passing tests
    # (source's meaning is asserted there), so this is computed
    # independently here — same precondition investigate_case uses
    # internally — purely so the audit trail can tell these apart.
    ai_was_invoked = detect_known_cause(fd, group_refunds, inputs.bank_txns) is None
    case_result = investigate_case(ai_client, fd, group_refunds, inputs.bank_txns)

    if ai_was_invoked:
        emit_event(session, case_id, CaseState.DIVERGENCE_TRACE, CaseState.ROOT_CAUSE_INVESTIGATE,
                    tool="root_cause_investigator", message=case_result.detail)

    if case_result.proposal is None:
        _persist_investigation(session, case_id, trace, status="unknown")
        reason = f"escalated: divergence at '{fd.stage}' has no known deterministic cause ({case_result.detail})"
        from_state = CaseState.ROOT_CAUSE_INVESTIGATE if ai_was_invoked else CaseState.DIVERGENCE_TRACE
        emit_event(session, case_id, from_state, CaseState.ESCALATED, tool="escalate", message=reason)
        return finish("ESCALATED", reason, CaseState.ESCALATED)

    # --- VERIFY (second pass: does the proposal actually close the gap?) ---
    from_state = CaseState.ROOT_CAUSE_INVESTIGATE if ai_was_invoked else CaseState.DIVERGENCE_TRACE
    emit_event(session, case_id, from_state, CaseState.VERIFY, tool="constraint_verifier",
                message=f"verifying proposed cause '{case_result.proposal.root_cause}' ({case_result.source})")
    proposal_verification = verify_root_cause_proposal(
        case_result.proposal, fd.expected_paisa, fd.actual_paisa, _known_ids(inputs), tolerance_paisa=tolerance_paisa,
    )

    if proposal_verification.passed:
        _persist_investigation(session, case_id, trace, status="verified",
                                root_cause=case_result.proposal.root_cause, confidence=case_result.proposal.confidence)
        emit_event(session, case_id, CaseState.VERIFY, CaseState.RESOLVED, tool="constraint_verifier",
                    message=f"cause '{case_result.proposal.root_cause}' verified", verifier_result=_verification_dict(proposal_verification))
        return finish("RESOLVED", f"resolved: {case_result.source} cause '{case_result.proposal.root_cause}' verified", CaseState.RESOLVED)

    _persist_investigation(session, case_id, trace, status="rejected",
                            root_cause=case_result.proposal.root_cause, confidence=case_result.proposal.confidence)
    reason = f"escalated: proposed cause '{case_result.proposal.root_cause}' ({case_result.source}) failed verification"
    emit_event(session, case_id, CaseState.VERIFY, CaseState.ESCALATED, tool="escalate", message=reason,
                verifier_result=_verification_dict(proposal_verification))
    return finish("ESCALATED", reason, CaseState.ESCALATED)


# --- persistence helpers -----------------------------------------------------------


def _verification_dict(result: VerificationResult) -> dict:
    return {"passed": result.passed, "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.checks]}


def _known_ids(inputs: CaseInputs) -> set[str]:
    ids = {inputs.order.order_id}
    ids.update(p.payment_id for p in inputs.payments)
    ids.update(r.refund_id for r in inputs.refunds)
    ids.update(p.payment_id for p in inputs.settlement_group_payments)
    ids.update(r.refund_id for r in inputs.settlement_group_refunds)
    ids.update(b.bank_txn_id for b in inputs.bank_txns)
    if inputs.settlement is not None:
        ids.add(inputs.settlement.settlement_id)
    return ids


def _persist_case_matches(session: Session, case_id: str, inputs: CaseInputs) -> None:
    sm = inputs.settlement_match
    session.add(Match(
        case_id=case_id, source_type=RecordType(sm.source_type), source_id=sm.source_id,
        target_type=RecordType(sm.target_type), target_id=sm.target_id,
        method=MatchMethod(sm.method), score=sm.score, accepted=sm.accepted,
    ))
    for bm in inputs.bank_matches:
        session.add(Match(
            case_id=case_id, source_type=RecordType(bm.source_type), source_id=bm.source_id,
            target_type=RecordType(bm.target_type), target_id=bm.target_id,
            method=MatchMethod(bm.method), score=bm.score, accepted=bm.accepted,
        ))


def _persist_investigation(
    session: Session, case_id: str, trace: DivergenceTrace, *, status: str,
    root_cause: str | None = None, confidence: float | None = None,
) -> None:
    fd = trace.first_divergence
    session.add(Investigation(
        case_id=case_id,
        divergence_stage=DivergenceStage(fd.stage) if fd else None,
        # Investigation's amount columns are NOT NULL (section 5's schema,
        # unchanged here) — 0 is a defensive placeholder for the
        # "unresolved" case, where actual_paisa is genuinely None (no bank
        # evidence exists at all), not a claim that the true amount is 0.
        expected_amount_paisa=(fd.expected_paisa if fd and fd.expected_paisa is not None else 0),
        actual_amount_paisa=(fd.actual_paisa if fd and fd.actual_paisa is not None else 0),
        delta_paisa=(fd.delta_paisa if fd and fd.delta_paisa is not None else 0),
        root_cause=RootCause(root_cause) if root_cause else None,
        confidence=confidence,
        status=status,
    ))
