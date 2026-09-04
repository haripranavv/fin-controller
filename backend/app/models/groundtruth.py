"""Ground truth model — ISOLATED BY DESIGN.

Do not import this module from agent-runtime code. See
app/db/groundtruth_session.py for the full isolation rule.
"""
from sqlalchemy import JSON, Boolean, Column, String

from app.core.config import settings
from app.db.groundtruth_session import GroundTruthBase


class GroundTruth(GroundTruthBase):
    __tablename__ = "ground_truth"
    __table_args__ = {"schema": settings.ground_truth_schema}

    record_id = Column(String(64), primary_key=True)
    true_match_ids = Column(JSON, nullable=False, default=list)
    true_divergence_stage = Column(String(32), nullable=True)
    true_root_cause = Column(String(64), nullable=True)
    is_ambiguous = Column(Boolean, nullable=False, default=False, server_default="false")
    injected_noise_type = Column(String(64), nullable=True)
