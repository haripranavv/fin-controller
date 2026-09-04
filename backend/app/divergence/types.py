"""Divergence tracer output types.

StageResult's shape (name/expected/actual/delta/consistent/evidence/note)
is deliberately audit-friendly for the same reason app.verifier.types is —
it's meant to become an Investigation row (divergence_stage,
expected_amount_paisa, actual_amount_paisa, delta_paisa — see
app/models/operational.py) and an Evidence trail once the orchestrator
exists, without a translation layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageResult:
    stage: str  # one of app.models.enums.DivergenceStage's values
    expected_paisa: int | None
    actual_paisa: int | None
    delta_paisa: int | None
    consistent: bool
    evidence: list[str]
    note: str


@dataclass
class DivergenceTrace:
    stages: list[StageResult] = field(default_factory=list)
    first_divergence: StageResult | None = None
    downstream_impact: list[StageResult] = field(default_factory=list)
    # "clean": every stage consistent.
    # "diverged": a concrete numeric mismatch was found.
    # "unresolved": the trace hit a stage with no evidence to compare
    # against at all (currently only possible at BANK — no bank
    # transaction found) — we know WHERE it broke down, not by how much.
    status: str = "clean"
    total_downstream_delta_paisa: int | None = 0

    @property
    def first_divergence_stage(self) -> str | None:
        return self.first_divergence.stage if self.first_divergence else None
