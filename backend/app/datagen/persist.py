"""Persists a GeneratedBatch into Postgres: financial + operational tables
via app.db.session, ground truth via app.db.groundtruth_session — the same
two-session split the rest of the app uses, so persistence exercises the
isolation boundary rather than working around it.

This is the ONLY module in app.datagen that imports app.models.groundtruth /
app.db.groundtruth_session — every other module in this package works with
the plain GenGroundTruth dataclass. See app/datagen/__init__.py.
"""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.datagen.models import GeneratedBatch, batch_id as make_batch_id
from app.models.financial import BankTransaction, Order, Payment, Refund, Settlement
from app.models.groundtruth import GroundTruth
from app.models.operational import Batch


def dataset_exists(session: Session, dataset_version: str) -> bool:
    return session.query(Batch).filter_by(batch_id=make_batch_id(dataset_version)).first() is not None


def purge_dataset(session: Session, gt_session: Session, dataset_version: str) -> None:
    """Delete every row belonging to a previously generated dataset_version,
    identified by ID prefix (financial records don't carry a dataset_version
    column — see PROJECT_SPEC.md section 4's data model). FK-safe order:
    refunds before payments before orders."""
    prefix = f"%_{dataset_version}_%"
    session.execute(delete(Refund).where(Refund.refund_id.like(prefix)))
    session.execute(delete(Payment).where(Payment.payment_id.like(prefix)))
    session.execute(delete(Order).where(Order.order_id.like(prefix)))
    session.execute(delete(Settlement).where(Settlement.settlement_id.like(prefix)))
    session.execute(delete(BankTransaction).where(BankTransaction.bank_txn_id.like(prefix)))
    session.execute(delete(Batch).where(Batch.batch_id == make_batch_id(dataset_version)))
    session.commit()

    gt_session.execute(delete(GroundTruth).where(GroundTruth.record_id.like(prefix)))
    gt_session.commit()


def persist_batch(session: Session, gt_session: Session, batch: GeneratedBatch, overwrite: bool = False) -> None:
    if dataset_exists(session, batch.dataset_version):
        if not overwrite:
            raise ValueError(
                f"dataset_version {batch.dataset_version!r} already has persisted data; "
                "pass overwrite=True to replace it"
            )
        purge_dataset(session, gt_session, batch.dataset_version)

    session.add(Batch(batch_id=batch.batch_id, dataset_version=batch.dataset_version, status="created"))

    # Field names on the Gen* dataclasses match their ORM model's columns
    # 1:1 by design (see app/datagen/models.py docstring).
    for o in batch.orders:
        session.add(Order(**o.__dict__))
    for p in batch.payments:
        session.add(Payment(**p.__dict__))
    for r in batch.refunds:
        session.add(Refund(**r.__dict__))
    for s in batch.settlements:
        session.add(Settlement(**s.__dict__))
    for b in batch.bank_transactions:
        session.add(BankTransaction(**b.__dict__))
    session.commit()

    for g in batch.ground_truth:
        gt_session.add(
            GroundTruth(
                record_id=g.record_id,
                true_match_ids=g.true_match_ids,
                true_divergence_stage=g.true_divergence_stage,
                true_root_cause=g.true_root_cause,
                is_ambiguous=g.is_ambiguous,
                injected_noise_type=g.injected_noise_type,
            )
        )
    gt_session.commit()
