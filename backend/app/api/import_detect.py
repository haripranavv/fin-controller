"""Deterministic source-type detection and row validation for the file
import flow (PROJECT_SPEC.md's chain: Order -> Payment -> Refund ->
Settlement -> Bank, section 4). Pure functions, no DB/FastAPI dependency
here — column-signature matching against the exact field sets
app.models.financial's tables already require, nothing fuzzy or
AI-assisted (explicitly not in scope: "do not add an LLM simply to map
obvious columns").

Also the one and only place that refuses a ground-truth-shaped upload
outright (GROUND_TRUTH_MARKER_COLUMNS) — a file carrying any of those
columns is flagged "rejected_ground_truth" and is never staged as
insertable rows for any of the five real types, so it structurally
cannot reach app.orchestrator regardless of what a caller does next.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

SOURCE_TYPES = ["order", "payment", "refund", "settlement", "bank_transaction"]

PK_COLUMN: dict[str, str] = {
    "order": "order_id", "payment": "payment_id", "refund": "refund_id",
    "settlement": "settlement_id", "bank_transaction": "bank_txn_id",
}

# Mirrors app.models.financial's nullable=False columns exactly (minus
# created_at, which every one of those tables server-defaults to now()).
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "order": {"order_id", "merchant_id", "amount_paisa", "status"},
    "payment": {"payment_id", "order_id", "amount_paisa", "method", "status"},
    "refund": {"refund_id", "payment_id", "amount_paisa"},
    "settlement": {"settlement_id", "merchant_id", "settled_amount_paisa", "period_start", "period_end"},
    "bank_transaction": {"bank_txn_id", "amount_paisa", "value_date"},
}

# Columns with a model-level default (fee_paisa=0, tax_on_fee_paisa=0,
# fee_deducted_paisa=0, currency="INR") or that are genuinely nullable
# (narration, reason_code, utr_ref) - optional in the upload.
OPTIONAL_COLUMNS: dict[str, set[str]] = {
    "order": {"currency"},
    "payment": {"fee_paisa", "tax_on_fee_paisa", "narration"},
    "refund": {"reason_code", "narration"},
    "settlement": {"fee_deducted_paisa"},
    "bank_transaction": {"utr_ref", "narration"},
}

AMOUNT_COLUMNS: dict[str, set[str]] = {
    "order": {"amount_paisa"},
    "payment": {"amount_paisa", "fee_paisa", "tax_on_fee_paisa"},
    "refund": {"amount_paisa"},
    "settlement": {"settled_amount_paisa", "fee_deducted_paisa"},
    "bank_transaction": {"amount_paisa"},
}

DATE_COLUMNS: dict[str, set[str]] = {
    "order": set(), "payment": set(), "refund": set(),
    "settlement": {"period_start", "period_end"},
    "bank_transaction": {"value_date"},
}

# A file carrying any of these is a ground_truth export, not a source
# record file - see app/models/groundtruth.py. Checked before anything
# else; such a file is never assigned a real source_type.
GROUND_TRUTH_MARKER_COLUMNS = {"true_root_cause", "true_match_ids", "is_ambiguous", "injected_noise_type", "true_divergence_stage"}


def parse_csv(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    columns = [c.strip() for c in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    return columns, rows


def detect_source_type(columns: list[str]) -> tuple[str, list[str]]:
    """Returns (detected_type, missing_required_columns_for_closest_match).
    detected_type is one of SOURCE_TYPES, "unknown", or
    "rejected_ground_truth"."""
    col_set = set(columns)
    if col_set & GROUND_TRUTH_MARKER_COLUMNS:
        return "rejected_ground_truth", []

    for t in SOURCE_TYPES:
        if REQUIRED_COLUMNS[t] <= col_set:
            return t, []

    # No exact match - report the closest candidate's missing columns
    # (whichever type shares the most required columns with what's here)
    # so the UI can tell the operator exactly what's missing.
    best_type, best_overlap = "unknown", -1
    for t in SOURCE_TYPES:
        overlap = len(REQUIRED_COLUMNS[t] & col_set)
        if overlap > best_overlap:
            best_type, best_overlap = t, overlap
    missing = sorted(REQUIRED_COLUMNS[best_type] - col_set) if best_overlap > 0 else []
    return "unknown", missing


def _parse_amount(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ValidatedRow:
    raw: dict[str, str]
    parsed: dict[str, object]  # amount/date columns coerced to int/datetime, rest left as str
    pk_value: str


@dataclass
class ValidationResult:
    valid_rows: list[ValidatedRow] = field(default_factory=list)
    invalid_row_count: int = 0
    duplicate_count: int = 0
    missing_field_count: int = 0
    sample_errors: list[str] = field(default_factory=list)


def validate_rows(source_type: str, rows: list[dict[str, str]]) -> ValidationResult:
    result = ValidationResult()
    required = REQUIRED_COLUMNS[source_type]
    amount_cols = AMOUNT_COLUMNS[source_type]
    date_cols = DATE_COLUMNS[source_type]
    pk_col = PK_COLUMN[source_type]
    seen_pks: set[str] = set()

    for i, row in enumerate(rows):
        errors: list[str] = []
        missing = [c for c in required if not row.get(c)]
        if missing:
            errors.append(f"row {i + 1}: missing required field(s) {missing}")
            result.missing_field_count += len(missing)

        parsed: dict[str, object] = dict(row)
        for c in amount_cols:
            if row.get(c):
                v = _parse_amount(row[c])
                if v is None:
                    errors.append(f"row {i + 1}: {c}={row[c]!r} is not a whole-paisa integer")
                else:
                    parsed[c] = v
        for c in date_cols:
            if row.get(c):
                d = _parse_date(row[c])
                if d is None:
                    errors.append(f"row {i + 1}: {c}={row[c]!r} is not a parseable ISO date/timestamp")
                else:
                    parsed[c] = d

        pk_value = row.get(pk_col, "")
        if pk_value and pk_value in seen_pks:
            errors.append(f"row {i + 1}: duplicate {pk_col}={pk_value!r} within this file")
            result.duplicate_count += 1
            errors_are_fatal = True
        else:
            errors_are_fatal = bool(missing) or any("not a" in e for e in errors)
            if pk_value:
                seen_pks.add(pk_value)

        if errors:
            result.sample_errors.extend(errors[:1])
            result.sample_errors = result.sample_errors[:10]
        if errors_are_fatal or not pk_value:
            result.invalid_row_count += 1
            continue

        result.valid_rows.append(ValidatedRow(raw=row, parsed=parsed, pk_value=pk_value))

    return result
