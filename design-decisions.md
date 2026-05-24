# Design Decisions Log

Numbered, accepted design decisions. `DESIGN.md` is the polished spec these feed into. Newest at the bottom.

| ID | Decision | Status | Date |
|----|----------|--------|------|
| DD-001 | ETL pipeline, not query-time Q&A | Accepted | 2026-05-22 |
| DD-002 | Event-centric data model | Accepted | 2026-05-22 |
| DD-003 | No raw-note preservation layer | Accepted | 2026-05-22 |
| DD-004 | SQLite as the store | Accepted | 2026-05-22 |
| DD-005 | Hybrid extraction (rules + LLM) | Accepted | 2026-05-22 |
| DD-006 | Per-note extraction + resolver stage | Accepted | 2026-05-22 |
| DD-007 | JSON `attributes` column with promotion path | Accepted | 2026-05-22 |
| DD-008 | Three-tier query layer (canned / raw SQL / NL→SQL) | Accepted | 2026-05-22 |
| DD-009 | Defer eval harness from MVP | Accepted | 2026-05-22 |
| DD-010 | Loss-neutral vocabulary (injury or illness) | Accepted | 2026-05-22 |
| DD-011 | "Never returned to work" is a first-class event | Accepted | 2026-05-22 |
| DD-012 | YAGNI trim of `Claim` and event taxonomy | Accepted | 2026-05-22 |
| DD-013 | Normalizer parses headers; body preserved verbatim | Accepted | 2026-05-23 |
| DD-014 | LLM retry: 3 attempts, jittered backoff, then drop+warn | Accepted | 2026-05-23 |
| DD-015 | Per-note extraction parallelized (default 4 workers) | Accepted | 2026-05-23 |
| DD-016 | Appointment identity by `(claim, date, party-overlap)` | Superseded by DD-019 | 2026-05-23 |
| DD-017 | Asymmetric appointment-status precedence | Superseded by DD-019 | 2026-05-23 |
| DD-018 | Route LLM appointment extractor by note shape | Superseded by DD-019 | 2026-05-23 |
| DD-019 | Three-stage per-claim appointment reconciliation | Accepted | 2026-05-23 |
| DD-020 | Appointment evidence is `(note_date, quote)` pairs | Accepted | 2026-05-24 |
| DD-021 | Appointments use per-note + reconciliation, not whole-claim LLM | Accepted | 2026-05-24 |
| DD-022 | RTW resolver merges within a ±7-day window per `(claim_id, duty_type)` | Accepted | 2026-05-24 |
| DD-023 | Widen `(note_date, quote)` evidence pairs to RTW + RTW-terminal; rename `AppointmentEvidence` → `EventEvidence` | Accepted | 2026-05-24 |

---

## DD-001 — ETL pipeline, not query-time Q&A
**Accepted · 2026-05-22**

Extract structured facts from prose once at ingest (LLM-assisted), store them, then run cheap deterministic queries. Per-query LLM calls over raw notes don't scale, aren't reproducible, and can't be audited.

---

## DD-002 — Event-centric data model
**Accepted · 2026-05-22**

Atomic unit is a typed, dated `Event` (`appointment`, `reserve_change`, `return_to_work`, `rtw_terminal`). All four sample queries are temporal/relational; a flat one-row-per-claim schema can't express "how many times" or "how long between."

---

## DD-003 — No raw-note preservation layer
**Accepted · 2026-05-22**

Persist only `claims` and `events`. Raw notes live as source files on disk and as in-memory objects during a run, not as a queried layer. Re-extraction workflows and audit tooling are out of scope. If light auditability is wanted later, a `source_excerpt` text field on `events` is the near-free extension.

---

## DD-004 — SQLite
**Accepted · 2026-05-22**

Single file, zero-config, SQL-native — right for human and AI-written queries. All DB access goes through one repository module so a Postgres swap is a connection-string change. Postgres rejected as premature ops overhead; document stores rejected for losing SQL aggregation.

---

