# Design Decisions Log

A running, append-only log of design decisions made for the Adaptional claim
analysis exercise. Each entry records **what** was decided, **why**, and **what
was rejected** — so the reasoning is traceable, not just the outcome.

- This file is the *decision tracker*. `DESIGN.md` is the polished spec that
  these decisions feed into. `claims file analysis.md` is the source-data
  inventory the decisions are based on.
- Newest entries at the bottom. Status: `Proposed` → `Accepted` → `Superseded`.

| ID | Decision | Status | Date |
|----|----------|--------|------|
| DD-001 | Treat the problem as an ETL pipeline, not a Q&A system | Accepted | 2026-05-22 |
| DD-002 | Event-centric data model — typed, dated `Event` as the atomic unit | Accepted | 2026-05-22 |
| DD-003 | Scope to a query-answering system — no raw-note preservation layer | Accepted | 2026-05-22 |
| DD-004 | Use SQLite as the store | Accepted | 2026-05-22 |
| DD-005 | Hybrid extraction — deterministic rules + LLM | Accepted | 2026-05-22 |
| DD-006 | Per-note extraction granularity, with a dedup/resolver stage | Accepted | 2026-05-22 |
| DD-007 | JSON `attributes` column for type-specific event payloads | Accepted | 2026-05-22 |
| DD-008 | Three-tier query layer (canned / raw SQL / NL→SQL) | Accepted | 2026-05-22 |
| DD-009 | Defer eval harness from MVP scope; keep as nice-to-have extension | Accepted | 2026-05-22 |
| DD-010 | Model claims as injury-or-illness from day one; use loss-neutral vocabulary | Accepted | 2026-05-22 |
| DD-011 | Model "never returned to work" as a first-class terminal event, not a null | Accepted | 2026-05-22 |
| DD-012 | Trim `Claim` and event taxonomy to query-minimum; defer speculative fields | Accepted | 2026-05-22 |
| DD-013 | Normalizer parses header dates into typed fields; body text preserved verbatim | Accepted | 2026-05-23 |
| DD-014 | LLM extractors retry transient failures up to 3 attempts with backoff, then drop+warn | Accepted | 2026-05-23 |
| DD-015 | Per-note extraction is parallelized across notes (default 4 workers, threads, --workers configurable) | Accepted | 2026-05-23 |
| DD-016 | Appointment identity is `(claim_id, encounter_date, party-overlap)`; LLM emits a `parties` list, no string fuzzy matching | Accepted | 2026-05-23 |
| DD-017 | Appointment status precedence is asymmetric: `missed`/`cancelled` beat `attended`; recency tiebreaker at the same tier | Accepted | 2026-05-23 |

---

## DD-001 — Treat the problem as an ETL pipeline, not a Q&A system
**Status:** Accepted · **Date:** 2026-05-22

**Context.** The brief states that a single claim could be answered with one LLM
call — that is explicitly *not* the goal. The goal is trends/insights over a
large corpus.

**Decision.** Build an ETL pipeline: extract structured facts from prose **once**
at ingest time (the expensive LLM-assisted step), store them, then run cheap,
deterministic queries over the corpus.

**Why.** Per-query LLM calls don't scale, aren't reproducible, and can't be
audited. Extracting once and querying many times amortizes the expensive step.

**Rejected.** Query-time LLM Q&A over raw notes — doesn't scale to a corpus, no
reproducibility, no auditability.

---

## DD-002 — Event-centric data model
**Status:** Accepted · **Date:** 2026-05-22

**Context.** All four sample queries are temporal or relational ("how long
between X and Y", "how many times Z").

**Decision.** The atomic unit of the model is a typed, dated `Event`
(`appointment`, `reserve_change`, `work_status_change`, `return_to_work`, …).
Queries are aggregations over events.

**Why.** A flat "attributes per claim" record cannot express "how many times" or
"how long between." An event timeline can.

**Rejected.** One-row-per-claim flat schema — cannot answer counting/duration
queries; no room to grow.

---

## DD-003 — Scope to a query-answering system; no raw-note preservation layer
**Status:** Accepted · **Date:** 2026-05-22

**Context.** An earlier framing proposed persisting verbatim notes for provenance
and future re-extraction. The deliverable for this exercise is a working
query-answering system — re-extraction workflows and audit tooling would expand
scope past the time budget.

**Decision.** Persist only what queries need: the `claims` and `events` tables.
Do **not** build a persisted raw-notes layer, re-extraction workflows, or
`--explain` audit tooling. Raw notes still exist as the source files on disk and
as in-memory `Note` objects during a pipeline run — they are just not a
persisted, queried layer.

**Why.** Keeps the build focused on the brief's core ask (ingest → store →
query). Provenance and re-extraction are valuable in a production insurance
product but are deliberate non-goals for a time-boxed exercise.

**Optional, near-free.** If light auditability is wanted later, a single
`source_excerpt` text field on `events` can be kept without adding a notes table
— deferred unless a query needs it.

**Rejected.** Full raw-note + re-extraction + audit infrastructure — correct for
production, scope creep here. (This revises the original DD-003, which proposed
exactly that.)

---

## DD-004 — Use SQLite as the store
**Status:** Accepted · **Date:** 2026-05-22

**Context.** The brief requires something easy to run on a developer's machine,
and wants "AI-written queries."

**Decision.** Store extracted data in SQLite (single file, zero setup, SQL-native).

