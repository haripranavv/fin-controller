# AI Finance Controller Agent — Project Specification

## 1. Project

Build a **finance-operations agent** for the Razorpay AI Buildathon Track 04: AI Finance Controller.

Official brief:
https://razorpay.com/buildathon/

The product is a real finance-ops agent, not a generic chatbot and not just a reconciliation dashboard.

### Core responsibility

The agent receives a batch of synthetic financial records and closes every reconciliation case as exactly one of:

- `RESOLVED`
- `ESCALATED`

Financial chain:

**Order → Payment → Refund → Settlement → Bank Transaction**

The agent must:
- reconcile the financial flow
- investigate genuinely ambiguous/unstructured cases
- verify proposed resolutions
- identify the first point of financial divergence
- provide evidence
- escalate only when it cannot safely resolve a case
- report batch-level metrics and honest exceptions

---

## 2. Product philosophy

### AI must have a genuine job

Do NOT use an LLM for:
- arithmetic
- simple ID matching
- date comparison
- fee calculation
- refund arithmetic
- deterministic validation
- graph traversal
- final financial verification

Use AI only where it adds real value:
1. messy/unseen financial narration → structured extraction
2. ambiguous root-cause investigation

AI output is always **untrusted** until deterministic verification succeeds.

### Core principle

**AI interprets/investigates. Deterministic systems establish financial truth.**

### Agent philosophy

The system should behave as a bounded agent:
- observe current case state
- perform an allowed action
- inspect the result
- move to the next allowed state
- terminate in `RESOLVED` or `ESCALATED`

Do NOT build an uncontrolled ReAct loop or allow arbitrary tool selection.

---

## 3. Product identity

The product should feel like a **professional finance-operations application** that a finance operator could use for hours.

The Bloomberg Terminal is only a reference for:
- practical information density
- fast drill-down
- persistent financial context
- operator-first workflow

Do NOT copy Bloomberg's black/yellow visual style.

UI should be:
- professional
- restrained
- clean
- information-first
- readable for long sessions
- useful rather than decorative

Avoid:
- gradients
- giant headings
- excessive cards
- chatbot-first UI
- flashy AI branding
- "AI MAGIC" style components

The agent is the product. The UI is the control/observation layer.

---

## 4. Finance data model

Primary financial entities:

### Order
Fields:
- `id`
- `order_id`
- `merchant_id`
- `amount_paisa`
- `currency`
- `status`
- `created_at`

### Payment
Fields:
- `id`
- `payment_id`
- `order_id`
- `amount_paisa`
- `fee_paisa`
- `tax_on_fee_paisa`
- `method`
- `status`
- `narration`
- `created_at`

### Refund
Fields:
- `id`
- `refund_id`
- `payment_id`
- `amount_paisa`
- `reason_code`
- `narration`
- `created_at`

### Settlement
Fields:
- `id`
- `settlement_id`
- `merchant_id`
- `settled_amount_paisa`
- `fee_deducted_paisa`
- `period_start`
- `period_end`
- `created_at`

IMPORTANT:
The real payment-to-settlement mapping must NOT be exposed to the agent through a convenient foreign-key field. Discovering that relationship is part of the reconciliation problem.

### Bank Transaction
Fields:
- `id`
- `bank_txn_id`
- `amount_paisa`
- `value_date`
- `utr_ref`
- `narration`

All monetary values are **integer paisa**. Never use floating-point money.

---

## 5. Operational entities

### Batch
Represents one processing run.

Fields:
- `id`
- `batch_id`
- `dataset_version`
- `created_at`
- `status`

### Reconciliation Case
The actual unit of work for the agent.

Fields:
- `id`
- `case_id`
- `batch_id`
- `anchor_type`
- `anchor_id`
- `state`
- `created_at`
- `updated_at`

A case represents a financial investigation chain, usually centered on a settlement or related financial event.

### Match
Stores candidate/accepted relationships.

Fields:
- `id`
- `case_id`
- `source_type`
- `source_id`
- `target_type`
- `target_id`
- `method`
- `score`
- `accepted`
- `created_at`

### Agent Event
Audit/event stream for state transitions and tool usage.

Fields:
- `id`
- `case_id`
- `state`
- `tool`
- `input_summary`
- `output_summary`
- `created_at`

### Evidence
Evidence references used in matching/investigation.

Fields:
- `id`
- `case_id`
- `source_type`
- `source_id`
- `evidence_type`
- `content`
- `created_at`

### Investigation
Stores first-divergence/root-cause results.

