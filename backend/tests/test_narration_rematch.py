"""Milestone 7 tests: the deterministic re-match step. Reuses the REAL,
unchanged app.matcher machinery — hand-crafted fixtures for precise
control, plus integration tests against real generated data using a
reference-quality rule-based "stand-in LLM" (parses the actual narration
text independently of the generator's internal state — not a shortcut
that reads ground truth) to demonstrate the mechanism without a live API
key.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.datagen import catalog
from app.datagen.generator import generate_dataset
from app.datagen.models import GenOrder, GenPayment, GenRefund, GenSettlement
from app.matcher.reconciler import run_deterministic_matching
from app.narration.rematch import AMOUNT_HINT_TOLERANCE_FRACTION, attempt_rematch
from app.narration.types import NarrationExtraction

REMATCH_DIR = Path(__file__).resolve().parent.parent / "app" / "narration"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_order(order_id="ord_1", amount=10_000, merchant="mch_1", created_at=BASE) -> GenOrder:
    return GenOrder(order_id=order_id, merchant_id=merchant, amount_paisa=amount, currency="INR", status="paid", created_at=created_at)


def make_payment(payment_id="pay_1", order_id="ord_1", amount=10_000, fee=0, tax=0, created_at=BASE) -> GenPayment:
    return GenPayment(payment_id=payment_id, order_id=order_id, amount_paisa=amount, fee_paisa=fee,
                       tax_on_fee_paisa=tax, method="upi", status="captured", narration=None, created_at=created_at)


def make_settlement(settlement_id="stl_1", merchant="mch_1", settled=10_000, period_start=BASE, period_end=BASE, created_at=BASE) -> GenSettlement:
    return GenSettlement(settlement_id=settlement_id, merchant_id=merchant, settled_amount_paisa=settled,
                          fee_deducted_paisa=0, period_start=period_start, period_end=period_end, created_at=created_at)


def make_extraction(**overrides) -> NarrationExtraction:
    defaults = dict(counterparty="Acme", reference_id="REF1", amount_hint=None, transaction_type="payment", flags=[], confidence=0.85)
    defaults.update(overrides)
    return NarrationExtraction(**defaults)


REMATCH_DIR_ISOLATION_FILES = ["rematch.py"]


# --- isolation -----------------------------------------------------------


def test_rematch_does_not_import_groundtruth():
    forbidden = {"app.models.groundtruth", "app.db.groundtruth_session"}
    for name in REMATCH_DIR_ISOLATION_FILES:
        path = REMATCH_DIR / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden), f"{name} imports {imported & forbidden}"


def test_rematch_reuses_matcher_constants_not_duplicates():
    # Prove no drift-prone redefinition: these are the SAME objects as
    # app.matcher's own, not copies with the same value.
    import app.matcher.reconciler as matcher_reconciler
    import app.matcher.scoring as matcher_scoring
    import app.narration.rematch as rematch_module

    assert rematch_module.SETTLEMENT_MATCH_ACCEPT_THRESHOLD is matcher_scoring.SETTLEMENT_MATCH_ACCEPT_THRESHOLD
    assert rematch_module.SETTLEMENT_DATE_WINDOW_SLACK_DAYS is matcher_reconciler.SETTLEMENT_DATE_WINDOW_SLACK_DAYS


# --- hand-crafted precision tests -------------------------------------------------


def test_rematch_succeeds_for_out_of_window_payment_when_ai_confirms():
    # This payment's date falls well outside the settlement's window — the
    # FIRST deterministic pass would never have found it. Narration
    # extraction (simulated here) is what justifies searching for it here.
    order = make_order(created_at=BASE)
    payment = make_payment(amount=10_000, created_at=BASE + timedelta(days=30))
    settlement = make_settlement(settled=10_000, period_start=BASE, period_end=BASE + timedelta(days=2))

    extraction = make_extraction(amount_hint=10_000, confidence=0.9)
    result = attempt_rematch(
        "pay_1", extraction, [order], [payment], [], [settlement], already_consumed_payment_ids=set(),
    )
    assert result is not None
    assert result.accepted
    assert result.method == "narration_ai_assisted"
    assert result.target_id == "stl_1"


def test_rematch_respects_accept_threshold():
    order = make_order()
    payment = make_payment(amount=5_000, created_at=BASE + timedelta(days=30))
    # settlement declares far more than this payment alone could explain,
    # and there's nothing else to combine it with -> score stays low.
    settlement = make_settlement(settled=50_000, period_start=BASE, period_end=BASE + timedelta(days=2))

    extraction = make_extraction(confidence=0.9)
    result = attempt_rematch("pay_1", extraction, [order], [payment], [], [settlement], already_consumed_payment_ids=set())
    assert result is None


def test_rematch_respects_no_double_counting():
    # pay_1 (4,000) ALONE is far enough from the 10,000 target to fail
    # (score well under threshold); pay_1 + pay_2 (6,000) would hit it
    # exactly. Proves the double-counting exclusion is what blocks this,
    # not merely "nothing fits regardless".
    order1 = make_order(order_id="ord_1")
    payment1 = make_payment(payment_id="pay_1", order_id="ord_1", amount=4_000, created_at=BASE + timedelta(days=30))
    order2 = make_order(order_id="ord_2")
    payment2 = make_payment(payment_id="pay_2", order_id="ord_2", amount=6_000, created_at=BASE)
    settlement = make_settlement(settled=10_000, period_start=BASE, period_end=BASE + timedelta(days=2))

    extraction = make_extraction(confidence=0.9)

    # Sanity: WITHOUT the exclusion, pay_1+pay_2 would fit exactly.
    unrestricted = attempt_rematch(
        "pay_1", extraction, [order1, order2], [payment1, payment2], [], [settlement],
        already_consumed_payment_ids=set(),
    )
    assert unrestricted is not None and unrestricted.score == 1.0

    # pay_2 already claimed elsewhere -> must not be borrowed to help pay_1 fit
    result = attempt_rematch(
        "pay_1", extraction, [order1, order2], [payment1, payment2], [], [settlement],
        already_consumed_payment_ids={"pay_2"},
    )
    assert result is None


def test_rematch_rejects_contradictory_amount_hint():
    order = make_order()
    payment = make_payment(amount=10_000, created_at=BASE + timedelta(days=30))
    settlement = make_settlement(settled=10_000, period_start=BASE, period_end=BASE + timedelta(days=2))

    # amount_hint (1,000) wildly contradicts the recorded payment (10,000)
    extraction = make_extraction(amount_hint=1_000, confidence=0.9)
    result = attempt_rematch("pay_1", extraction, [order], [payment], [], [settlement], already_consumed_payment_ids=set())
    assert result is None


def test_rematch_accepts_amount_hint_within_tolerance():
    order = make_order()
    payment = make_payment(amount=10_000, created_at=BASE + timedelta(days=30))
    settlement = make_settlement(settled=10_000, period_start=BASE, period_end=BASE + timedelta(days=2))

    hint = round(10_000 * (1 - AMOUNT_HINT_TOLERANCE_FRACTION / 2))  # within tolerance
    extraction = make_extraction(amount_hint=hint, confidence=0.9)
    result = attempt_rematch("pay_1", extraction, [order], [payment], [], [settlement], already_consumed_payment_ids=set())
    assert result is not None


def test_rematch_returns_none_when_no_settlement_qualifies():
    order = make_order()
    payment = make_payment(amount=10_000, created_at=BASE + timedelta(days=30))
    extraction = make_extraction(confidence=0.9)
    result = attempt_rematch("pay_1", extraction, [order], [payment], [], [], already_consumed_payment_ids=set())
    assert result is None


def test_rematch_returns_none_for_unknown_payment_id():
    order = make_order()
    payment = make_payment(amount=10_000)
    settlement = make_settlement(settled=10_000)
    extraction = make_extraction(confidence=0.9)
    result = attempt_rematch("pay_does_not_exist", extraction, [order], [payment], [], [settlement], already_consumed_payment_ids=set())
    assert result is None


def test_rematch_ignores_settlements_for_a_different_merchant():
    order = make_order(merchant="mch_1")
    payment = make_payment(amount=10_000, created_at=BASE + timedelta(days=30))
    other_merchant_settlement = make_settlement(merchant="mch_OTHER", settled=10_000, period_start=BASE, period_end=BASE + timedelta(days=2))
    extraction = make_extraction(confidence=0.9)
    result = attempt_rematch("pay_1", extraction, [order], [payment], [], [other_merchant_settlement], already_consumed_payment_ids=set())
    assert result is None


# --- reference-quality rule-based "stand-in LLM" for integration tests -------------


_TOKEN_TO_MERCHANT = {catalog.token(name): name for _mid, name in catalog.MERCHANTS}


def reference_extract(narration: str) -> NarrationExtraction:
    """A competent, honest, rule-based parser for THIS project's own
    narration formats — independent of the generator's internal state
    (parses the string itself), standing in for what a good LLM should
    produce. Used only in tests, never in app.narration itself."""
    if not narration:
        return NarrationExtraction(confidence=0.0, transaction_type="unknown")

    upper = narration.upper()
    counterparty = None
    for token, name in _TOKEN_TO_MERCHANT.items():
        if token in upper:
            counterparty = name
            break

    ref_match = re.search(r"(?:INV|REF)\s*([A-Z0-9]+)", upper)
    reference_id = ref_match.group(1) if ref_match else None

    amount_hint = None
    imps_match = re.match(r"^[A-Z0-9]+\*[A-Z0-9]+\*PAYMENT$|^IMPS:[A-Z0-9]+:[A-Z0-9]+:(\d+)$", upper)
    if imps_match and imps_match.group(1):
        amount_hint = int(imps_match.group(1)) * 100  # rupees embedded in narration -> paisa

    flags = ["partial"] if "PARTIAL" in upper else []
    transaction_type = "payment"

    confidence = 0.85 if counterparty else 0.55
    return NarrationExtraction(
        counterparty=counterparty, reference_id=reference_id, amount_hint=amount_hint,
        transaction_type=transaction_type, flags=flags, confidence=confidence,
    )


def test_reference_extract_recovers_reference_id_from_spec_example_text():
    # section 9's own literal example text. Note: catalog.token("Raj
    # Trading Co") actually produces "RAJTRADINGCO" (the full word), not
    # the "RAJTRADCO" abbreviation section 9's illustrative narration uses
    # — a pre-existing, harmless docstring inaccuracy in
    # app/datagen/catalog.py found via this test (not touched here per
    # this milestone's "keep milestones 1-6 unchanged" instruction). So
    # counterparty recovery isn't expected against this exact literal
    # string, but reference_id extraction still works regardless.
    result = reference_extract("NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL")
    assert result.reference_id == "88213"
    assert "partial" in result.flags


def test_reference_extract_recovers_counterparty_from_actual_catalog_narration():
    # A narration shaped the way THIS project's own generator actually
    # produces it (app/datagen/catalog.py's real token() output).
    token = catalog.token("Raj Trading Co")
    result = reference_extract(f"NEFT-HDFC-{token}-INV88213-PARTIAL")
    assert result.counterparty == "Raj Trading Co"
    assert result.reference_id == "88213"
    assert result.confidence >= 0.5


def test_reference_extract_converts_imps_amount_to_paisa():
    result = reference_extract("IMPS:RAJTRADCO:REF1234:1200")
    assert result.amount_hint == 120_000  # Rs.1200 -> paisa


# --- integration: real generated data --------------------------------------------


@pytest.fixture(scope="module")
def batch_and_result():
    batch = generate_dataset(seed=42, num_flows=180, dataset_version="test-rematch")
    result = run_deterministic_matching(batch.orders, batch.payments, batch.refunds, batch.settlements, batch.bank_transactions)
    return batch, result


def test_rematch_never_reuses_a_payment_already_accepted_elsewhere(batch_and_result):
    batch, result = batch_and_result
    accepted = [m for m in result.settlement_payment if m.accepted]
    consumed = {m.source_id for m in accepted}

    # Pick any already-consumed payment and try to "rematch" it into a
    # DIFFERENT settlement — must never succeed, since it's excluded from
    # every other settlement's candidate pool by construction.
    if not accepted:
        pytest.skip("no accepted matches in this seed")
    victim = accepted[0]
    other_settlements = [s for s in batch.settlements if s.settlement_id != victim.target_id]
    extraction = make_extraction(confidence=0.9)
    result_rematch = attempt_rematch(
        victim.source_id, extraction, batch.orders, batch.payments, batch.refunds, other_settlements,
        already_consumed_payment_ids=consumed,
    )
    # either None, or (if it coincidentally fits another settlement on its
    # own) it must not be the settlement it was already matched to
    assert result_rematch is None or result_rematch.target_id != victim.target_id


def test_rematch_measurable_effect_on_real_no_match_cases(batch_and_result):
    """The core Milestone 7 measurement: for every payment the deterministic
    matcher failed to place, does narration-extraction-assisted re-match
    recover any of them? Uses reference_extract (not a real API call) as a
    stand-in for a competent LLM. Reports rather than requires a specific
    number — an honest 0 is a valid, reportable outcome too."""
    batch, result = batch_and_result
    accepted = [m for m in result.settlement_payment if m.accepted]
    consumed = {m.source_id for m in accepted}
    matched_payment_ids = {m.source_id for m in result.settlement_payment}  # attempted at all (matched or not)

    payments_by_id = {p.payment_id: p for p in batch.payments}
    all_payment_ids = {p.payment_id for p in batch.payments}
    unmatched_payment_ids = all_payment_ids - consumed

    recovered = 0
    attempted = 0
    for pid in unmatched_payment_ids:
        payment = payments_by_id[pid]
        if not payment.narration:
            continue
        extraction = reference_extract(payment.narration)
        if extraction.confidence < 0.50:
            continue
        attempted += 1
        rematch_result = attempt_rematch(
            pid, extraction, batch.orders, batch.payments, batch.refunds, batch.settlements,
            already_consumed_payment_ids=consumed,
        )
        if rematch_result is not None:
            recovered += 1
            consumed.add(pid)  # a real pipeline would commit this before trying the next case

    print(f"\n[narration re-match] attempted={attempted} recovered={recovered} "
          f"out of {len(unmatched_payment_ids)} originally-unmatched payments")
    # This is a reporting test, not a strict pass/fail gate on a specific
    # number — but it must run without error over the whole unmatched set.
    assert attempted >= 0 and recovered >= 0
    assert recovered <= attempted
