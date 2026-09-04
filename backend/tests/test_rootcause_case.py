"""Milestone 8 tests: investigate_case's orchestration — enforcing
"deterministic rules first, AI only when they return None" as a real code
path. Hand-crafted fixtures for precise control, plus integration tests
against real generated data reusing app.matcher / app.divergence /
app.pipeline.known_causes UNCHANGED.
"""
from __future__ import annotations

import json

import pytest

from app.datagen.generator import generate_dataset
from app.datagen.models import GenBankTransaction, GenRefund
from app.divergence.tracer import trace_chain
from app.divergence.types import StageResult
from app.matcher.reconciler import run_deterministic_matching
from app.pipeline.assemble import assemble_case_inputs
from app.rootcause.case import investigate_case
from app.rootcause.client import MockRootCauseClient


def make_refund(refund_id="rfd_1", amount=1_500) -> GenRefund:
    from datetime import datetime, timezone
    return GenRefund(refund_id=refund_id, payment_id="pay_1", amount_paisa=amount,
                      reason_code="customer_request", narration=None, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


def make_bank_txn(bank_txn_id="bnk_1", amount=8_500, narration=None) -> GenBankTransaction:
    from datetime import datetime, timezone
    return GenBankTransaction(bank_txn_id=bank_txn_id, amount_paisa=amount,
                               value_date=datetime(2026, 1, 1, tzinfo=timezone.utc), utr_ref="UTR1", narration=narration)


AI_PAYLOAD = {"root_cause": "unreported_fee", "supporting_evidence": ["bnk_1"], "confidence": 0.85,
              "explanation": "narration references an additional processing charge"}


# --- deterministic-first precedence ---------------------------------------------


def test_ai_not_invoked_when_deterministic_rule_covers_it():
    # delta exactly matches a known refund amount -> known_causes fires,
    # AI must never be called.
    refund = make_refund(amount=1_500)
    stage = StageResult(stage="settlement", expected_paisa=8_500, actual_paisa=10_000, delta_paisa=1_500,
                         consistent=False, evidence=["stl_1"], note="")
    client = MockRootCauseClient(default=json.dumps(AI_PAYLOAD))

    result = investigate_case(client, stage, [refund], [])

    assert result.source == "deterministic"
    assert result.proposal.root_cause == "missing_refund_netting"
    assert client.calls == []  # the AI was never called


def test_ai_invoked_when_no_deterministic_rule_matches():
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=9_850, delta_paisa=-150,
                         consistent=False, evidence=["stl_1"], note="")
    client = MockRootCauseClient(default=json.dumps({**AI_PAYLOAD, "confidence": 0.85}))

    result = investigate_case(client, stage, [], [make_bank_txn(narration="SETTLE/mch/stl_1 ADDL PROC CHG APPLIED")])

    assert result.source == "ai"
    assert result.proposal is not None
    assert result.proposal.root_cause == "unreported_fee"
    assert len(client.calls) == 1


def test_ai_declines_below_confidence_gate():
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=9_850, delta_paisa=-150,
                         consistent=False, evidence=["stl_1"], note="")
    client = MockRootCauseClient(default=json.dumps({**AI_PAYLOAD, "confidence": 0.30}))

    result = investigate_case(client, stage, [], [])

    assert result.source == "none"
    assert result.proposal is None
    assert "below the" in result.detail


def test_unresolved_case_never_calls_ai():
    stage = StageResult(stage="bank", expected_paisa=10_000, actual_paisa=None, delta_paisa=None,
                         consistent=False, evidence=["stl_1"], note="no bank transaction found")
    client = MockRootCauseClient(default=json.dumps(AI_PAYLOAD))

    result = investigate_case(client, stage, [], [])

    assert result.source == "none"
    assert result.proposal is None
    assert client.calls == []


def test_schema_failure_from_ai_results_in_no_proposal():
    stage = StageResult(stage="settlement", expected_paisa=10_000, actual_paisa=9_850, delta_paisa=-150,
                         consistent=False, evidence=["stl_1"], note="")
    client = MockRootCauseClient(default="not valid json")

    result = investigate_case(client, stage, [], [])

    assert result.source == "none"
    assert result.proposal is None
    assert "AI investigation failed" in result.detail


# --- integration: real generated data, real matcher/divergence/known_causes -------


def _build_diverged_cases(batch):
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    cases = []
    for order in batch.orders:
        inputs = assemble_case_inputs(batch, result, order.order_id)
        if inputs.settlement is None:
            continue
        trace = trace_chain(
            inputs.order, inputs.payments, inputs.refunds, inputs.settlement, inputs.bank_txns,
            settlement_group_payments=inputs.settlement_group_payments or inputs.payments,
            settlement_group_refunds=inputs.settlement_group_refunds or inputs.refunds,
        )
        if trace.status != "diverged":
            continue
        cases.append((order.order_id, trace.first_divergence, inputs.settlement_group_refunds or inputs.refunds, inputs.bank_txns))
    return cases


def test_deterministic_rules_still_win_on_real_data_before_ai_is_asked():
    batch = generate_dataset(seed=42, num_flows=200, dataset_version="test-rootcause-case")
    cases = _build_diverged_cases(batch)
    assert cases

    client = MockRootCauseClient(default=json.dumps(AI_PAYLOAD))
    deterministic_count = 0
    for order_id, first_divergence, group_refunds, bank_txns in cases:
        result = investigate_case(client, first_divergence, group_refunds, bank_txns)
        if result.source == "deterministic":
            deterministic_count += 1
    assert deterministic_count > 0
    # Every deterministic-source case must not have triggered a client call
    # for THAT case specifically — verified structurally by the mock's
    # call count staying below the case count once any deterministic hits
    # occur (a looser but real signal, since the mock is shared across the
    # whole loop here).
    assert len(client.calls) < len(cases)
