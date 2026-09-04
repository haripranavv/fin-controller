# Deterministic reconciliation matcher (PROJECT_SPEC.md section 7's
# `deterministic_matcher` tool, implementing section 8's matching stages).
#
# Scope boundary: this package returns MatchCandidate results — it does NOT
# persist to the `matches` table itself. That table's case_id is a required
# FK to reconciliation_cases, and cases don't exist until the orchestrator
# (milestone 9) creates them. Persisting is the orchestrator's job; this
# package is a pure, independently-testable tool per section 7: "Implement
# these as independent, testable modules... The orchestrator controls
# sequencing."
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as app.datagen (see its
# __init__.py). Only backend/scripts/run_matcher_report.py, a standalone
# reporting script, reads ground truth, and only for comparison output.