**Why.** Zero-config, runs anywhere, speaks SQL (the natural interface for both
human and AI-written queries), comfortably handles thousands of claims. All DB
access goes through one repository module, so swapping to Postgres later is a
connection-string change.

**Rejected.** Postgres (premature ops overhead for the exercise); a NoSQL/document
store (loses SQL aggregation, which is the whole point for corpus trends).

---

## DD-005 — Hybrid extraction (rules + LLM)
**Status:** Accepted · **Date:** 2026-05-22

**Context.** Some facts follow strict templates (`Activity: Reserving` notes);
others are buried in free prose (was an appointment attended or just scheduled?).

**Decision.** Use deterministic rules/regex where the text is structured, and an
LLM only where genuine language understanding is required.

**Why.** Rules are free, instant, 100% reproducible, and testable — right for
exact financial data. The LLM is reserved for semantic judgment. Putting reserve
math through an LLM would be reckless; forcing RTW detection into regex would be
brittle.

**Rejected.** Pure-LLM extraction (cost, non-reproducibility on exact numbers);
pure-rules extraction (cannot handle messy narrative prose).

---

## DD-006 — Per-note extraction + dedup/resolver stage
**Status:** Accepted · **Date:** 2026-05-22

**Context.** Notes restate facts constantly — every Resolution Strategy snapshot
repeats the surgery, diagnosis, etc. A single appointment is described across
multiple notes (booked in one, attended in another).

**Decision.** Extract events per-note (small, cacheable, parallelizable prompts),
then run a dedicated Resolver stage that merges duplicate mentions into one
canonical event.

**Why.** Per-note granularity gives natural provenance and cheap prompts.
Resolution is required anyway — e.g. query 4 (scheduled→seen lag) needs the
booking note and the visit note merged into one appointment event.

**Rejected.** Whole-claim single extraction call — huge prompt, no per-fact
provenance, still needs dedup of internal restatement.

---

## DD-007 — JSON `attributes` column for type-specific payloads
**Status:** Accepted · **Date:** 2026-05-22

**Context.** Different event types need different fields; the taxonomy will keep
growing.

**Decision.** The `events` table has stable columns (`event_type`, `event_date`,
provenance, confidence) plus a JSON `attributes` column holding type-specific
fields.

**Why.** Adding a new event type needs no schema migration. A query-hot type can
later be promoted to its own typed table with indexes.

**Rejected.** A table per event type now — premature; lots of migrations while
the taxonomy is still moving.

---

## DD-008 — Three-tier query layer
**Status:** Accepted · **Date:** 2026-05-22

**Context.** The brief wants queries by "humans or AI"; correctness must be
trustworthy.

**Decision.** Layer the query surface from safe to flexible: (1) canned, tested
query functions per sample query + corpus aggregates; (2) raw SQL over the event
schema; (3) NL→SQL via LLM as a stretch goal.

**Why.** Canned functions are the reliable demoable deliverable. Raw SQL exposes
corpus trends. NL→SQL satisfies "AI-written queries" — but the LLM only *writes*
SQL, it never *computes* answers; all arithmetic stays in SQL over verified data.

**Rejected.** LLM-computes-the-answer — puts a non-deterministic component in the
path of the actual numbers.

---

## DD-009 — Defer eval harness from MVP scope; keep as nice-to-have extension
**Status:** Accepted · **Date:** 2026-05-22

**Context.** An eval harness with a hand-labeled gold dataset is genuinely
valuable — the brief lists evals as an X-factor — but it is a self-contained
extension on top of the system, not a prerequisite for delivering the 4 sample
queries.

**Decision.** Defer the eval harness and `evals/gold.json` from the MVP build.
Keep them designed-for: §7 of `DESIGN.md` sketches the shape so they can be
added later without rework.

**Why.** Scope control. The MVP correctness story is the four canned queries
producing the right answers on the two sample claims, verified manually against
the source notes; unit tests cover mechanical regressions. The eval harness is
the polish layer on top of that, not the foundation.

**Rejected.** Building evals inside MVP scope — valuable work, but not where the
time is best spent given the brief's depth-over-breadth weighting.

**Revises.** Supersedes the earlier framing of evals as in-scope.

---

## DD-010 — Model claims as injury-or-illness from day one; use loss-neutral vocabulary
**Status:** Accepted · **Date:** 2026-05-22

**Context.** Workers' compensation covers both **discrete-event injuries**
(a fall, a lifting strain) and **occupational illness / disease** (carpal
tunnel from repetitive motion, silicosis from dust exposure, occupational
hearing loss, dermatitis, etc.). Both sample claims happen to be injury
claims, but a real corpus contains a meaningful share of illness claims.
The original schema named the anchor date `date_of_injury` and the
description `injury_description` — vocabulary that silently excludes a
whole compensable population.

**Decision.** Use loss-neutral vocabulary on the `Claim` row from v1:
- `date_of_loss` instead of `date_of_injury` (matches the notes' own
  `DOL` / `FOL` terminology).
- `loss_description` instead of `injury_description`.
- Add a `claim_type` enum (`injury | illness`) as the corpus-query axis.
  (Originally drafted as a three-value enum `injury | occupational_disease
  | cumulative_trauma`; trimmed to two values per DD-012 — nothing in the
  MVP queries by the finer distinction. Sub-types can split out later when
  a query needs them, via DD-007.)

The MVP build still only ingests injury claims (both samples are
`injury`), but the schema and query layer no longer assume that.