## DD-005 — Hybrid extraction (rules + LLM)
**Accepted · 2026-05-22**

Deterministic rules/regex on structured text (reserve-change templates); LLM only for semantic judgment (RTW, appointments). Money + LLM = silent rounding errors; regex on free prose is brittle.

---

## DD-006 — Per-note extraction + resolver stage
**Accepted · 2026-05-22**

Extract per-note (cacheable, parallelizable prompts), then a dedicated Resolver merges duplicates. Notes restate facts constantly; a single appointment spans multiple notes. Whole-claim single extraction rejected: huge prompt, no per-fact provenance, still needs dedup.

---

## DD-007 — JSON `attributes` column with promotion path
**Accepted · 2026-05-22**

`events` table has stable columns plus a JSON `attributes` column for type-specific fields. Adding an event type needs no migration; a query-hot field can later be promoted to a typed column with an index. Schema-additive migrations only.

---

## DD-008 — Three-tier query layer
**Accepted · 2026-05-22**

Tier 1: canned query functions per sample query. Tier 2: raw SQL over the event schema. Tier 3: NL→SQL via LLM as stretch. The LLM only *writes* SQL — it never *computes* answers; all arithmetic stays in SQL over verified data.

---

## DD-009 — Defer eval harness from MVP
**Accepted · 2026-05-22**

Eval harness with hand-labeled gold set is valuable but a self-contained extension, not a prerequisite. MVP correctness is the four canned queries on the two sample claims, verified manually + unit tests for mechanical regressions. `DESIGN.md` §7 sketches the eval shape so it can be added later without rework.

---

## DD-010 — Loss-neutral vocabulary (injury or illness)
**Accepted · 2026-05-22**

Workers' comp covers discrete-event injuries *and* occupational illness/disease. Use `date_of_loss` (not `date_of_injury`), `claim_type ∈ {injury, illness}` as a corpus-query axis. MVP only ingests injury claims (both samples), but the schema doesn't assume that. Renaming columns later is cheap in code, expensive in trust.

---

## DD-011 — "Never returned to work" as a first-class event
**Accepted · 2026-05-22**

Add `rtw_terminal` event with `reason ∈ {ptd, deceased, separated, closed_no_rtw}` and an effective date. Q1 returns a discriminated union `returned | never_returned (reason) | pending`, never a `null`. Treating PTD/deceased/separated as `null` silently filters them from corpus averages.

---

## DD-012 — YAGNI trim of `Claim` and event taxonomy
**Accepted · 2026-05-22**

`Claim` keeps only: `claim_id`, `account`, `jurisdiction`, `claim_type`, `date_of_loss`, `source_file`, `ingested_at`. Event taxonomy keeps only: `reserve_change` (Q3), `appointment` (Q2/Q4), `return_to_work` (Q1), `rtw_terminal` (Q1). `work_status_change`, `loss_description`, `body_parts`, `diagnoses`, `avg_weekly_wage`, `comp_rate` dropped — no current query consumes them. DD-007 makes adding them later cheap.

---

## DD-013 — Normalizer parses headers; body preserved verbatim
**Accepted · 2026-05-23**

Normalizer parses the header date into typed `note_date`. Body is preserved verbatim apart from lossless cleanup (mojibake repair, line endings, whitespace). Body dates stay in original form. A shared `parse_date` utility is what extractors call on demand.

**Why.** The LLM `evidence_quote` substring check requires body fidelity — rewriting body dates breaks the anti-hallucination guarantee. Date detection in arbitrary prose is also ambiguous (`5/4`, `the 21st`, `4 weeks`); preserving the body means the worst case is a recoverable miss, not a corrupted note. Centralization is achieved by shared code, not mass rewrite.

---

## DD-014 — LLM retry: 3 attempts, jittered backoff, then drop+warn
**Accepted · 2026-05-23**

Wrap each provider's `structured()` call in a bounded-retry helper (`src/claims/llm/retry.py`): up to 3 attempts, exponential backoff (1s → 3s) with ±50% jitter, INFO per retry, WARNING on final failure. After exhaust, the extractor's `except LLMError: return []` catches it — pipeline completes but only after real effort to recover.