Fields:
- `id`
- `case_id`
- `divergence_stage`
- `expected_amount_paisa`
- `actual_amount_paisa`
- `delta_paisa`
- `root_cause`
- `confidence`
- `status`
- `created_at`

### Exception
Stores cases requiring human attention.

Fields:
- `id`
- `case_id`
- `reason`
- `severity`
- `amount_paisa`
- `status`
- `created_at`

### Evaluation Run
Stores benchmark results.

Fields:
- `id`
- `dataset_version`
- `mode`
- `records_processed`
- `match_rate`
- `precision`
- `recall`
- `false_match_rate`
- `exception_count`
- `throughput`
- `created_at`

### Ground Truth
Must remain isolated from normal agent execution.

Contains:
- `record_id`
- `true_match_ids`
- `true_divergence_stage`
- `true_root_cause`
- `is_ambiguous`
- `injected_noise_type`

The agent must never be able to query ground truth during normal processing.

---

## 6. Agent state machine

The state machine is bounded and explicit.

```text
INGESTED
  ↓
MATCH_ATTEMPT
  ├─ MATCHED → VERIFY
  │              ├─ PASS → RESOLVED
  │              └─ FAIL → DIVERGENCE_TRACE
  │
  └─ NO_MATCH
       ↓
    narration available?
       ├─ NO → ESCALATED
       └─ YES → NARRATION_EXTRACT
                    ↓
                 confidence gate
                    ├─ low → ESCALATED
                    └─ high → RE_MATCH
                                ↓
                              VERIFY
                                ├─ PASS → RESOLVED
                                └─ FAIL → DIVERGENCE_TRACE
                                             ↓
                                      known cause?
                                       ├─ YES → VERIFY → RESOLVED
                                       └─ NO → ROOT_CAUSE_INVESTIGATE
                                                  ↓
                                                VERIFY
                                             ┌────┴────┐
                                             ↓         ↓
                                         RESOLVED   ESCALATED
```

Terminal states:
- `RESOLVED`
- `ESCALATED`

No silent drop.
No forced match.
No infinite retry.

Recommended limits:
- one narration extraction attempt per case
- one root-cause investigation attempt per case

---

## 7. Agent tools

Implement these as independent, testable modules:

### `deterministic_matcher`
Find candidate relationships using deterministic matching logic.

### `narration_extractor`
AI tool. Converts messy/unseen narration into structured fields.

### `constraint_verifier`
Deterministic financial safety gate.

### `divergence_tracer`
Deterministic graph/chain walk. Finds the first point where expected and actual state diverge.

### `root_cause_investigator`
AI tool. Proposes a bounded root cause using evidence.

### `escalate`
Writes a complete exception record.

The orchestrator controls sequencing. Tools should not call one another directly.

---

## 8. Deterministic reconciliation

The matcher should work in stages:

### 8.1 Normalize
Normalize:
- case
- whitespace
- reference prefixes
- punctuation
- date representation
- obvious narration noise

### 8.2 Exact match
Prefer strong references and exact identifiers.

### 8.3 Candidate matching
Use:
- amount compatibility
- date compatibility
- normalized reference similarity
- narration/name similarity

Use bounded fuzzy matching (for example, RapidFuzz) where useful.

### 8.4 Batched settlement matching
A settlement can represent multiple payments.

Use bounded subset-sum / constrained matching:
- candidate set must be limited
- matching must satisfy the settlement amount after known deductions
- no record can be reused across accepted matches

### 8.5 Partial/split payments
Support one payment/order flow being represented by multiple records where the synthetic dataset explicitly creates that condition.

### 8.6 Candidate score
Candidate scoring may combine:
- identifier strength
- amount match
- date match
- text/narration similarity

Use deterministic thresholds. Do not let an LLM choose the final threshold.

### 8.7 Stop conditions
The deterministic matcher should stop when:
- a sufficiently strong candidate exists, or
- no candidate exists, or
- evidence is too ambiguous

Do not create an endless refinement loop.

---

## 9. AI component 1: Narration extraction

### Purpose

Handle financial narration formats that the deterministic parser has not seen before.

Example:

`NEFT-HDFC-RAJTRADCO-INV88213-PARTIAL`

Expected structured extraction:

```json
{
  "counterparty": "Raj Trading Co",
  "reference_id": "INV88213",
  "amount_hint": 12000,
  "transaction_type": "payment",
  "flags": ["partial"],
  "confidence": 0.87
}
```

### Input