**Why.** Renaming columns later is cheap in code but expensive in
trust — every downstream consumer (canned functions, AI-written SQL,
analysts) would have to relearn the schema. Better to commit to honest
vocabulary now. The `claim_type` axis is needed for any corpus
aggregation to be meaningful: "average days-to-RTW" mixed across injury
and occupational-disease claims is a misleading number, because the
populations have structurally different timelines.

**Rejected.** "Inject only for MVP, rename later" — defers a cheap rename
into an expensive migration, and produces a schema whose names lie about
what it actually models. Also rejected: separate `Claim` tables per
type — premature; the shared columns vastly outweigh the differences,
and `claim_type` + targeted attributes (e.g., illness-claim exposure
history) cover the divergence.

**Follow-ons (not built).** Illness claims would add structure not
present in the injury samples: an *exposure-history* event series, a
*causation-opinion* event type (medical opinion linking the condition
to workplace exposure), and ICD-10 codes drawn from `J` / `L` / `H` /
`G` ranges rather than `S` / `M`. These plug into the existing event
taxonomy via the `Extractor` interface — no schema changes required.

---

## DD-011 — Model "never returned to work" as a first-class terminal event
**Status:** Accepted · **Date:** 2026-05-22

**Context.** Q1 ("how long to RTW?") has a non-obvious failure mode:
several different real-world outcomes all produce "no `return_to_work`
event" — pending recovery, permanent total disability (PTD), claimant
deceased, voluntary separation, settlement closing the case without RTW.
Treating all of these as `null` silently filters PTD/deceased/separated
claims out of corpus averages and tells an analyst nothing about *why*
there's no RTW.

**Decision.** Add an `rtw_terminal` event type to the taxonomy. It
records the definitive "this claim will never have an RTW" outcome with
a `reason` attribute (`ptd | deceased | separated | closed_no_rtw`) and
an effective date. Q1's canned function returns a discriminated union:
`returned` / `never_returned (reason)` / `pending`, eliminating the
ambiguous `null`.

**Why.** Encoding the negative outcome in the event timeline — rather
than inferring it inside the canned function — means every query gets
the signal for free. Future queries like "% of claims closing without
RTW," "PTD rate by jurisdiction," or "average time-to-settlement for
non-returners" are simple aggregations over `rtw_terminal`, not
re-extraction. Mirrors the same principle that put `appointment.status`
in the stored data instead of at query time.

**Rejected.** Returning a nullable number from Q1 and adding the
discrimination logic inside the function — produces the right answer
once, doesn't help any other query, and hides a definitive negative
("PTD") as missing data. Also rejected: a separate `claim_closure`
table — premature; the event taxonomy already absorbs this without a
schema change.

---

## DD-012 — Trim `Claim` and event taxonomy to query-minimum; defer speculative fields
**Status:** Accepted · **Date:** 2026-05-22

**Context.** During design discussion the `Claim` table accumulated columns
(`loss_description`, `body_parts`, `diagnoses`, `avg_weekly_wage`,
`comp_rate`) and the event taxonomy accumulated a `work_status_change`
type. None of these are read by any of the four sample queries. They were
added as "useful data to have" or "obvious near-term axis" — speculative
scope, not query-driven.

**Decision.** Apply strict YAGNI to the data model:
- **`Claim`** keeps only: `claim_id`, `account`, `jurisdiction`,
  `claim_type`, `date_of_loss`, `source_file`, `ingested_at`. All
  query-required or free from the file header.
- **Event taxonomy** keeps only: `reserve_change` (Q3), `appointment`
  (Q2/Q4), `return_to_work` (Q1), `rtw_terminal` (Q1 definitive
  negative). Every active type is tied to a current query.
