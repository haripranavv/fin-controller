# AI Finance Controller

A finance-operations agent for the Razorpay AI Buildathon (Track 04). See
[`PROJECT_SPEC.md`](PROJECT_SPEC.md) for the full spec and
[`docs/ARCHITECTURE_NOTES.md`](docs/ARCHITECTURE_NOTES.md) for decisions made
where the spec was silent or ambiguous.

Status: **Finalized (Stage 13 shipping pass complete).** See the build
sequence in `PROJECT_SPEC.md` §22.

## Stack

- Backend: Python 3.11+, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres 16.
- AI: root-cause investigator uses Gemini 3.6 Flash (Google `google-genai`) as its real provider, falling back to Claude Haiku 4.5 (Anthropic API) if no `GEMINI_API_KEY` is set, then a labeled rule-based stand-in if neither key is set.
- Frontend: React + TypeScript + Vite.

## Backend setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
copy .env.example .env        # Windows; `cp` on macOS/Linux — fill in GEMINI_API_KEY (or ANTHROPIC_API_KEY) when you reach the AI milestones
```

### Database

```bash
# from the repo root
docker compose up -d

# from backend/, with the venv active
alembic upgrade head
```

The initial migration (`alembic/versions/0001_initial_schema.py`) creates
every table from the current SQLAlchemy model metadata, plus a separate
`ground_truth` Postgres schema for isolated ground-truth data. Verified
against live Postgres 16 — see `docs/ARCHITECTURE_NOTES.md` for what that
verification found and fixed.

`alembic/versions/0002_auth_and_import_jobs.py` adds real authentication
(`users`, `user_sessions`) and the async file-import job state machine
(`import_jobs`, `import_job_files`), plus a nullable `batches.user_id` —
`NULL` means "system-owned, visible to every authenticated user" (every
pre-existing script-generated batch), a real id means private to that user.
Both migrations must be applied (`alembic upgrade head` applies whichever
haven't run yet).

## Authentication & the demo account

Real authentication: registration, login, bcrypt password hashing, and a
server-side session token — not a hardcoded demo-only credential. Scope is
deliberately narrow (no OAuth, MFA, email verification, password reset, or
roles/RBAC).

**You do not need to register to try this.** The login page has a one-click
**"Use demo account"** button that signs straight into a seeded account
containing only synthetic data:

- Email: `operator@financecontroller.demo`
- Password: `finance-demo-2026` (only needed if signing in manually instead
  of using the one-click button)

Every batch and case a user creates (by importing files or, for the demo
account, by generating synthetic data into it) is private to that user;
another authenticated user gets a 404, not the data, for a batch they don't
own. Batches created directly by the CLI scripts below (`dev-v1`,
`heldout-v1`, ...) have no owner and remain visible to every authenticated
user, matching how they always worked before real auth existed.

### Run the API

```bash
# from backend/, with the venv active
uvicorn app.main:app --reload
```

`GET /health` should return `{"status": "ok"}`. The operator-console API
(milestone 11) lives under `/api/*` — see `app/api/__init__.py` for its
scope boundary and `docs/ARCHITECTURE_NOTES.md`'s Milestone 11 section for
every route. It is read-mostly against already-persisted state, plus one
`POST /api/runs` that triggers a real (unchanged) orchestrator run in the
background so `GET /api/runs/{batch_id}/stream` can show it happening live.

### Run tests

```bash
# from backend/, with the venv active
pytest
```

The test suite runs model-level and data-generator smoke tests against
in-memory SQLite / pure Python (fast, no DB required), plus the full
operator-console API — including `tests/test_auth.py` (registration, login
success/failure, demo login, session/logout, protected-endpoint 401s) and
`tests/test_import.py` (source-type detection, the async job lifecycle,
duplicate-submission prevention, a failed import, a larger bulk-insert
path, cross-user isolation, and one true end-to-end test proving imported
rows reach the real `app.orchestrator.batch_runner.run_batch`). It does not
exercise Postgres-specific behavior — that's what `alembic upgrade head`
and the manual verification steps in `docs/ARCHITECTURE_NOTES.md` are for.

### Generate a synthetic dataset

```bash
# from backend/, with the venv active, Postgres up and migrated
python scripts/generate_dataset.py --dataset-version dev-v1 --seed 42 --count 140
python scripts/generate_dataset.py --dataset-version heldout-v1 --seed 1337 --count 90
```

Deterministic given the same `--seed`/`--count`/`--dataset-version`. Add
`--overwrite` to replace an existing dataset with the same version. See
`docs/ARCHITECTURE_NOTES.md`'s Milestone 2 section for the noise-category
design (the "axis A / axis B" split) and which divergence scenarios are
meant to be deterministically explainable vs. genuinely require the AI
root-cause investigator.

### Run the deterministic matcher

```bash
# from backend/, with the venv active, Postgres up and a dataset generated
python scripts/run_matcher_report.py --dataset-version dev-v1
```

Loads a persisted dataset, runs `app.matcher` (Order↔Payment, Payment↔Refund,
the bounded subset-sum Payment(s)↔Settlement matcher, and the
reference-then-fallback Settlement↔Bank matcher), and reports accuracy
against ground truth by axis-A category and by true root cause. Not the
real evaluation harness (that's milestone 10) — a standalone verification
script; see `docs/ARCHITECTURE_NOTES.md`'s Milestone 3 section for the full
results and the known `partial_settlement_split` limitation.

### Run the deterministic end-to-end pipeline

```bash
# from backend/, with the venv active, Postgres up and a dataset generated
python scripts/run_pipeline_report.py --dataset-version dev-v1
```

Runs every case through `app.pipeline.resolve_case` — matcher → verifier →
divergence tracer → the deterministic "known cause" rule table — and
reports RESOLVED/ESCALATED counts by true root cause. No AI yet (see
`docs/ARCHITECTURE_NOTES.md`'s Milestone 6 section): any case that would
need narration extraction or root-cause investigation honestly escalates
rather than guessing.

### Run the narration extraction evaluation

```bash
# from backend/, with the venv active, Postgres up and a dataset generated
python scripts/run_narration_eval.py --dataset-version dev-v1
```

Compares deterministic-only matching against narration-extraction-assisted
re-match, reported by category and specifically by known (clean) vs.
unseen (messy) narration format. Uses the real Anthropic API if
`ANTHROPIC_API_KEY` is set, otherwise a clearly-labeled rule-based
stand-in. See `docs/ARCHITECTURE_NOTES.md`'s Milestone 7 section for the
honest result: no differential lift by narration format in this system
(and why), but a real, measurable lift on genuine matching ambiguity
regardless of category. A follow-up validation experiment
(`scripts/run_narration_validation.py`) then formally rejected this
mechanism as an AI contribution — see the "2026-09-01 follow-up" note.

### Run the root-cause investigator evaluation

```bash
# from backend/, with the venv active, Postgres up and a dataset generated
python scripts/run_rootcause_eval.py --pool
```

Runs the real `app.rootcause.case.investigate_case` (deterministic rules
first, AI fallback) against the pooled real/generated datasets, reporting
coverage, precision, escalation rate, and a paired comparison against the
deterministic-only baseline. Uses the real Gemini API if `GEMINI_API_KEY`
is set, else the real Anthropic API if `ANTHROPIC_API_KEY` is set,
otherwise a clearly-labeled rule-based stand-in — same precedence in
`scripts/run_orchestrator.py` and `scripts/run_evaluation.py`. See
`docs/ARCHITECTURE_NOTES.md`'s Milestone 8 section for the validated
result: coverage nearly doubles (30.2%→57.9%) with 100% precision once
conditioned on correct upstream matching, and its "Provider swap" section
for the Gemini integration itself.

### Run the real Gemini smoke test

```bash
# from backend/, with the venv active, Postgres up, a dataset generated,
# and a real GEMINI_API_KEY in backend/.env
python scripts/gemini_smoke_test.py --max-real-calls 9 --dataset-version heldout-v1
```

Exercises the real, unchanged production path end to end on real
persisted cases — Gemini (real network call) → structured schema
validation → the confidence gate → `verify_root_cause_proposal` →
RESOLVED/ESCALATED — printing the raw model output, finish_reason, token
usage, latency, and full verifier checks for each case. Deliberately
targets narration-signal buckets and ground-truth-ambiguous cases (never
fed to the AI or verifier, only used to pick which real cases to spend
quota on) instead of an id-ordered slice, and hard-caps real generation
calls via `--max-real-calls` — plus one free real auth-error call (a
deliberately invalid key, real network round-trip, zero generation-quota
cost since it fails before inference) to exercise the fail-safe
escalation path for real. Read-only (writes nothing to Postgres); refuses
to run rather than silently substituting the stand-in if no key is set.
See `docs/ARCHITECTURE_NOTES.md`'s "Real Gemini smoke test" sections
(milestone/provider-swap and Stage 13) for a genuine bug this run found
and fixed (`max_output_tokens` truncation from Gemini 3.6's internal
"thinking" budget), the full Stage 13 real-sample results across all six
requested coverage categories, and why validation runs are quota-limited
(the free tier caps at 20 requests/day/model).

### Run the real agent orchestrator

```bash
# from backend/, with the venv active, Postgres up and a dataset generated
python scripts/run_orchestrator.py --dataset-version dev-v1
python scripts/run_orchestrator.py --dataset-version dev-v1 --overwrite   # re-run, replacing prior results
```

Runs every case through the real, persisted bounded state machine —
`ReconciliationCase`, `AgentEvent` (one per transition), `Match`,
`Investigation`, and `ExceptionRecord` rows are actually written, not just
reported. See `docs/ARCHITECTURE_NOTES.md`'s Milestone 9 section for the
full state-machine diagram, two real bugs found integrating against live
Postgres, and resolution/escalation counts on both datasets.

### Run the final evaluation

```bash
# from backend/, with the venv active, Postgres up, heldout-v1 (and/or dev-v1) generated
python scripts/run_evaluation.py --dataset-version heldout-v1
python scripts/run_evaluation.py --dataset-version dev-v1     # cross-check only, NOT held out
python scripts/run_evaluation.py --dataset-version heldout-v1 --no-persist  # report-only, skips EvaluationRun writes
```

Compares Arm A (`app.pipeline.resolve_case`, deterministic-only,
in-memory) against Arm B (`app.orchestrator.batch_runner.run_batch`, the
real AI-enhanced orchestrator, freshly re-run) on the same held-out
dataset, scored against ground truth read-only. The headline metric:
every case is tagged whether the upstream settlement match was actually
correct per ground truth, and every other metric — resolution rate,
precision, recall, false-match rate, escalation rate, exception value,
correct root-cause rate, AI-assisted resolution rate — is reported both
in aggregate and split by that flag, so AI performance is never counted
as "correct" on a case where the upstream match itself was wrong. Persists
`EvaluationRun` rows (a table unused since milestone 1). See
`docs/ARCHITECTURE_NOTES.md`'s Milestone 10 section for full results: on
heldout-v1, Arm B resolves 73.3% vs Arm A's 64.4% with 100.0% precision
once conditioned on a correct upstream match — the one aggregate false
resolution traces entirely to a pre-existing, already-documented matcher
limitation, not to the investigator.

## Frontend (operator console)

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, expects the API at http://127.0.0.1:8000
npm run build    # type-checks (tsc -b) then production-builds to dist/
```

Sidebar is grouped **Workspace** (Control Center, Batches, Cases,
Investigations, Exceptions) and **Operations** (Import Data, Agent
Activity), all reading real backend state — no mock/sample data anywhere
in the frontend:

1. **Control Center** — "what needs attention right now": a compact
   IMPORT → RECONCILE → FIND DIVERGENCE → INVESTIGATE → VERIFY →
   RESOLVE/ESCALATE loop banner, batch totals, the highest-value open
   cases with one click into the top one, recent activity, and the last
   offline held-out evaluation (ground-truth scored, read-only, milestone
   10's `EvaluationRun` rows).
2. **Batches** — every persisted batch (generated or imported) and the
   import job history behind it, so an import's status is visible even
   after navigating away or reloading.
3. **Cases** (`Reconciliation.tsx`) — dense, filterable table of every
   case (state, outcome, amount, root cause, resolved-via, severity).
4. **Agent Activity** — the real `AgentEvent` state/tool transition log,
   either replayed instantly for an already-completed batch or tailed
   live (Server-Sent Events) while a batch you trigger from the top bar
   is actually running.
5. **Investigations** — for a divergent case, the first divergence /
   root cause / confidence / verifier result / downstream impact is
   shown immediately at the top, before the stage-by-stage order → payment
   → refund → settlement → bank chain below it. For an AI-assisted case,
   an explicit AI INVESTIGATION → ROOT-CAUSE PROPOSAL → DETERMINISTIC
   VERIFIER → RESOLVED/ESCALATED block, plus an AI PRIVACY BOUNDARY panel
   showing exactly what evidence was sent to Gemini for that case (see
   below) — reading the literal payload `app.rootcause.evidence.build_evidence`
   (unchanged) would send, not a description of it.
6. **Exceptions** — every open exception with its actual escalation
   reason, severity, AI proposal/confidence if one was made, and the
   real verifier failure detail (not a placeholder).
7. **Import Data** — select files → detect source type → preview →
   validate → create batch → run controller, backed by a real,
   Postgres-persisted async job (`app.models.import_job.ImportJob`,
   QUEUED → VALIDATING → IMPORTING → READY/FAILED). A job's status
   survives navigating away and back, or a page reload — it's tracked by
   `job_id` in the URL and fetched fresh from the server, not held only
   in browser state.
8. **Record Detail** — one case's complete dossier: every financial
   record, every match, the investigation, the exception, the full
   event trace. Reached from any case row.

### AI privacy boundary

Before any evidence reaches Gemini, `app.rootcause.evidence.build_evidence`
(unchanged, frozen core logic) builds a minimal, bounded evidence set for
one case — never the raw uploaded files, never the full database, never
ground truth (a different Postgres schema no application code path can
read), and never more PII than the ids/amounts/stages/narration text the
investigation actually needs. The Investigations screen's privacy panel
shows this as a real, verifiable fact, not a claim: it re-invokes that same
function from the display layer and shows the literal payload that would be
sent, alongside explicit SENT / NOT SENT rows for raw files, ground truth,
unnecessary PII, and structured evidence.

Uses `VITE_API_BASE` (default `http://127.0.0.1:8000`, see `.env.example`)
and plain `fetch`/`EventSource` — no state-management or UI-kit dependency
beyond `react-router-dom`. `EventSource` cannot set an `Authorization`
header, so the live Agent Activity stream carries its session token as a
`?token=` query parameter instead; `get_current_user` accepts either.

## Repo layout

```
backend/
  app/
    core/config.py           settings (env vars, see .env.example)
    db/session.py            main DB engine/session (everything except ground truth)
    db/groundtruth_session.py isolated ground-truth DB engine/session — see its docstring
    models/                  SQLAlchemy models (financial.py, operational.py, groundtruth.py, enums.py)
    datagen/                 synthetic data generator — see its __init__.py for the isolation rule
    matcher/                 deterministic reconciliation matcher — see its __init__.py for scope boundary
    verifier/                financial verifier (constraint_verifier) — see its __init__.py for scope boundary
    divergence/              first-divergence engine (divergence_tracer) — see its __init__.py for scope boundary
    pipeline/                deterministic end-to-end pipeline (no AI yet) — see its __init__.py for scope boundary
    narration/               AI narration extraction + deterministic re-match — see its __init__.py for scope boundary (result: rejected, see ARCHITECTURE_NOTES.md)
    rootcause/               real root-cause investigator — see its __init__.py for scope boundary (result: validated, kept)
    orchestrator/            agent orchestration — the bounded state machine, wired and persisted for real
    api/                     milestone 11: the operator-console API layer — see its __init__.py for scope boundary
      auth_service.py        password hashing, session issuance/resolution, demo-user provisioning
      routes_auth.py         register / login / demo-login / session / logout, get_current_user dependency
      routes_import.py       async file-import job (Postgres-persisted state machine, bulk insert)
    models/auth.py           User, UserSession
    models/import_job.py     ImportJob, ImportJobFile
    main.py                  FastAPI app
  alembic/                   migrations
  scripts/generate_dataset.py CLI for the data generator
  scripts/run_matcher_report.py matcher verification report vs. ground truth
  scripts/run_evaluation.py   milestone 10 final evaluation (baseline vs. AI-enhanced)
  tests/                     pytest suite
frontend/
  src/screens/               the six operator-console screens (PROJECT_SPEC.md section 17)
  src/api.ts, src/types.ts   typed client for the backend's app/api/ routes
docs/
  ARCHITECTURE_NOTES.md      decisions log for spec gaps/ambiguities
docker-compose.yml           local Postgres
```
