"""Persists one AgentEvent row per state/tool transition (PROJECT_SPEC.md
sections 14 and 18: "The backend should emit agent events as the case
changes state"). A pure persistence helper — it records that a transition
happened; case_runner.py decides when.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.enums import CaseState
from app.models.operational import AgentEvent


def emit_event(
    session: Session,
    case_id: str,
    from_state: CaseState | None,
    to_state: CaseState,
    *,
    tool: str | None = None,
    message: str | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    verifier_result: dict | None = None,
) -> None:
    session.add(AgentEvent(
        case_id=case_id,
        from_state=from_state,
        to_state=to_state,
        tool=tool,
        input_summary=input_summary,
        output_summary=output_summary,
        message=message,
        verifier_result=verifier_result,
    ))
