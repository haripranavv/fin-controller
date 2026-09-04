"""Milestone 11: the read-mostly API layer serving the operator console.

Scope boundary (mirrors every other package's __init__.py docstring in this
project): this package NEVER imports app.db.groundtruth_session or
app.models.groundtruth — see test_api_does_not_import_groundtruth. The
operator console shows only what the real system actually did; ground
truth stays isolated to offline evaluation (scripts/run_evaluation.py),
never surfacing here.

Every route here either (a) reads already-persisted operational state
(ReconciliationCase, AgentEvent, Match, Investigation, ExceptionRecord,
Batch, and the underlying financial records) and formats it for display,
or (b) recomputes an already-tested, deterministic, pure function
(app.divergence.tracer.trace_chain, UNCHANGED) against a case's own
persisted records purely so the Investigation screen can show the
stage-by-stage breakdown that isn't itself persisted (only the Investigation
row's single first-divergence summary is). Route (c), triggering a batch
run, calls app.orchestrator.batch_runner.run_batch UNCHANGED in a
background thread — no new decision logic anywhere in this package.
"""