```json
{
  "narration": "...",
  "amount": 12000,
  "date": "2026-08-30"
}
```

### Output

```json
{
  "counterparty": "string|null",
  "reference_id": "string|null",
  "amount_hint": "number|null",
  "transaction_type": "payment|refund|settlement|unknown",
  "flags": [],
  "confidence": 0.0
}
```

The output must be schema validated.

Suggested confidence gate:
- `< 0.50` → escalate
- `>= 0.50` → allow deterministic re-match

AI confidence alone never creates a match.

---

## 10. AI component 2: Root-cause investigator

Used only after deterministic divergence analysis cannot explain a mismatch using known rules.

### Input

```json
{
  "divergence_stage": "settlement",
  "expected_amount": 10500,
  "actual_amount": 10350,
  "delta": 150,
  "evidence": [],
  "allowed_causes": []
}
```

### Output

```json
{
  "root_cause": "unreported_fee",
  "supporting_evidence": ["fee_123"],
  "confidence": 0.89,
  "explanation": "..."
}
```

Allowed causes should be bounded, for example:
- `duplicate_refund`
- `missing_refund_netting`
- `unreported_fee`
- `partial_settlement_split`
- `currency_rounding`
- `duplicate_bank_credit`
- `unmatched_external_deduction`
- `unknown`

Suggested confidence gate:
- `< 0.60` → escalate
- `>= 0.60` → send to verifier

The AI cannot invent new cause categories.

---

## 11. Financial verifier

The verifier is the final authority.

Every accepted match and every AI-assisted resolution must pass it.

At minimum verify:

- amount arithmetic
- defined tolerance
- date constraints
- no double-counting
- relationship consistency
- AI-derived match consistency
- root-cause amount coverage

Example:

```text
Expected settlement = ₹10,500
Actual settlement   = ₹10,350
Delta               = ₹150

AI claims fee = ₹150

Verifier:
10,500 - 150 = 10,350
PASS
```

If it does not reconcile:

`ESCALATED`

AI confidence is never enough.

---

## 12. First-divergence engine

Financial chain:

**Order → Payment → Refund(s) → Settlement → Bank**

At every hop, compute:

- expected amount/state
- actual amount/state
- delta
- evidence

Example formulas:

```text
Payment expected = Order.amount

Settlement expected =
Σ(Payment.amount)
− Σ(Refund.amount)
− Σ(Payment.fee)
− Σ(Payment.tax_on_fee)

Bank expected =
Settlement.settled_amount
```

The first hop where:

`abs(expected - actual) > tolerance`

is the **FIRST POINT OF DIVERGENCE**.

The engine is deterministic.

It should also calculate downstream impact by projecting the chain forward from the divergence.

This is a key product capability, not a cosmetic timeline.

---

## 13. Investigation timeline

For a selected case, show:

```text
ORDER
  ↓
PAYMENT
  ↓
REFUND
  ↓
SETTLEMENT
  ↓
BANK
```

Each event should expose:
- timestamp
- source
- amount
- expected amount
- actual amount
- delta
- consistency status

Highlight the first divergence.

The UI should allow the operator to understand:

- what happened
- where it first went wrong
- why it likely went wrong
- what downstream records were affected
- what evidence supports the conclusion

A useful investigation statement is:

> “The settlement was not the original problem; the first divergence occurred earlier in the chain.”

---

## 14. Evidence and audit trail

Every tool/action should be traceable.

An agent event should capture:
- case
- previous state
- next state
- tool
- timestamp
- relevant input/reference
- output
- verifier result

A resolved case should have an auditable chain.

An escalated case must state:
- what was attempted
- what failed
- why automation stopped
- evidence available
- remaining uncertainty

---

## 15. Synthetic data

Create a seedable generator with known ground truth.

Target approximately **150–250 order flows** for evaluation.

Use a separate held-out test set.

Generate realistic cases including:
- clean
- messy/unseen narration
- duplicate references
- delayed events
- batched settlements
- partial payments
- refunds
- ambiguous causes
- genuinely unresolvable cases

Noise should be injected into the actual records, not simply exposed as a label.

Ground truth must remain separate.

The dataset should be realistic enough that a judge cannot dismiss it as trivial toy rows.

---

## 16. Baseline vs AI evaluation

Build two modes:

### Baseline
Deterministic system only.

### AI-enhanced
Same deterministic system plus:
- narration extraction
- root-cause investigation

Run both on the same held-out test set.