**Why.** Transient `503`/`429` is the dominant failure class. Bounded budget keeps cost predictable; jitter desynchronizes retry storms; drop+warn preserves pipeline progress; `NonRetryableError` sentinel is reserved for deterministic failures.

---

## DD-015 — Per-note extraction parallelized (default 4 workers)
**Accepted · 2026-05-23**

`ThreadPoolExecutor` in `__main__._cmd_ingest` fans out `run_all(note, …)` across notes. Default `workers=4`, configurable via `--workers N` (`--workers 1` for serial). Threads (not async) — SDKs are sync, work is I/O-bound.

**Why.** DD-006 already commits to independent per-note extraction. 4 workers ≈ 12 RPM, just under Gemini free-tier's 15 RPM ceiling; DD-014's jittered retry absorbs occasional crowding. Async rejected (broad rewrite, no win); multiprocessing rejected (pickling overhead). Event-emission order becomes non-deterministic with workers > 1 but correctness is unaffected.

---

## DD-016 — Appointment identity by `(claim, date, party-overlap)` *(superseded)*
**Superseded by DD-019 · 2026-05-23**

Original decision: replace `provider: str | None` with `parties: list[str]`; merge key = same claim, exact `encounter_date`, ≥1 party overlap; deterministic normalization, no fuzzy matching. The `parties` field and its normalization rules survive inside DD-019's per-cluster prompt; the pairwise resolver merge key was replaced by DD-019's pre-cluster step. Known same-facility-same-day-two-clinician failure mode carries over.

---

## DD-017 — Asymmetric appointment-status precedence *(superseded)*
**Superseded by DD-019 · 2026-05-23**

