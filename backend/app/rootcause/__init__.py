# The root_cause_investigator tool (PROJECT_SPEC.md section 7 & 10): the
# second of the two AI components. Proposes a bounded root cause for a
# divergence that deterministic tracing found but could not explain with a
# known rule.
#
# VALIDATED, unlike narration extraction (see docs/ARCHITECTURE_NOTES.md):
# an evaluation-only experiment (scripts/run_rootcause_validation.py) ran a
# rule-based stand-in against 642 real divergent cases across 5 datasets
# and found it materially improved coverage over the deterministic rule
# table (30.2% -> 57.9%) with 100% precision once conditioned on correct
# upstream matching (162/642 discordant pairs, ALL favoring the AI stand-in,
# exact sign-test p~=0.0000). This package implements the real version.
#
# Section 2's core principle still applies in full:
#   - investigator.py/client.py/prompts.py/evidence.py/types.py: the AI
#     call itself - strict schema validation, confidence gate, never
#     touches verification or resolution.
#   - case.py: enforces "AI must only run after deterministic divergence
#     tracing finds an unexplained case" as an actual code path (tries
#     app.pipeline.known_causes.detect_known_cause FIRST, unchanged;
#     calls the AI only when that returns None on a genuine divergence)
#     and converts an AI proposal into a RootCauseProposal for
#     app.verifier.checks.verify_root_cause_proposal (unchanged) to
#     independently check — section 10: "AI cannot invent new cause
#     categories", and AI confidence alone never resolves a case.
#
# Scope boundary: like every prior tool package, this does not persist to
# the database and is not wired into app.pipeline (that's the
# orchestrator's job, milestone 9) — app.pipeline, app.matcher,
# app.verifier, app.divergence, app.datagen, app.narration are all
# UNCHANGED by this milestone.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as every other app.* package.
