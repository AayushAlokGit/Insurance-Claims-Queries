# Implementation Plan

Phased roadmap from design-complete to a working system that answers
Q1–Q4 over the sample corpus. Each phase has a clear deliverable, an
exit criterion, and references to the design docs that govern it.

**Working principles** (carried over from the design phase):
- Depth before breadth. One stage at a time, end-to-end on a small
  slice, before broadening.
- YAGNI (DD-012). Build what the four queries need; defer the rest.
- Every LLM extractor obeys the four-principle contract (extractor.md
  §6) — schema-constrained output, discriminated-union empty case,
  required `evidence_quote` with substring check, explicit negative
  prompt rules.
- Tests land with the code, not after.

---

## Phase 0 — Project scaffold

**Goal:** repository builds, tests run, type checker passes on an
empty package.

- `pyproject.toml` managed by `uv`. Runtime deps: `pydantic` v2,
  `openai`, `python-dotenv`. Dev deps: `pytest`, `pyright`.
- `.python-version` pinned to 3.12.
- `src/claims/{loader,normalizer,extractor,resolver,store,query}/`
  package skeleton with `__init__.py` files.
- `tests/` mirror.
- `pyright` strict config in `pyproject.toml`.
- One smoke test (`tests/test_smoke.py`) asserting `import claims`
  succeeds.
- README skeleton — how to install (`uv sync`), run tests, run the
  CLI (placeholder).

**Exit:** `uv run pytest` green, `uv run pyright` clean, `uv run
python -c "import claims"` succeeds.

---

## Phase 1 — Domain model

**Goal:** `pydantic` v2 models for `Claim`, `Note`, `Event` matching
data-modeling.md exactly.

- `claims.models.Claim` — 7 columns per DD-012, `claim_type` literal
  `{"injury", "illness"}` per DD-010.
- `claims.models.Note` — output shape from normalizer.md §2.
- `claims.models.Event` — four active types as a discriminated union:
  `reserve_change`, `appointment`, `return_to_work`, `rtw_terminal`.
- `attributes` JSON column modeled as a typed union per event type.
- Unit tests covering union exhaustiveness and JSON round-trip.

**Exit:** every event type and claim type representable; pyright
catches a missing case in a `match` over `event_type`.

---

## Phase 2 — Storage layer

**Goal:** SQLite schema, migrations, and a thin repository API.

- `claims.store.schema` — DDL for `claim`, `event` tables and the
  initial expression indexes on `json_extract(attributes, '$.field')`
  paths called out in data-modeling.md.
- `claims.store.db` — connection helper around stdlib `sqlite3` with
  `PRAGMA foreign_keys=ON`, JSON1 enabled.
- `claims.store.repo` — insert/query helpers; one function per
  storage need, not an ORM.
- Integration tests against a temp-file DB.

**Exit:** can insert a `Claim` + a list of `Event`s and read them
back with full fidelity. Q3-shaped query (sum of deltas per claim)
works against hand-seeded rows.

---

## Phase 3 — Loader

**Goal:** turn the sample `.md` files into a stream of `RawNoteBlock`s
(normalizer.md §2).

- `claims.loader.parse_file(path) -> Iterable[RawNoteBlock]`.
- Detect note boundaries from the headers visible in
  `sample_claim_notes/`.
- No interpretation: emit raw header string + raw body + source
  offset.
- Fixture-driven tests pinned against the two sample files.

**Exit:** both sample claims load deterministically; note count
matches a hand-counted expected value.

---

## Phase 4 — Normalizer + shared date parser

**Goal:** implement normalizer.md end-to-end. Header dates typed,
body fidelity preserved (DD-013), mojibake repaired.

- `claims.normalizer.parse_date` — the shared parser per
  normalizer.md §4. Two-digit-year pivot, yearless-date inference,
  `iso | None` + flags interface. Heaviest unit-test surface in the
  project.
- `claims.normalizer.cleanup` — the mojibake table from §3.1.
- `claims.normalizer.normalize(raw) -> Note` — orchestration.
- Failure modes: every row in §6 has a fixture.

**Exit:** every surface form in §4's table parses; every mojibake
case in §3.1 round-trips; `data_quality_flags` populated correctly
on the failure-mode fixtures.

---

## Phase 5 — Extractor: ReserveChangeExtractor (Q3)

**Goal:** start with the easiest extractor — pure regex, no LLM
(q3-reserve-changes.md). Validates the per-note `Extractor`
interface (extractor.md §4) end-to-end before introducing LLM costs.

- `claims.extractor.base.Extractor` protocol/ABC.
- `claims.extractor.reserve` implementing the regex against
  `Indemnity for ... Bucket Changed to $X`.
- Orchestration: `claims.extractor.run_all(note) -> list[Event]`.
- Tests on the sample claims; expected reserve-change counts pinned.

**Exit:** Q3 answerable end-to-end on both sample claims by chaining
Loader → Normalizer → ReserveChangeExtractor → Store → SQL query.

---

## Phase 6 — Extractor: AppointmentMarkerExtractor (rule fast-path)

**Goal:** templated-header appointments, no LLM yet
(extractor.md §5.2).