Primary metrics:
- match rate by count
- match rate by ₹ value
- precision
- recall
- false-match rate
- exception count
- exception value
- throughput
- AI-assisted resolution rate on unseen/ambiguous cases

Central goal:

> **AI should improve difficult-case resolution without increasing false financial matches.**

Particularly important:
- seen narration formats → baseline should already perform strongly
- unseen narration formats → AI should provide the measurable lift

---

## 17. User interface

Minimum operator views:

### 1. Overview
Show:
- total records/cases
- resolved
- escalated
- match rate
- ₹ affected
- throughput

### 2. Reconciliation
Dense/filterable table of cases.

### 3. Agent Activity
Show live state/tool transitions:
- MATCH_ATTEMPT
- EXTRACT
- RE_MATCH
- VERIFY
- TRACE
- INVESTIGATE
- RESOLVED / ESCALATED

### 4. Investigation
Show:
- financial chain
- first divergence
- expected vs actual
- evidence
- root-cause proposal
- verifier result

### 5. Exceptions
Show:
- case
- amount
- severity
- reason
- evidence
- AI proposal/confidence
- verifier failure

### 6. Record Detail
Show complete case evidence and event trace.

Do not make the UI chatbot-first.

A small evidence-backed Q&A feature may be added later if the core is complete, but it is not the primary product.

---

## 18. Realtime agent activity

The backend should emit agent events as the case changes state.

Example:

```json
{
  "case_id": "case_001",
  "from_state": "MATCH_ATTEMPT",
  "to_state": "EXTRACT",
  "tool": "deterministic_matcher",
  "message": "No candidate found",
  "timestamp": "..."
}
```

The frontend should consume this stream so the judge can watch the agent work.

---

## 19. Scope boundaries

### Core MVP
- financial batch ingestion
- Order → Payment → Refund → Settlement → Bank
- synthetic data + hidden ground truth
- deterministic reconciliation
- AI narration extraction
- first-divergence engine
- AI root-cause investigation
- financial verifier
- bounded agent state machine
- exception handling
- evaluation harness
- professional operator UI
- agent trace
- investigation timeline
- audit/evidence trail

### Optional after core works
- constrained evidence-backed Q&A
- confidence calibration visualization
- replay/step-through of an agent run
- downloadable exception report
- richer financial impact views

### Not part of the initial core
- full GST/tax reconciliation
- forecasting
- open-ended financial chatbot
- multi-currency
- full settlement approval workflows
- authentication/roles
- live bank integration
- additional finance domains
- unlimited agent retries
- unnecessary microservices/infrastructure

---

## 20. Design constraints

Do not:
- force AI into deterministic work
- let AI directly modify financial truth
- expose ground truth to the agent
- automatically resolve low-confidence cases
- build a generic chatbot
- add features simply because they sound impressive
- over-engineer the agent framework

Prefer:
- deterministic logic where possible
- bounded AI
- strong tests
- reproducible synthetic data
- auditable decisions
- clear failure handling
- measurable AI contribution
- professional operator UX

---

## 21. Definition of DONE

The project is ready for demo only when:

- a synthetic batch can be generated reproducibly
- ground truth is isolated
- 50+ cases can be processed
- every case reaches RESOLVED or ESCALATED
- deterministic reconciliation works
- AI narration extraction works
- AI outputs are schema validated
- AI cannot bypass verification
- first divergence is correctly identified
- root causes are bounded and verified
- unresolved cases show clear reasons/evidence
- baseline vs AI evaluation works
- held-out results are reproducible
- match rate / accuracy / throughput / exceptions are reported
- agent state transitions are visible
- investigation timeline works
- the difficult demo case can be replayed convincingly
- the application remains usable without relying on a chatbot

---

## 22. Implementation approach

This project is being built from a **completely empty repository**.

Do not assume Stage 1 or Stage 2 already exist.

Build from scratch while keeping the architecture above.

Recommended build sequence:

1. project/database foundation
2. synthetic data + ground truth
3. deterministic reconciliation
4. verifier
5. divergence engine
6. deterministic end-to-end tests
7. AI narration extraction
8. AI root-cause investigation
9. agent orchestration
10. evaluation
11. UI
12. final demo/polish

Before each major stage:
- inspect the current code
- preserve working functionality
- do not rewrite unrelated modules
- add tests
- keep the application runnable

The final goal is not “use lots of AI.”

The final goal is:

> **A credible AI Finance Controller that closes real-looking finance-ops cases, knows when to use AI, verifies its own conclusions, explains the financial trail, and honestly escalates what it cannot safely resolve.**
