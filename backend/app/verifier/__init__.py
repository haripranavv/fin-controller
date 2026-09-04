# The constraint_verifier tool (PROJECT_SPEC.md section 7), implementing
# section 11's checklist: "Every accepted match and every AI-assisted
# resolution must pass it."
#
# Scope boundary: this is a deterministic financial safety GATE, not the
# divergence engine (milestone 5) — it does not walk the Order -> Payment ->
# Refund -> Settlement -> Bank chain to compute expected/actual amounts
# itself. Callers (a future divergence engine, or this milestone's own
# tests) supply expected_paisa/actual_paisa; the verifier's job is deciding
# PASS/FAIL against those numbers, plus the structural checks (relationship
# consistency, chronology, double-counting, bounded root causes, confidence
# gates) that don't depend on chain traversal.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as app.datagen and app.matcher.
