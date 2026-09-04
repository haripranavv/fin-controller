"""Builds the bounded evidence contract for root_cause_investigator
(PROJECT_SPEC.md section 10's input: "evidence": []).

"Give the model only the structured evidence it needs" — exposes ONLY the
facts relevant to one case's divergence (the settlement group's refund
amounts, and the matched bank transaction(s) with their narration), never
the full batch and never ground truth.
"""
from __future__ import annotations

from app.datagen.models import GenBankTransaction, GenRefund


def build_evidence(group_refunds: list[GenRefund], bank_txns: list[GenBankTransaction]) -> list[dict]:
    items: list[dict] = []
    for r in group_refunds:
        items.append({
            "id": r.refund_id,
            "type": "refund",
            "amount_paisa": r.amount_paisa,
            "reason_code": r.reason_code,
        })
    for b in bank_txns:
        items.append({
            "id": b.bank_txn_id,
            "type": "bank_transaction",
            "amount_paisa": b.amount_paisa,
            "narration": b.narration,
        })
    return items