- Regex over the appointment marker headers.
- Emits `appointment` events with `status="scheduled"` from the
  templated form.
- Tests pinned against the sample claims.

**Exit:** marker-shaped appointments captured. Sets the stage for
the LLM extractor to handle everything else.

---

## Phase 7 — OpenAI client + LLM contract harness

**Goal:** the shared scaffolding every LLM extractor will use.

- `claims.llm.client` — thin wrapper around `openai` SDK, reads
  `OPENAI_API_KEY` / `OPENAI_MODEL` from env.
- Structured-outputs call helper: takes a pydantic model, derives
  the JSON schema, sets `response_format={"type": "json_schema",
  "strict": true, ...}`.
- `evidence_quote` substring-check helper — the post-LLM safety net
  from extractor.md §6.
- Per-call logging (request, response, token counts) to a local
  JSONL for audit and cost tracking.
- Mocked-response test harness so extractor tests don't hit the API.

**Exit:** a trivial extractor using the harness round-trips a pinned
fixture without touching the network in tests, and against the live
API in one manual smoke test.

---

## Phase 8 — Extractor: AppointmentExtractor LLM (general path) (Q2)

**Goal:** the general-path appointment extractor from extractor.md
§5.3 — the prefilter, the prompt, the discriminated-union response.

- `canHandle` prefilter per the simplified rule (status verbs OR
  date + appointment context).
- Prompt with explicit negative rules (paperwork, phone calls,
  authorizations rejected).
- Status literal `{"attended", "missed", "cancelled", "scheduled"}`.

**Exit:** Q2 answerable after Resolver wires the marker + LLM
extractors together (next phase).

---

## Phase 9 — Resolver

**Goal:** the five-pass algorithm in resolver.md. Load-bearing for
Q1 dedup, Q2 status precedence, Q3 `delta` derivation, Q4 cross-note
merge.

- `claims.resolver.resolve(events) -> list[ResolvedEvent]`.
- Match-key construction per event type. For `appointment`: DD-016
  `(claim_id, encounter_date exact, parties_overlap ≥ 1)` — no
  `± window`, no provider-string canonicalization (those were the
  original Phase-9 plan, replaced after the Phase-12 audit).
- Merge with **asymmetric** status precedence (DD-017):
  `missed`/`cancelled` beat `attended`/`scheduled`/`unknown`;
  ties broken by `source_note_date` recency.
- `delta` derivation for reserve changes (Q3 first-set edge case).
- `parties` normalization (`resolver/parties.py`): strip honorifics
  + degree suffixes + punctuation, lowercase, `&`→`and`, exact set
  intersection.

**Exit:** Q2 and Q3 fully answerable on the sample corpus. Q4
partially answerable (one of the two events still missing —
scheduling vs. seeing).

---

## Phase 10 — Extractors: RTW + RTW Terminal (Q1)

**Goal:** Q1 extractors per extractor.md §5.4–§5.5 and
q1-return-to-work.md.

- `claims.extractor.rtw` — explicit-evidence-only.
- `claims.extractor.rtw_terminal` — first-class negatives (DD-011),
  reason literal.
- Resolver integration: Q1 discriminated-union return
  `{"status": "returned" | "never_returned" | "pending", ...}`.

**Exit:** Q1 answerable end-to-end on both samples.

---

## Phase 11 — Query layer

**Goal:** the four canned query functions whose signatures already
exist as contracts in the q*.md docs.

- `claims.query.q1_return_to_work(claim_id) -> RTWResult`.
- `claims.query.q2_appointments_attended(claim_id) -> int |
  Distribution`.
- `claims.query.q3_reserve_changes(claim_id) -> list[ReserveChange]`.
- `claims.query.q4_schedule_to_seen(claim_id) -> list[ScheduleToSeen]`.
- CLI (`python -m claims query q1 --claim-id ...`) for manual
  inspection.
- Pinned outputs against the two sample claims.

**Exit:** all four queries return correct results for both sample
claims; CLI demonstrably runs.

---

## Phase 12 — Documentation and handoff polish

**Goal:** repo readable cold by an evaluator.

- README expanded: install, run, query examples, architecture
  one-pager pointing at the design docs.
- One end-to-end "happy path" walkthrough showing a real note →
  extracted events → query result.
- Cost/latency log from a full run on both sample claims, committed
  as an artifact.

**Exit:** evaluator can clone, `uv sync`, decrypt the OpenAI key
into `.env`, run a single command, and see Q1–Q4 answered for the
sample claims.

---

## Deferred (post-MVP)

These were explicit non-goals in the design phase (DD-009, DD-012)
and remain so:

- Eval harness with corpus-scale precision/recall metrics.
- Tightened parties-overlap (person-only) — deferred DD-016 follow-up.
- Future event types sketched in data-modeling.md §4.5
  (medical_status, work_capacity, claim_status_change, etc.).
- Per-claim-type pipeline forks.
- Pre-extracted `body_dates` sidecar (rejected in DD-013).

---

## Tracking

Phase progress is tracked via task lists during active work, not in
this file. This document is the roadmap; the task list is the
working state.
