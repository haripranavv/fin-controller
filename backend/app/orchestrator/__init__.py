# Agent orchestration (PROJECT_SPEC.md section 22 build step 9): wires the
# already-validated tools — app.matcher, app.verifier, app.divergence,
# app.pipeline.known_causes, app.rootcause — into section 6's bounded state
# machine, with real persistence (ReconciliationCase, AgentEvent, Match,
# Investigation, ExceptionRecord).
#
# Every tool package listed above is used UNCHANGED. This package adds no
# new matching, verification, tracing, or investigation logic of its own —
# it sequences existing, tested decisions and records what happened.
#
# Narration extraction is deliberately NOT part of this wiring: an
# evaluation-only validation experiment formally rejected it as an AI
# contribution (see docs/ARCHITECTURE_NOTES.md) — B and C were identical
# in every measured case, and the underlying mechanism had a 97% false-
# match rate. A NO_MATCH case therefore escalates directly here, exactly
# as it already did in milestone 6's deterministic-only pipeline. This
# mirrors the user's own instruction for this milestone, which lists the
# matcher, verifier, divergence tracer, root-cause investigator, and
# escalation path — not narration extraction.
#
# Bounded, not agentic in the open-ended sense (section 2: "Do NOT build an
# uncontrolled ReAct loop or allow arbitrary tool selection"): each case
# makes exactly one deterministic pass through the graph below — one
# match attempt, one verify, at most one divergence trace, at most one
# root-cause investigation attempt — and always terminates RESOLVED or
# ESCALATED. There is no retry loop and no cycle in the state graph, so
# "no infinite loops" holds structurally, not just by convention.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as every other app.* package.
