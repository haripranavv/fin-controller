# The divergence_tracer tool (PROJECT_SPEC.md section 7), implementing
# section 12's first-divergence engine: a deterministic graph/chain walk
# over Order -> Payment -> Refund(s) -> Settlement -> Bank.
#
# Scope boundary: this module ONLY detects and locates divergence — it
# never explains it. No AI, no root-cause inference (that's
# root_cause_investigator, a later milestone). It also does not duplicate
# app.verifier's chronology/relationship/double-counting/root-cause-
# proposal checks — those stay in app.verifier (milestone 4, unchanged).
# This module is purely the amount-arithmetic PATH: where along the chain
# the money first stops adding up, and what that implies for every stage
# after it.
#
# ISOLATION RULE: nothing here imports app.models.groundtruth or
# app.db.groundtruth_session — same rule as app.datagen, app.matcher, and
# app.verifier.
