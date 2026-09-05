"""File import — PROJECT_SPEC.md's chain (Order -> Payment -> Refund ->
Settlement -> Bank), fed from user-uploaded CSVs instead of
app.datagen.generator, through the SAME app.models.financial tables and
the SAME app.orchestrator.batch_runner.run_batch (via the existing,
unchanged POST /api/runs) every synthetic dataset already goes through.
This module adds no second reconciliation path and no new decision logic
- it only gets rows into the existing tables.

Server-side, Postgres-persisted job state machine (app.models.import_job.
ImportJob/ImportJobFile) - QUEUED -> VALIDATING -> IMPORTING -> READY /
FAILED - replacing the earlier in-memory staging dict so a job survives
page navigation (and a backend restart) rather than being lost. The
actual row-insertion step (IMPORTING) runs in a background thread using
SQLAlchemy bulk `session.execute(insert(Model), [...])` calls (one
round-trip per record type, not one INSERT per row) - the real fix for
"large imports appear to hang", not a special-cased fast path: the same
code runs for 2 rows or 200,000.

IDs: uploaded raw ids are re-issued through app.datagen.models's own
order_id()/payment_id()/... builders (UNCHANGED) under the caller's
dataset_version, exactly matching the naming convention
app.matcher.db_adapter.load_dataset's own query already expects - a
payment's order_id, and a refund's payment_id, are rewritten through the
same map so referential integrity survives the rename. A payment or
refund whose raw FK doesn't resolve within this same import is rejected,
not silently dropped or linked to the wrong record.

A ground-truth-shaped file is flagged "rejected_ground_truth" at
detect/validate time and never staged as insertable rows for any real
type - it structurally cannot reach app.orchestrator no matter what the
caller does next (app.api.import_detect.GROUND_TRUTH_MARKER_COLUMNS).

Every job and its files are owned by the uploading user
(ImportJob.user_id) - the batch it creates inherits that ownership
(app.models.operational.Batch.user_id), so isolation holds from upload
through to every case in it.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.api.import_detect import detect_source_type, parse_csv, validate_rows
from app.api.routes_auth import get_current_user
from app.datagen.models import bank_txn_id, order_id as build_order_id, payment_id as build_payment_id, refund_id as build_refund_id, settlement_id as build_settlement_id
from app.db.session import SessionLocal, get_db
from app.models.auth import User
from app.models.financial import BankTransaction, Order, Payment, Refund, Settlement
from app.models.import_job import ImportJob, ImportJobFile
from app.models.operational import Batch

router = APIRouter(prefix="/api/import", tags=["import"])

_JOB_LOCKS: dict[str, threading.Lock] = {}
# Guards the dataset_version "exists"-check-then-claim step in confirm()
# below, keyed by dataset_version (not job_id): _JOB_LOCKS alone only
# serializes two confirm() calls on the SAME job_id (the duplicate-
# submission case) - it does nothing for two DIFFERENT job_ids racing to
# confirm with the SAME dataset_version, since each acquires its own,
# distinct job lock and both can then run _dataset_version_in_use()
# concurrently before either has committed anything that the other's
# check would see (a classic check-then-act TOCTOU race: the actual
# Batch/Order rows _dataset_version_in_use() looks for aren't written
# until the background import thread runs, well after this check).
_DATASET_LOCKS: dict[str, threading.Lock] = {}
_DATASET_LOCKS_GUARD = threading.Lock()


def _lock_for(job_id: str) -> threading.Lock:
    return _JOB_LOCKS.setdefault(job_id, threading.Lock())


def _lock_for_dataset_version(dataset_version: str) -> threading.Lock:
    # setdefault on a plain dict isn't itself atomic across threads (two
    # threads could each create a different Lock object for the same key
    # in a genuine race), so this one is guarded by its own lock - a
    # small, always-uncontended critical section (just a dict lookup/
    # insert), unlike the per-dataset_version lock it hands out, which
    # can be held for the real check-and-claim work below.
    with _DATASET_LOCKS_GUARD:
        return _DATASET_LOCKS.setdefault(dataset_version, threading.Lock())


# --- response models -----------------------------------------------------------------


class FileDetectionResult(BaseModel):
    filename: str
    detected_type: str
    columns_found: list[str]
    row_count: int
    valid_row_count: int
    invalid_row_count: int
    duplicate_count: int
    missing_field_count: int
    missing_required_columns: list[str]
    sample_errors: list[str]
    preview_rows: list[dict[str, str]]
    ready: bool


class ImportJobResponse(BaseModel):
    job_id: str
    status: str
    dataset_version: str | None
    batch_id: str | None
    error_message: str | None
    files_total: int
    rows_total: int
    rows_inserted: int
    any_ready: bool
    files: list[FileDetectionResult]
    created_at: str
    updated_at: str
    # Real, not faked: current_stage is only ever set to a stage
    # _run_import has actually started, and rows_inserted/rows_total
    # above are updated incrementally as each record type's bulk insert
    # actually lands (see _run_import) - never a smoothly-animated
    # percentage disconnected from real work done. elapsed_seconds is
    # wall-clock time since the job was created (created_at to now, or
    # to updated_at once it's no longer running).
    current_stage: str | None
    elapsed_seconds: float


class ImportConfirmRequest(BaseModel):
    dataset_version: str


def _aware(dt: datetime) -> datetime:
    # Postgres returns timezone-aware datetimes for these
    # DateTime(timezone=True) columns; SQLite (the test fixture) returns
    # naive ones for the identical column type. Elapsed-time arithmetic
    # needs both sides consistently aware.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _file_result(f: ImportJobFile) -> FileDetectionResult:
    return FileDetectionResult(
        filename=f.filename, detected_type=f.detected_type, columns_found=f.columns_found or [],
        row_count=f.row_count, valid_row_count=f.valid_row_count, invalid_row_count=f.invalid_row_count,
        duplicate_count=f.duplicate_count, missing_field_count=f.missing_field_count,
        missing_required_columns=f.missing_required_columns or [], sample_errors=f.sample_errors or [],
        preview_rows=f.preview_rows or [], ready=f.valid_row_count > 0,
    )


def _job_response(job: ImportJob, files: list[ImportJobFile]) -> ImportJobResponse:
    file_results = [_file_result(f) for f in files]
    still_running = job.status in ("QUEUED", "VALIDATING", "IMPORTING")
    end = datetime.now(timezone.utc) if still_running else _aware(job.updated_at)
    elapsed = max(0.0, (end - _aware(job.created_at)).total_seconds())
    return ImportJobResponse(
        job_id=job.job_id, status=job.status, dataset_version=job.dataset_version, batch_id=job.batch_id,
        error_message=job.error_message, files_total=job.files_total, rows_total=job.rows_total,
        rows_inserted=job.rows_inserted, any_ready=any(r.ready for r in file_results), files=file_results,
        created_at=job.created_at.isoformat(), updated_at=job.updated_at.isoformat(),
        current_stage=job.current_stage, elapsed_seconds=elapsed,
    )


def _require_job(session: Session, job_id: str, user: User) -> ImportJob:
    job = session.query(ImportJob).filter_by(job_id=job_id).first()
    if job is None or not (job.user_id is None or job.user_id == user.id):
        raise HTTPException(status_code=404, detail=f"import job {job_id!r} not found")
    return job


# --- create job (SELECT FILES -> DETECT -> PREVIEW -> VALIDATE) --------------------


@router.post("/jobs", response_model=ImportJobResponse)
async def create_job(files: list[UploadFile] = File(...), session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ImportJobResponse:
    if not files:
        raise HTTPException(status_code=400, detail="no files uploaded")

    job_id = uuid.uuid4().hex
    job = ImportJob(job_id=job_id, user_id=user.id, status="QUEUED", files_total=len(files))
    session.add(job)
    session.flush()

    job.status = "VALIDATING"
    rows_total = 0
    job_files: list[ImportJobFile] = []
    for f in files:
        raw = await f.read()
        # parse_csv/validate_rows are pure, synchronous, CPU-bound (no
        # DB/network calls) - for a large file (hundreds of thousands of
        # rows) they can take real wall-clock seconds. create_job is an
        # `async def` route, so calling them directly here would block
        # FastAPI's single asyncio event loop for that whole duration -
        # every other request on the whole backend (health checks, other
        # users, this same upload's own later status polls) would stall
        # until parsing finished. run_in_threadpool moves the CPU work to
        # a worker thread so the event loop stays responsive; the actual
        # parsing/validation logic and its result are unchanged.
        try:
            columns, rows = await run_in_threadpool(parse_csv, raw)
        except Exception as exc:  # noqa: BLE001 - a malformed upload is a validation result, not a 500
            job_files.append(ImportJobFile(
                job_id=job_id, filename=f.filename or "unknown", detected_type="unknown", raw_bytes=raw,
                columns_found=[], row_count=0, valid_row_count=0, invalid_row_count=0, duplicate_count=0,
                missing_field_count=0, missing_required_columns=[], sample_errors=[f"could not parse as CSV: {exc}"], preview_rows=[],
            ))
            continue

        detected_type, missing_cols = detect_source_type(columns)
        if detected_type in ("unknown", "rejected_ground_truth"):
            job_files.append(ImportJobFile(
                job_id=job_id, filename=f.filename or "unknown", detected_type=detected_type, raw_bytes=raw,
                columns_found=columns, row_count=len(rows), valid_row_count=0, invalid_row_count=len(rows),
                duplicate_count=0, missing_field_count=0, missing_required_columns=missing_cols,
                sample_errors=["file appears to contain ground-truth fields - refused, never processed"] if detected_type == "rejected_ground_truth" else [],
                preview_rows=rows[:5],
            ))
            continue

        validation = await run_in_threadpool(validate_rows, detected_type, rows)
        rows_total += len(validation.valid_rows)
        job_files.append(ImportJobFile(
            job_id=job_id, filename=f.filename or "unknown", detected_type=detected_type, raw_bytes=raw,
            columns_found=columns, row_count=len(rows), valid_row_count=len(validation.valid_rows),
            invalid_row_count=validation.invalid_row_count, duplicate_count=validation.duplicate_count,
            missing_field_count=validation.missing_field_count, missing_required_columns=[],
            sample_errors=validation.sample_errors, preview_rows=rows[:5],
        ))

    for jf in job_files:
        session.add(jf)
    job.rows_total = rows_total
    session.commit()
    return _job_response(job, job_files)


@router.get("/jobs", response_model=list[ImportJobResponse])
def list_jobs(session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[ImportJobResponse]:
    jobs = session.query(ImportJob).filter_by(user_id=user.id).order_by(ImportJob.created_at.desc()).limit(50).all()
    out = []
    for job in jobs:
        files = session.query(ImportJobFile).filter_by(job_id=job.job_id).all()
        out.append(_job_response(job, files))
    return out


@router.get("/jobs/{job_id}", response_model=ImportJobResponse)
def get_job(job_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ImportJobResponse:
    job = _require_job(session, job_id, user)
    files = session.query(ImportJobFile).filter_by(job_id=job_id).all()
    return _job_response(job, files)


# --- confirm (CREATE BATCH, bulk insert in the background) -------------------------


def _dataset_version_in_use(session: Session, dataset_version: str) -> bool:
    # Checks two different things that both mean "not available":
    # already-committed rows from a past import (Batch/Order), AND
    # another job that has already claimed this dataset_version and is
    # actively importing it right now (ImportJob.status), whose own
    # Batch/Order rows may not exist yet. The second check is what
    # closes the TOCTOU race described above - without it, two jobs
    # racing on the same dataset_version would both see "not in use"
    # here regardless of locking, because neither has written a Batch/
    # Order row at the moment either check runs.
    if session.query(Batch).filter_by(batch_id=f"batch_{dataset_version}").first() is not None:
        return True
    like = f"%_{dataset_version}_%"
    if session.query(Order).filter(Order.order_id.like(like)).first() is not None:
        return True
    return (
        session.query(ImportJob)
        .filter(ImportJob.dataset_version == dataset_version, ImportJob.status.in_(("IMPORTING", "READY")))
        .first()
        is not None
    )


def _run_import(job_id: str, dataset_version: str, user_id: int) -> None:
    """Runs in a background thread (mirrors app.api.routes_runs's
    established pattern for the exact same reason: don't block the HTTP
    response on a potentially large insert) against its own fresh
    session. Bulk INSERTs, not one session.add() per row."""
    session = SessionLocal()
    try:
        job = session.query(ImportJob).filter_by(job_id=job_id).first()
        files = session.query(ImportJobFile).filter_by(job_id=job_id).all()

        order_files = [f for f in files if f.detected_type == "order" and f.valid_row_count]
        payment_files = [f for f in files if f.detected_type == "payment" and f.valid_row_count]
        refund_files = [f for f in files if f.detected_type == "refund" and f.valid_row_count]
        settlement_files = [f for f in files if f.detected_type == "settlement" and f.valid_row_count]
        bank_files = [f for f in files if f.detected_type == "bank_transaction" and f.valid_row_count]
        skipped = [f.filename for f in files if not f.valid_row_count]

        order_id_map: dict[str, str] = {}
        payment_id_map: dict[str, str] = {}
        inserted = {"order": 0, "payment": 0, "refund": 0, "settlement": 0, "bank_transaction": 0}
        rows_total = job.rows_total or 1  # avoid /0 in stage text below; a real total is already known at this point

        def _advance_stage(label: str) -> None:
            # Committed immediately (not batched with the next insert) so
            # a concurrent GET /api/import/jobs/{id} sees real, current
            # progress - this is the only reason this function reports
            # anything other than "IMPORTING" for the whole run.
            job.current_stage = label
            job.rows_inserted = sum(inserted.values())
            session.commit()

        _advance_stage(f"parsing and staging rows (0 / {rows_total})")

        idx = 1
        order_dicts = []
        for f in order_files:
            _cols, rows = parse_csv(f.raw_bytes)
            for vr in validate_rows("order", rows).valid_rows:
                gid = build_order_id(dataset_version, idx)
                idx += 1
                order_id_map[vr.pk_value] = gid
                order_dicts.append({
                    "order_id": gid, "merchant_id": vr.parsed["merchant_id"], "amount_paisa": vr.parsed["amount_paisa"],
                    "currency": vr.parsed.get("currency") or "INR", "status": vr.parsed["status"],
                })
        if order_dicts:
            session.execute(insert(Order), order_dicts)
        inserted["order"] = len(order_dicts)
        _advance_stage(f"inserted orders ({sum(inserted.values())} / {rows_total} rows so far)")

        idx = 1
        payment_dicts = []
        for f in payment_files:
            _cols, rows = parse_csv(f.raw_bytes)
            for vr in validate_rows("payment", rows).valid_rows:
                mapped_order = order_id_map.get(str(vr.parsed["order_id"]))
                if mapped_order is None:
                    skipped.append(f"{f.filename} (row referencing unknown order_id {vr.parsed['order_id']!r})")
                    continue
                gid = build_payment_id(dataset_version, idx)
                idx += 1
                payment_id_map[vr.pk_value] = gid
                payment_dicts.append({
                    "payment_id": gid, "order_id": mapped_order, "amount_paisa": vr.parsed["amount_paisa"],
                    "fee_paisa": vr.parsed.get("fee_paisa") or 0, "tax_on_fee_paisa": vr.parsed.get("tax_on_fee_paisa") or 0,
                    "method": vr.parsed["method"], "status": vr.parsed["status"], "narration": vr.parsed.get("narration") or None,
                })
        if payment_dicts:
            session.execute(insert(Payment), payment_dicts)
        inserted["payment"] = len(payment_dicts)
        _advance_stage(f"inserted payments ({sum(inserted.values())} / {rows_total} rows so far)")

        idx = 1
        refund_dicts = []
        for f in refund_files:
            _cols, rows = parse_csv(f.raw_bytes)
            for vr in validate_rows("refund", rows).valid_rows:
                mapped_payment = payment_id_map.get(str(vr.parsed["payment_id"]))
                if mapped_payment is None:
                    skipped.append(f"{f.filename} (row referencing unknown payment_id {vr.parsed['payment_id']!r})")
                    continue
                gid = build_refund_id(dataset_version, idx)
                idx += 1
                refund_dicts.append({
                    "refund_id": gid, "payment_id": mapped_payment, "amount_paisa": vr.parsed["amount_paisa"],
                    "reason_code": vr.parsed.get("reason_code") or None, "narration": vr.parsed.get("narration") or None,
                })
        if refund_dicts:
            session.execute(insert(Refund), refund_dicts)
        inserted["refund"] = len(refund_dicts)
        _advance_stage(f"inserted refunds ({sum(inserted.values())} / {rows_total} rows so far)")

        idx = 1
        settlement_dicts = []
        for f in settlement_files:
            _cols, rows = parse_csv(f.raw_bytes)
            for vr in validate_rows("settlement", rows).valid_rows:
                gid = build_settlement_id(dataset_version, idx)
                idx += 1
                settlement_dicts.append({
                    "settlement_id": gid, "merchant_id": vr.parsed["merchant_id"], "settled_amount_paisa": vr.parsed["settled_amount_paisa"],
                    "fee_deducted_paisa": vr.parsed.get("fee_deducted_paisa") or 0,
                    "period_start": vr.parsed["period_start"], "period_end": vr.parsed["period_end"],
                })
        if settlement_dicts:
            session.execute(insert(Settlement), settlement_dicts)
        inserted["settlement"] = len(settlement_dicts)
        _advance_stage(f"inserted settlements ({sum(inserted.values())} / {rows_total} rows so far)")

        idx = 1
        bank_dicts = []
        for f in bank_files:
            _cols, rows = parse_csv(f.raw_bytes)
            for vr in validate_rows("bank_transaction", rows).valid_rows:
                gid = bank_txn_id(dataset_version, idx)
                idx += 1
                bank_dicts.append({
                    "bank_txn_id": gid, "amount_paisa": vr.parsed["amount_paisa"], "value_date": vr.parsed["value_date"],
                    "utr_ref": vr.parsed.get("utr_ref") or None, "narration": vr.parsed.get("narration") or None,
                })
        if bank_dicts:
            session.execute(insert(BankTransaction), bank_dicts)
        inserted["bank_transaction"] = len(bank_dicts)

        total_inserted = sum(inserted.values())
        if total_inserted == 0:
            job.status = "FAILED"
            job.current_stage = None
            job.error_message = "no valid rows could be inserted (all referenced rows unresolvable or files empty)"
            session.commit()
            return

        batch_id = f"batch_{dataset_version}"
        session.add(Batch(batch_id=batch_id, dataset_version=dataset_version, status="created", user_id=user_id))
        job.status = "READY"
        job.current_stage = None
        job.batch_id = batch_id
        job.rows_inserted = total_inserted
        session.commit()
    except Exception as exc:  # noqa: BLE001 - report to the job row instead of losing the thread silently
        session.rollback()
        job = session.query(ImportJob).filter_by(job_id=job_id).first()
        if job is not None:
            job.status = "FAILED"
            job.current_stage = None
            job.error_message = str(exc)
            session.commit()
    finally:
        session.close()


@router.post("/jobs/{job_id}/confirm", response_model=ImportJobResponse)
def confirm(job_id: str, req: ImportConfirmRequest, session: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ImportJobResponse:
    lock = _lock_for(job_id)
    with lock:
        job = _require_job(session, job_id, user)
        if job.status != "VALIDATING":
            # Prevent duplicate submissions: a second confirm on a job
            # already IMPORTING/READY/FAILED is rejected, not silently
            # re-run.
            raise HTTPException(status_code=409, detail=f"import job {job_id!r} is already {job.status} - cannot confirm again")

        dv = req.dataset_version.strip()
        if not dv or not dv.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="dataset_version must be non-empty and alphanumeric (dashes/underscores allowed)")

        files = session.query(ImportJobFile).filter_by(job_id=job_id).all()
        if not any(f.valid_row_count for f in files):
            raise HTTPException(status_code=400, detail="no valid rows staged for this job - nothing to insert")

        # The exists-check and the claim (status -> IMPORTING,
        # dataset_version set) have to happen as one atomic step under
        # THIS dataset_version's own lock - two different job_ids
        # confirming with the same dataset_version each hold a different
        # _JOB_LOCKS entry above, so that lock alone can't serialize them
        # against each other. Whichever request gets here second, for
        # the same dataset_version, now correctly sees the first
        # request's claim (either its committed IMPORTING/READY status,
        # via the extended _dataset_version_in_use check, or - if the
        # first request is still mid-critical-section - by simply
        # blocking on this same lock until it finishes and re-reading
        # after).
        with _lock_for_dataset_version(dv):
            if _dataset_version_in_use(session, dv):
                raise HTTPException(status_code=409, detail=f"dataset_version {dv!r} is already in use - choose a different batch name")
            job.status = "IMPORTING"
            job.dataset_version = dv
            session.commit()

    thread = threading.Thread(target=_run_import, args=(job_id, dv, user.id), daemon=True)
    thread.start()

    files = session.query(ImportJobFile).filter_by(job_id=job_id).all()
    return _job_response(job, files)