- `work_status_change` moves to the "future event types" section — to
  be added when a query (e.g., "offer-to-acceptance time," "% of
  pending claims with outstanding offers") actually consumes it.
- Q1's `pending` case is intentionally undifferentiated — just `status`
  + `daysOpen`. No sub-reason. Differentiating *why* a claim is
  pending is recorded as the extension path in `q1-return-to-work.md`
  §6.1.

**Why.** Every speculative field bloats either the LLM extractor's
contract (more fields to infer, more failure modes) or the schema's
surface area (more columns to maintain, document, and answer questions
about). Both costs are real; both compound; neither buys anything until a
query consumes the data. The JSON `attributes` design (DD-007) makes
adding fields later cheap — pure addition, no migration — so the cost of
*not* adding now is essentially zero.

**Rejected.**
- *Keep speculative fields because they're "near-free at extraction"* —
  ignores the ongoing maintenance and explanation cost; also not true for
  fields like `loss_description` that require LLM judgment.
- *Add a `pending_reason` sub-enum to Q1 now* — would require the LLM to
  infer reasons from absence-of-evidence (speculation), and no current
  query reads it.
- *Drop `claim_type` too* — user retained for injury-vs-illness corpus
  separability; the column is essentially free (header / first-note
  inference) and provides the only currently-meaningful corpus filter.

**Revises.** Supersedes the broader `Claim` schema sketched in earlier
revisions of `DESIGN.md` §3.1 and `data-modeling.md` §2. DD-010's
loss-neutral naming (`date_of_loss`, `claim_type`) is preserved; only
the speculative columns are dropped.

---

## DD-013 — Normalizer parses header dates into typed fields; body text preserved verbatim
**Status:** Accepted · **Date:** 2026-05-23

**Context.** The Normalizer's stated job is to centralize date parsing
so format variance (`5.21.25` vs `5-21-25` vs `May 21 2025`) doesn't
leak into every extractor. The obvious-but-wrong implementation is to
rewrite *all* dates — including those inside note bodies — into
ISO-8601 in-place. That produces a body where every date is uniform,
which sounds appealing but breaks downstream invariants.

**Decision.** The Normalizer parses the **header date** of each note
into a typed ISO field (`note_date`) on the `Note` object. The note
**body is preserved verbatim** apart from lossless character-level
cleanup (mojibake repair, line-ending and whitespace normalization).
Body dates remain in their original surface form (`Apr 17 2025 9:00AM`,
`5.21.25`, etc.). The date-parsing logic lives in a **shared utility
module** that extractors import and call directly on body substrings
when they need an ISO value.

**Why.** Three reasons, in order of severity:
1. **The LLM `evidence_quote` substring check depends on body
   fidelity** (extractor.md §6, §8). The LLM extractor copies a
   verbatim phrase from the body into `evidence_quote`; a post-LLM
   safety net verifies the quote is a substring of the body. If the
   Normalizer rewrites `Apr 17 2025` → `2025-04-17` in the body, the
   LLM either copies the rewritten form (and an auditor reading the
   original source can no longer trace the extraction) or copies the
   original form (and the substring check fails). Either way, the
   anti-hallucination guarantee breaks.
2. **Date detection in arbitrary prose is ambiguous.** `5/4` could be
   May 4 or April 5; `the 21st` is contextual; `4 weeks` is a
   duration, not a date. Rewriting in-place risks silently mangling
   text when the parser was wrong. Preserving the body means the
   worst case is "the extractor's call to the date parser returns
   null" — a recoverable miss, not a corrupted note.
3. **Centralization is achieved by shared code, not by mass rewrite.**
   The promise of DESIGN.md §4.2 ("date parsing is centralized in
   one module") is satisfied by exporting one well-tested `parse_date`
   function that both the Normalizer (for headers) and the extractors
   (for body substrings) call. The benefits — single test suite,
   one place to change the two-digit-year pivot, one place to fix a
   parsing bug — apply identically; only the *mechanism* differs.

**Rejected.**
- *Rewrite all dates in the body to ISO in-place* — breaks the
  evidence-quote substring check; risks mangling ambiguous strings;
  conflates lossless character cleanup with lossy semantic rewriting.
- *Annotate dates with sidecar markup in the body* (`<date
  iso="2025-04-17">Apr 17 2025</date>`) — pollutes the body with
  machine markup the LLM would have to ignore, complicates the
  evidence-quote check, and adds a parsing dialect for no real gain
  over a shared `parseDate` utility.
- *Pre-extract a structured `body_dates: ISODate[]` sidecar field on
  the `Note`* — speculative; no extractor currently needs it, and
  every extractor already knows which substring it cares about, so
  the on-demand `parseDate(substring)` call is sufficient.

---

## DD-014 — LLM extractors retry transient failures up to 3 attempts with backoff, then drop+warn
**Status:** Accepted · **Date:** 2026-05-23

**Context.** Both LLM providers exhibit transient failure modes —
Gemini emits `503 UNAVAILABLE` on demand spikes, OpenAI emits `429`
rate-limits, both can hit generic network blips. During the Phase 11
verification ingest of claim 2, 3 of ~75 Gemini calls hit `503`. The
original Phase 8 policy was "catch `LLMError`, return `[]`" — the
events those notes would have produced were silently lost. With
~4% failure rate at this scale, recall is unreliable in a way that
isn't visible at the query layer.

**Decision.** Wrap each provider's `structured()` API call in a
bounded-retry helper (`src/claims/llm/retry.py`). Up to **3 total
attempts** with **exponential backoff** (1s, then 3s between
attempts) **plus ±50% jitter** on each sleep. After the final
attempt, the exception propagates and the extractor's existing
`except LLMError: return []` catches it — so the pipeline still
completes, but only after a real effort to recover. Each retry is
logged at INFO; the final failure logs at WARNING.

**Why.**
- **Transient failures are the dominant class.** Of the ~3 failures
  observed in the verification run, all three were `503 UNAVAILABLE`
  on Gemini — exactly the case retry is designed for.
- **Bounded budget keeps cost predictable.** 3 attempts is a hard
  cap; even a sustained provider outage adds at most ~6 seconds of
  delay + 2 extra calls per note. Worst case across both samples:
  ~400 extra LLM calls in a row, still within free-tier daily limits.
- **Jitter desynchronizes retry storms.** When a provider has an
  outage that resolves at a specific moment, every client that hit
  the same fixed backoff would retry at the exact same time and
  re-overload the API the instant it came back up. ±50% multiplicative
  jitter on each sleep spreads retries over a window, smoothing the
  recovery curve. Same principle as TCP backoff and AWS's "full
  jitter" pattern.
- **Drop+warn at the end preserves pipeline progress.** A single
  unrecoverable note doesn't block the rest of the claim — important
  for long-running batch ingests. The WARNING line in the run log
  makes the loss inspectable.
- **Compatible with existing extractor error handling.** Extractors
  already catch `LLMError` and return `[]`. The retry helper sits
  *inside* the client, not at the extractor level — extractors get
  the same contract (succeed or `LLMError`) they had before, just
  with a higher probability of success.

**Rejected.**
- *Single-attempt silent drop (Phase 8 policy)* — what we just lived
  with. Loses data invisibly; ingest output isn't reproducible across
  runs because the same input can produce different event counts.
- *Sidecar deferred-extraction queue* — a separate table for failed
  notes plus a re-run command. Correct in the limit but adds a whole
  reconciliation surface for marginal gain when transient errors are
  already mostly catchable by retry.
- *Fail-loud when failure rate > N%* — appealing but brittle. One
  bad token (e.g. a deprecated model) would block the entire run,
  and the "right" threshold depends on the corpus.
- *Unbounded retry with longer backoff* — risks runaway cost and
  obscures genuine systemic issues. The 3-attempt cap forces operator
  attention on persistent failures.

The retry helper deliberately accepts a `NonRetryableError`
sentinel: a future need to skip retries on deterministic failures
(schema validation, prompt refusals) has a hook ready without
expanding the policy now.

---

## DD-015 — Per-note extraction is parallelized across notes
**Status:** Accepted · **Date:** 2026-05-23

**Context.** Verification ingests of the two sample claims each
took ~2 minutes serially — almost entirely wall-clock time on
serial LLM network round-trips. DD-006 makes per-note extraction
independent (no cross-note state during extraction; the Resolver
handles cross-note merge afterward). So the work is
embarrassingly parallel; only the rate-limit ceiling stops us
from going wider.

**Decision.** Use a `concurrent.futures.ThreadPoolExecutor` in
`__main__._cmd_ingest` to fan out `run_all(note, …)` calls across
notes. **Default `workers=4`**; configurable via `--workers N`
(use `--workers 1` for fully serial). Threads (not async) because
the underlying SDKs are sync and threading is GIL-friendly for
I/O-bound work.

**Why.**
- **Embarrassingly parallel.** DD-006 already commits us to
  per-note extraction with no shared state; the only thing serial
  was the orchestrator loop.
- **4 workers fits Gemini free-tier RPM ceiling.** With up to 3
  LLM extractors firing per note, 4 workers peaks around 12 RPM
  vs. the 15 RPM free-tier limit on `gemini-2.5-flash-lite`.
  Higher concurrency would trip the limit; lower leaves too much
  wall-clock on the table.
- **DD-014's retry-with-jitter absorbs the 429s.** Concurrent
  workers occasionally crowd the rate limit; jittered backoff
  spreads the recovery. The two DDs work together — neither is
  fully effective without the other.
- **Cheap to opt out.** `--workers 1` restores the pre-DD-015
  serial behavior bit-for-bit. Useful when an evaluator wants
  deterministic event-emission order, or when running against a
  shared free-tier quota with other workloads.
- **No async refactor.** ThreadPoolExecutor is a one-import,
  ten-line change. Going async would touch the entire LLM client
  layer and the SDK call sites for marginal additional throughput.

**Rejected.**
- *Async/await throughout* — broader rewrite, no clear win when
  the SDKs are sync.
- *Multiprocessing* — overkill for I/O-bound work; adds pickling
  overhead.
- *Higher default (e.g. 8 workers)* — would exceed Gemini free-tier
  RPM and rely entirely on retries to recover. Bad default for the
  exercise's primary expected provider.
- *No-cap parallelism* — unbounded concurrency invites runaway
  spend on paid tiers and retry storms on free tier.

The order in which events are emitted into `raw_events` becomes
non-deterministic with workers > 1. This doesn't affect
correctness — the Resolver orders deterministically and stored
events carry their own dates — but tests that asserted order
would need adjustment. Existing tests call extractors directly
on a single note, not through the parallel orchestrator, so they
are unaffected.

---

## DD-016 — Appointment identity is `(claim_id, encounter_date, party-overlap)`; LLM emits a `parties` list
**Status:** Accepted · **Date:** 2026-05-23

**Context.** The Phase 12 verification audit surfaced a class of
resolver bugs all rooted in one place: `AppointmentAttributes.provider`
was a single free-form string carrying two distinct concepts (the
clinician *and* the facility) into a regex-based canonicalizer that
had to guess which one it was looking at. Concrete fallout:

- `"Dr. Harmon"` and `"Dr. Harmon's office"` canonicalized to
  different keys (`"harmon"` vs `"office"`), so the same encounter
  fragmented into two Q2 rows.
- Generic phrases (`"office"`, `"my office"`, `"ophthalmology"`)
  survived as standalone "providers" and inflated Q2 counts.
- Same provider with multiple visits in a `±N day` window merged the
  wrong scheduled/attended pair (Q4 negative-lag outliers).

Patching the regex (strip `'s office`, add stop-word lists) closes
specific cases but does not address the structural cause: **string
canonicalization is the wrong tool for entity resolution**, and the
schema field forces the LLM to discard information by picking one
party when an encounter has multiple.

**Decision.** Three coordinated changes:

1. **Schema.** Replace `AppointmentAttributes.provider: str | None`
   with `parties: list[str]` — every named individual *and* every
   named organization/facility involved in the encounter, as a set
   of strings. Empty list = no identifiable party.

2. **LLM contract.** The Appointment extractor's prompt is rewritten
   around a bounded definition of "party":

   > Emit, in `parties`, every named individual or organization that is
   > **party to this specific appointment** — attending clinician(s),
   > the facility/clinic where the appointment occurs, and any case
   > manager/interpreter physically present. Do **not** include:
   > referring physicians named only in history, other clinicians
   > mentioned in diagnosis or plan, specialty names
   > (`"ophthalmology"`, `"spine surgery"`), or generic phrases
   > (`"office"`, `"clinic"`, `"the doctor"`).
   >
   > Each entry names one entity. Strip honorifics, location suffixes
   > (`'s office`, `at <X>`), and degree suffixes before emitting.

3. **Resolver merge key.** Two appointment events merge iff:

   ```
   same claim_id
   AND same encounter_date           (exact match; no ±N window)
   AND parties_overlap(a, b)          (≥1 shared entity, or either side empty)
   ```

   `encounter_date = scheduled_for_date or occurred_on`. Party
   comparison uses **deterministic normalization** (lowercase,
   collapse whitespace, strip remaining honorifics/degree suffixes,
   `&` → `and`) and **exact set intersection**. No fuzzy matching,
   no edit distance, no probability threshold.

**Why.**

- **Removes the fragmentation class entirely.** `"Dr. Harmon"`,
  `"Dr. Harmon's office"`, and `"Harmon at Orthopedic & Spine"` all
  emit `"Harmon"` in `parties` (the LLM strips suffixes with full
  note context). Any cross-note reference that names either Dr. Harmon
  or the same clinic will overlap and merge. The previous design
  couldn't do this because a single field can't carry two concepts.

- **Captures the multi-party reality of clinical encounters.** An
  appointment has a clinician *and* a clinic *and* sometimes an FCM
  in attendance. Different notes surface different subsets. A set
  with overlap as the match rule lets notes share *any* identifier
  to be recognized as the same encounter — strict win over forcing
  a single canonical party.

- **Pushes disambiguation upstream, to where the context lives.**
  The LLM is reading the whole note when it decides whether
  `"Dr. Harmon's office"` is Harmon-the-person or a place called
  "office". The resolver, looking only at the extracted string,
  never had that context — every heuristic it ran was guesswork.
  Now the LLM commits to the parties; the resolver compares them
  deterministically.

- **Exact date + party overlap kills the cross-date false-merge bug
  for free.** The `±N day` window existed to absorb date-parsing
  fuzz, but it was also absorbing Marker mis-attributions (a
  historical "Next Office Visit: 4-17-25" in a July note merging
  with an April MRI). Replacing the window with exact equality
  ejects these. Date-format ambiguity belongs in the Normalizer
  (DD-013), not the resolver.

- **No fuzzy / similarity matching.** Surnames are short (6–8
  chars); 75% edit-similarity merges `"Harmon"` and `"Harman"`.
  Org names share boilerplate (`"Group"`, `"Center"`); 75%
  similarity merges `"Spine Surgery Group"` and `"Spine &
  Neurology Group"`. Fuzzy matching fails *silently* (downstream
  query produces a wrong number with no signal); exact match
  fails *loudly* (a visible duplicate row that can be diagnosed).
  In a system whose correctness story rests on `evidence_quote`
  substring checks (DD-013), giving up determinism at the merge
  step undermines the chain of custody. Residual same-entity
  variants that survive deterministic normalization are handled by
  an **explicit alias table** (deferred until needed), not by a
  probability threshold.

- **The set-empty escape hatch keeps coverage high.** When either
  side has `parties = []` (e.g. the Marker extractor saw only a
  date), party overlap doesn't *block* the merge — date alone
  carries it. Q4's schedule-to-seen pairing depends on this:
  the booking note often names the doctor, the attending note
  often names only the clinic; either may be empty, both merge by
  date.

**Rejected.**

- *Regex band-aid on `canonicalize_provider`* — strip `'s office`,
  add stop-word list. Fixes the visible cases, leaves the structural
  cause (one field, two concepts; resolver doing string heuristics
  without context) intact. Next surface variation reproduces the bug.

- *Single `attending_party` (priority: person > org > null)* —
  cleaner than the regex approach, but **forces the LLM to discard
  information**. When Note A names the doctor and Note B names only
  the clinic for the same encounter, the chosen canonical party
  differs and they don't merge. Set-with-overlap dominates this
  design on coverage with no precision cost.

- *Fuzzy string matching (75% Levenshtein, Jaro-Winkler, etc.)* —
  see "no fuzzy matching" above. Surname / org-name collision rates
  at any reasonable threshold are too high; silent failures are
  worse than visible duplicates; deterministic normalization plus an
  alias table covers the legitimate cases.

- *`±N day` proximity window on `encounter_date`* — was load-bearing
  in the previous resolver and was responsible for the Q4 negative-
  lag outliers (`−113d`, `−177d`). Exact equality is correct;
  date-interpretation noise belongs in the Normalizer's date parser,
  not in the resolver's merge logic.

- *Provider as a first-class `Provider` entity with persistent IDs +
  resolver stage that runs before appointments* — the correct
  long-term answer for a production system (Wikidata-style entity
  resolution: learn aliases over time, IDs not strings), but
  out of scope for this exercise. The `parties` list with explicit
  aliases is a clean step in that direction — when the alias table
  starts to hurt, promoting it to a `Provider` table is purely
  additive.

**Consequences.**

- `provider.py` collapses from the `_is_person()` classifier to a
  deterministic `normalize()` (~10 lines). Renamed to `parties.py`.
- `appointments.py` `_should_merge` becomes ~10 lines: claim
  equality, date equality, set-overlap check. No window parameter.
- LLM prompt rewritten with the bounded "parties" definition above;
  schema field renamed.
- Marker extractor emits its captured provider string (or empty)
  as a single-element `parties` list.
- Q2 output: per-row `parties: list[str]` field; the display string
  picks the longest party from the set if a single label is needed.
- Q4 unaffected at the function level — already keys on
  `scheduled_for_date`/`occurred_on`; benefits indirectly from
  cleaner upstream merges.
- The provider canonicalization fragmentation, the generic-string
  false-positive class, and the cross-date false-merge class (issues
  #1, #2, #3, #4, #5, #7 in the post-verification audit) collapse
  to a single root-cause fix. Status-precedence-demotion (#5b) and
  negation-reconciliation (#6) remain as separate, narrower issues.

**Known residual failure mode.** Two different clinicians who happen
to see the same claimant at the **same facility on the same date**
will be merged incorrectly. Concretely: if Note A describes a visit
on 4/22 with `parties = ["Caldwell", "Spine & Neurology Group"]` and
Note B describes a separate same-day visit at the same practice with
`parties = ["Farano", "Spine & Neurology Group"]`, the merge key
matches (same claim, same `encounter_date`, overlap on
`"Spine & Neurology Group"`) and the two encounters collapse into
one.

This is the price of using set-overlap as a tie-rule: a *shared*
party (the facility) is sufficient evidence even when the *clinician*
differs. The same-day-multi-clinician-at-one-practice pattern is the
case where this rule misfires.

Why we accept it for the MVP:
- It does not occur in either of the two sample claims, and the
  pattern is rare in single-claim corpora (a patient seeing two
  different specialists on the same day at one practice is unusual
  outside hospital inpatient stays).
- The visible artifact is an undercount, not a wrong-merge of
  unrelated events — both real encounters are present in the merged
  record's `parties`, so the loss is *one Q2 row*, not the contents
  of one.
- The fix is local and additive when it becomes necessary: tighten
  the overlap rule from "any shared party" to "shared **person**
  party" (`{p for p in a.parties if is_person(p)} & {…}`), with the
  facility treated as confirming evidence rather than identifying
  evidence. Defer this until a real corpus shows the pattern matters.

The contrast worth keeping in mind: the **fuzzy-string** failure
mode (`"Harmon"` collapsing with `"Harman"`) was *silent and
unbounded* — any two notes with similar provider strings might
merge, with no way to predict which. The **same-facility-same-day**
failure mode is *bounded* (must be same claim, same date, same
practice) and *inspectable* (the merged event's `parties` lists
both clinicians, so an auditor sees the collapse). Bounded-and-
visible is acceptable; unbounded-and-silent is not.

## DD-017 — Appointment status precedence is asymmetric; recency breaks ties
**Status:** Accepted · **Date:** 2026-05-23

**Context.** The Phase 12 verification audit surfaced a class of
Q2 false positives: the resolver was reporting attended visits
that the claimant actually missed. Concrete example in claim 2:

- 7/29 note schedules `"8/11/25 1:00pm with Dr. Caldwell"` →
  `scheduled` event for 8/11.
- A later extraction emits `attended` for 8/11 with Dr. Caldwell
  (likely from a recap that worded the historical reference in a
  way the LLM read as past-tense visit).
- 8/14 note: `"awaiting the 8/13 OV notes ... missed OV w/ Dr.
  Caldwell"` — explicit miss.
- 8/18 note: `"she was unable to attend her appointments
  scheduled for 08/11 and 08/13"` — explicit miss.

When `attended` and `missed` events for the same encounter both
reach the resolver, `_stronger_status` picked `attended` because
the previous rank was a monotonic-upward order:

    attended (4) > missed (3) > cancelled (2) > scheduled (1) > unknown (0)

That rank conflates two different signals — *confidence on a
scale* and *outcome class*. It is correct for the "upgrade from
low-information" cases (`unknown` → `scheduled` → `attended`)
where each step refines an earlier state with new evidence. It is
wrong when two notes *disagree* about the outcome.

**Decision.** Replace the linear rank with an asymmetric two-tier
rule:

1. **Negatives beat positives.** When at least one event in a
   merge group has status `missed` or `cancelled`, *that* status
   wins regardless of any `attended` / `scheduled` / `unknown`
   events in the same group. Within the negative tier:
   `missed > cancelled`.
2. **Within a single tier (all positives, or all negatives), the
   monotonic rank still applies.** `attended > scheduled >
   unknown` for positives.
3. **Recency breaks ties.** When two events have the same
   effective status, the one with the more recent `source_note_date`
   wins for any non-status attribute populated by `_merge`.

This requires propagating `note.note_date` into each event so the
resolver can compare. Added as `AppointmentAttributes.source_note_date:
date | None = None` (DD-007 makes this additive).

**Why asymmetric is the right shape.**

The asymmetry rests on extraction-cost asymmetry — how hard each
status is for the LLM to emit by accident:

| Status | How it gets emitted | False-positive risk |
|---|---|---|
| `attended` | Soft signals: clinical visit summary, narrative recap, "EE attended ...", "saw Dr. X on ...". The prompt instructs the LLM to infer attended even from administrative paragraphs that contain a date + diagnosis content. | **High.** Easy to over-infer. |
| `missed` / `cancelled` | Explicit language only: "missed", "no-show", "DNA", "unable to attend", "cancelled", "rescheduled before it could happen". | **Low.** The LLM has to find direct disconfirmation. |

When the two collide, trust the harder-to-produce signal:

- If the model wrongly emits `attended`, the asymmetric rule
  correctly demotes when a `missed` exists. This is the audited
  failing case.
- If the model wrongly emits `missed` (much rarer), the
  asymmetric rule incorrectly demotes a true `attended`. Cost
  bounded by how often `missed` false-positives occur (audit
  found zero in the two samples).

The cost trade strictly favors the audited failure mode over the
hypothetical one.

**Why recency as tiebreaker, not as primary rule.**

"Latest note always wins" was the alternative — strict temporal
precedence. Rejected because:

- The note's `note_date` is when the note was *written*, not when
  the appointment happened. A summary note written months after
  the fact has an older effective signal than an appointment-day
  observation.
- A single stray late note (e.g., a Resolution Strategy recap
  that misremembers an outcome) would unconditionally override
  every prior careful extraction.
- The asymmetric structure captures the *kind* of evidence
  (positive vs. negative); recency only matters as a tiebreaker
  among same-kind events.

Recency does the right thing at the tier-tie level: two `scheduled`
events for the same encounter (a booking + a reschedule
confirmation) keep the most recent; two `missed` events for the
same encounter (multiple confirmations) keep the most recent.

**Rejected.**

- *Monotonic-up rank (the previous Phase 9 design)* — what the
  audit caught failing. `attended` always beats `missed` even
  when `missed` came from a later, explicit note.
- *Latest-note-wins, full stop* — see above. Conflates note
  recency with evidence quality.
- *Note-relative-to-encounter rule (testimony vs. prediction)* —
  more principled, more complex. Would require categorizing
  every event as "before the encounter date" (prediction) vs.
  "after" (testimony) and only allowing testimony to override
  testimony. Asymmetric + recency gets us 95% of the value with
  one new field and a 15-line function. Reserved as a future
  refinement if asymmetric proves insufficient.
- *Require a `correction` event type for explicit retraction* —
  cleanest model long-term but pushes complexity into the LLM
  prompt and a new event taxonomy entry. Not justified by the
  current corpus.

**Consequences.**

- `_stronger_status` (pairwise comparator) becomes `_resolve_status`
  (group resolver) in `appointments.py`. The rank dict splits into
  `_POSITIVE_RANK` and `_NEGATIVE_RANK`.
- `AppointmentAttributes.source_note_date: date | None = None`
  added. Populated by both `AppointmentExtractor` and
  `AppointmentMarkerExtractor` from `note.note_date`. Not surfaced
  by Q2 / Q4 output — internal to the resolver.
- After a merge, the merged event's `source_note_date` is set to
  the latest among the merged group so subsequent re-merges
  (re-ingestion of the same claim with overlapping notes) maintain
  recency-correct behavior.

**Known limitations.**

- An LLM-emitted false `missed` will incorrectly demote a real
  `attended`. Mitigation: prompt requires explicit negative
  language for `missed`; corpus audit confirms zero false `missed`
  extractions across the two samples. If observed in a larger
  corpus, the upgrade path is the "testimony vs prediction" rule
  noted above (requires the negative to come from a note dated
  >= the encounter date, blocking pre-appointment "missed"
  emissions).
- The rule does not cover the case where the LLM fails to emit a
  `missed` event at all (silently dropping the negative). That's
  a prompt-coverage issue, separate from this decision. Verified
  separately: the 8/14 and 8/18 notes in claim 2 must produce
  `missed` extractions for the asymmetric rule to fire at all.

**Postscript (2026-05-23).** The `source_note_date: date | None`
field was widened to `source_note_dates: tuple[date, ...]` across
all four event payloads. Same semantics for the DD-017 tiebreaker
(`max(source_note_dates)` is the new "latest"), but merges now
preserve every contributing note date instead of collapsing to
one — so a merged event's audit trail names every note that fed
it, not just the most recent. Schema-additive (DD-007), no design
re-litigation; the rename just made the merged-event audit story
complete.

## DD-018 — Route the LLM appointment extractor by note shape
**Status:** Accepted · **Date:** 2026-05-23

**Context.** Running the LLM appointment extractor on every note made Contact / Investigation / Reserving notes (paperwork, emails, surveillance, scheduling chatter) the dominant source of phantom appointments — cross-attributed parties, hallucinated dates near templated ones, claimant-as-party.

**Decision.** The LLM appointment extractor runs only on notes that match one of two shapes:
- `Activity: Resolution Strategy` (periodic summaries with clean per-line attribution), or
- Body contains a `Date of Appointment:` template (canonical visit-summary form).

Marker + Reserve extractors keep running on every note (rule-based, cheap, no FP in practice). RTW / RTW-Terminal extractors unchanged.

**Why.** RS notes already deduplicated by the human author; DOA-template notes are the canonical visit-summary shape. Cuts LLM cost ~5×, eliminates the biggest phantom class.

**Rejected.** Tightening the prompt further (already long; rules compete). Strict RS-only (loses visit-summary DOA blocks like the MMI declaration note).

**Known coverage loss.** One-off mentions in narrative-only notes (e.g., retrospective ER mention in an intake's HPI section; standalone PT scheduling notices) — acceptable for Q2 count / Q4 lag.

<!-- Append new decisions below this line. Template:

## DD-0NN — <short title>
**Status:** Proposed · **Date:** YYYY-MM-DD

**Context.** <what situation forced the decision>

**Decision.** <what was decided>

**Why.** <rationale>

**Rejected.** <alternatives considered and why not>

-->
