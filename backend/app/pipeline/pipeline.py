"""resolve_case: the deterministic-only subset of PROJECT_SPEC.md section
6's state machine.

    MATCH_ATTEMPT
      |- no accepted settlement match -> ESCALATED
      |     (NO_MATCH -> narration extraction would run here; no AI yet)
      `- MATCHED -> VERIFY (trace_chain "clean"? + verify_match)
              |- PASS -> RESOLVED
              `- FAIL -> DIVERGENCE_TRACE (already run, to get first_divergence)
                     |- unresolved (missing evidence) -> ESCALATED
                     |- known cause found -> VERIFY (verify_root_cause_proposal)
                     |        |- PASS -> RESOLVED
                     |        `- FAIL -> ESCALATED
                     `- no known cause -> ESCALATED
                            (ROOT_CAUSE_INVESTIGATE would run here; no AI yet)

Every branch is deterministic. Every branch terminates in RESOLVED or
ESCALATED (PROJECT_SPEC.md section 6: "No silent drop", and section 21's
Definition of DONE: "every case reaches RESOLVED or ESCALATED").
"""
from __future__ import annotations

from app.divergence.tracer import trace_chain
from app.pipeline.known_causes import detect_known_cause
from app.pipeline.types import CaseInputs, CaseResult
from app.verifier.checks import verify_root_cause_proposal
from app.verifier.verifier import verify_match


def resolve_case(inputs: CaseInputs, *, tolerance_paisa: int = 0) -> CaseResult:
    order_id = inputs.order.order_id

    if inputs.settlement is None or inputs.settlement_match is None:
        return CaseResult(
            order_id=order_id, outcome="ESCALATED",
            reason="no_match: no confident settlement candidate found for this payment "
                   "(narration-assisted re-match is not available in this deterministic-only pipeline)",
        )

    trace = trace_chain(
        inputs.order, inputs.payments, inputs.refunds, inputs.settlement, inputs.bank_txns,
        settlement_group_payments=inputs.settlement_group_payments or inputs.payments,
        settlement_group_refunds=inputs.settlement_group_refunds or inputs.refunds,
        tolerance_paisa=tolerance_paisa,
    )

    settlement_stage = next(s for s in trace.stages if s.stage == "settlement")
    settlement_verification = verify_match(
        inputs.settlement_match, settlement_stage.expected_paisa, settlement_stage.actual_paisa,
        tolerance_paisa=tolerance_paisa,
    )

    if trace.status == "clean" and settlement_verification.passed:
        return CaseResult(
            order_id=order_id, outcome="RESOLVED",
            reason="resolved: the full order -> payment -> refund -> settlement -> bank chain reconciles exactly",
            trace=trace, verification=settlement_verification,
        )

    if trace.status == "unresolved":
        return CaseResult(
            order_id=order_id, outcome="ESCALATED",
            reason=f"escalated: unresolved — missing evidence at '{trace.first_divergence.stage}' "
                   f"({trace.first_divergence.note})",
            trace=trace, verification=settlement_verification,
        )

    proposal = detect_known_cause(trace.first_divergence, inputs.settlement_group_refunds or inputs.refunds, inputs.bank_txns)
    if proposal is None:
        return CaseResult(
            order_id=order_id, outcome="ESCALATED",
            reason=f"escalated: divergence at '{trace.first_divergence.stage}' "
                   f"(delta={trace.first_divergence.delta_paisa}p) has no known deterministic cause "
                   "(root-cause investigation is not available in this deterministic-only pipeline)",
            trace=trace, verification=settlement_verification,
        )

    known_evidence_ids = _all_known_ids(inputs)
    proposal_verification = verify_root_cause_proposal(
        proposal, trace.first_divergence.expected_paisa, trace.first_divergence.actual_paisa,
        known_evidence_ids, tolerance_paisa=tolerance_paisa,
    )

    if proposal_verification.passed:
        return CaseResult(
            order_id=order_id, outcome="RESOLVED",
            reason=f"resolved: known cause '{proposal.root_cause}' verified "
                   f"(adjustment={proposal.claimed_adjustment_paisa}p at '{trace.first_divergence.stage}')",
            trace=trace, root_cause_proposal=proposal, verification=proposal_verification,
        )

    failed = ", ".join(c.name for c in proposal_verification.failed_checks())
    return CaseResult(
        order_id=order_id, outcome="ESCALATED",
        reason=f"escalated: known cause '{proposal.root_cause}' proposed but failed verification ({failed})",
        trace=trace, root_cause_proposal=proposal, verification=proposal_verification,
    )


def _all_known_ids(inputs: CaseInputs) -> set[str]:
    ids = {inputs.order.order_id}
    ids.update(p.payment_id for p in inputs.payments)
    ids.update(r.refund_id for r in inputs.refunds)
    ids.update(p.payment_id for p in inputs.settlement_group_payments)
    ids.update(r.refund_id for r in inputs.settlement_group_refunds)
    ids.update(b.bank_txn_id for b in inputs.bank_txns)
    if inputs.settlement is not None:
        ids.add(inputs.settlement.settlement_id)
    return ids
