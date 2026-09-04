# The narration_extractor tool (PROJECT_SPEC.md section 7 & 9): the first
# of the two AI components. Converts messy/unseen payment narration into
# structured fields, used ONLY after deterministic matching fails and
# narration exists (section 6's state machine:
# MATCH_ATTEMPT -> NO_MATCH -> narration available? -> NARRATION_EXTRACT).
#
# Section 2's core principle applies in full here: "AI interprets/
# investigates. Deterministic systems establish financial truth." This
# package is split accordingly:
#   - extractor.py / client.py / prompts.py / types.py: the AI call itself
#     — strict schema validation, confidence gate, never touches matching.
#   - rematch.py: the DETERMINISTIC re-match step that actually decides
#     whether anything gets matched, reusing app.matcher's own scoring/
#     threshold/subset-sum machinery UNCHANGED. Section 9: "AI confidence
#     alone never creates a match."
#
# Scope boundary: like every prior tool package, this does not persist to
# the database and is not wired into app.pipeline (that's the
# orchestrator's job, milestone 9) — app.pipeline (milestones 1-6) is
# UNCHANGED by this milestone.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as every other app.* package.
