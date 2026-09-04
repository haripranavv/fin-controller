"""Matcher output types. Field names deliberately mirror
app.models.operational.Match's columns (same pattern as
app.datagen.models's Gen* dataclasses vs the financial models) so a future
orchestrator can construct Match ORM rows directly from these without a
hand-written field mapping that could drift.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MatchCandidate:
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    method: str
    score: float
    accepted: bool


@dataclass
class MatchRunReport:
    """Per-subject diagnostic info: one entry per settlement processed by
    each matching stage. Not persisted anywhere in this milestone — useful
    for this milestone's own dev-batch report, and shaped so a future
    AgentEvent's input_summary/output_summary text could be built from it."""

    subject_type: str
    subject_id: str
    outcome: str  # "matched" | "no_match" | "ambiguous" | "too_many_candidates"
    detail: str