Rule (preserved inside DD-019's per-cluster prompt): `missed`/`cancelled` beat `attended`/`scheduled`/`unknown`; within negatives `missed > cancelled`; within positives `attended > scheduled > unknown`; ties broken by max `source_note_date`. Rationale: extraction-cost asymmetry — `missed` requires explicit negative language, `attended` can be over-inferred from narrative recaps. The pairwise resolver comparator was removed; the LLM now applies the rule per cluster.

---

## DD-018 — Route LLM appointment extractor by note shape *(superseded)*
**Superseded by DD-019 · 2026-05-23**

Original gate: run LLM appointment extractor only on Resolution Strategy notes or notes containing `Date of Appointment:`. DD-019 replaces this with a permissive prefilter (status verb OR date + appt-context) at extraction, with cleanup pushed downstream to reconciliation. Under-emission at the per-note stage is unrecoverable; over-emission is recoverable.

---

## DD-019 — Three-stage per-claim appointment reconciliation
**Accepted · 2026-05-23 · revised 2026-05-24**

**Stage 0 — per-note candidate generator (permissive).** LLM runs on every note that loosely mentions an appointment. Emits `(date, parties, status, evidence_quote)`. Dateless candidates dropped at the source. Marker extractor retired.

**Stage 1 — deterministic pre-cluster.** Greedy single-pass clustering on `(anchor_date ± 3 days, party-overlap)`, with empty-party absorption on one side only.

**Stage 2 — per-cluster LLM.** One small call per cluster (~2–10 candidates). Picks **status** (DD-017 rule in prompt) and **cleaned parties** (DD-016 normalization in prompt). Nothing else.

**Stage 3 — deterministic dates + evidence.** Encounter date = mode of contributing dates (occurred-on > scheduled-for, earliest breaks ties). `scheduled_notice_date` = earliest contributing `note_date ≤ encounter` (excludes retrospective recaps — fixes the Q4 negative-lag bug). Evidence = union of contributors per DD-020.

Resolver still handles `reserve_change`, `return_to_work`, `rtw_terminal`. Appointments bypass it; defensive warning fires if one slips through.

**Why.** Deterministic clustering beats LLM clustering on same-day + party-overlap (unambiguous). Per-cluster LLM has one job with focused context — DD-017 and DD-016 compliance went up materially after the split from a single all-in-one per-claim call. Date math stays rule-mechanical; LLM-chosen notice dates produced the retrospective-recap negative-lag bug.

**Known failure modes.** (a) Disjoint same-day parties bridged by an empty-party candidate collapse incorrectly. (b) LLM PROXIMITY-rule non-compliance at the per-note stage occasionally emits candidates whose quote doesn't co-locate date+party+status.

---

## DD-020 — Appointment evidence is `(note_date, quote)` pairs
**Accepted · 2026-05-24**

Replace `evidence_quote: str | None` + `source_note_dates: tuple[date, ...]` on `AppointmentAttributes` with a structured `evidence: tuple[AppointmentEvidence, ...]` where each entry is `(note_date, quote)`. Per-note extraction emits one element; reconciliation unions deterministically (dedup, sort by `note_date`). The per-cluster LLM no longer picks an evidence quote — its schema is just `(status, parties)`. `source_note_dates` survives as a derived `@property`.

**Scope.** Appointments only. Other event types keep the old shape; they aren't merged across notes the same way.

**Why.** Provenance is the point of audit fields — dropping six of seven quotes per merged row defeated it. Removing the LLM's evidence-selection job also shrinks the prompt and tightens the schema.

---

## DD-021 — Appointments use per-note + reconciliation, not a whole-claim LLM call
**Accepted · 2026-05-24**

Two architectures were prototyped (`scripts/AppointmentScripts/`): (M1) per-note extractor → `reconcile_appointments`; (M2) one whole-claim LLM call. On the sample claims M2 under-recalls attended visits by 27–42% and drops every missed/cancelled event; DD-017 precedence is not consistently applied across a 50+ page prompt.

Production uses M1. The whole-claim script is kept as a comparison harness. Per-cluster prompts concentrate evidence for one appointment so precedence and negative-event detection work; only `(status, parties)` is LLM-decided. Trade-off accepted: higher LLM call volume (one per cluster) and the same-facility-same-day two-doctor failure mode (DD-016 §known limitations).

---

## DD-022 — RTW resolver merges within a ±7-day window per `(claim_id, duty_type)`
**Accepted · 2026-05-24**

Identity-merge on `(claim_id, event_date, duty_type)` kept duplicates when the LLM's per-note date math drifted (claim 1: 11/10 + 11/11 events for the same return). Replace with greedy clustering on `event_date ± 7 days` within `(claim_id, duty_type)`; survivor = earliest event_date (matches Q1's precedence). Deterministic, no LLM — RTW has 0–2 events per claim with narrow disagreement axes, so a DD-019-style reconciliation is overkill.

**Rejected.** Per-claim LLM reconciliation (wrong tool for the scale). Whole-claim LLM call for Q1 — tested in `scripts/RtwScripts/`; on claim 2 it emitted `returned` from an HR *request* ("HR Director has requested clmt RTW on Monday, 8/25"), violating the strict-evidence rule. Same DD-021 lesson, opposite direction (over-emission vs under-recall).

---

## DD-023 — Widen `(note_date, quote)` evidence pairs to every event type
**Accepted · 2026-05-24**

DD-020 scoped structured `evidence: tuple[EventEvidence, ...]` to appointments only. Extend the same shape to **every** event type — `reserve_change`, `return_to_work`, `rtw_terminal` — for a uniform evidence surface. Single-source events carry a one-element tuple; merged events union one entry per contributing note (dedup by `(note_date, quote)`, sort by `note_date`). `AppointmentEvidence` renamed to `EventEvidence`. `source_note_dates` and `evidence_quote` survive as derived `@property`s on every attributes class. Q1 and Q3 outputs ship the full evidence list in place of scalar `evidence_quote` + `source_note_dates`.

---

<!-- Append new decisions below. Template:

## DD-0NN — <short title>
**Status:** Proposed · **Date:** YYYY-MM-DD

**Context.** ...
**Decision.** ...
**Why.** ...
**Rejected.** ...
-->
