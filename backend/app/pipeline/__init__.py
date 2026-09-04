# Deterministic end-to-end pipeline (PROJECT_SPEC.md section 22's build
# step 6: "deterministic end-to-end tests", scheduled deliberately BEFORE
# step 7's AI narration extraction).
#
# Wires app.matcher -> app.verifier -> app.divergence together for one
# order's case, implementing the DETERMINISTIC-ONLY subset of section 6's
# state machine. No AI: NARRATION_EXTRACT and ROOT_CAUSE_INVESTIGATE don't
# exist yet (milestones 7-8), so any case that would need them terminates
# ESCALATED here — honestly, not by guessing.
#
# This is NOT the orchestrator (milestone 9): it doesn't persist
# Batch/ReconciliationCase/AgentEvent/Investigation/ExceptionRecord rows
# (no case exists yet to persist against — same scope boundary
# app.matcher/app.verifier/app.divergence already established), and it
# will need extending once AI tools exist. It proves the deterministic
# skeleton works end-to-end first, per the spec's own recommended build
# order.
#
# app.matcher, app.verifier, app.divergence, and app.models are all used
# here UNCHANGED — this package only composes them.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as every other app.* package.
