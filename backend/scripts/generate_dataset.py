#!/usr/bin/env python
"""CLI to generate and persist synthetic reconciliation datasets.

Usage (run from backend/, with the venv active):
    python scripts/generate_dataset.py --dataset-version dev-v1 --seed 42 --count 140
    python scripts/generate_dataset.py --dataset-version heldout-v1 --seed 1337 --count 90

Requires DATABASE_URL configured (see .env.example) and `alembic upgrade
head` already applied.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datagen.generator import generate_dataset  # noqa: E402
from app.datagen.persist import persist_batch  # noqa: E402
from app.db.groundtruth_session import GroundTruthSessionLocal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-version", required=True, help="e.g. dev-v1, heldout-v1")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--count", type=int, required=True, help="number of order flows to generate")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing dataset with the same version")
    args = parser.parse_args()

    batch = generate_dataset(seed=args.seed, num_flows=args.count, dataset_version=args.dataset_version)

    session = SessionLocal()
    gt_session = GroundTruthSessionLocal()
    try:
        persist_batch(session, gt_session, batch, overwrite=args.overwrite)
    finally:
        session.close()
        gt_session.close()

    print(
        f"Generated and persisted {args.dataset_version!r} (seed={args.seed}): "
        f"{len(batch.orders)} orders, {len(batch.payments)} payments, {len(batch.refunds)} refunds, "
        f"{len(batch.settlements)} settlements, {len(batch.bank_transactions)} bank txns, "
        f"{len(batch.ground_truth)} ground truth rows."
    )


if __name__ == "__main__":
    main()
