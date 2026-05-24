# Claim Analysis System — Design

This is a time-boxed take-home exercise. The deliverable is a working ingestion
and query system, scoped narrowly on purpose. The brief explicitly rewards depth
over breadth — every design choice below is made under that constraint. See §8
for the scope split and `design-decisions.md` for the decisions feeding into it.

## 1. Overview

Build a service that ingests unstructured workers' comp claim notes, parses them
into a structured, queryable dataset, and supports queries over the **entire
corpus** to surface trends and insights.

**The central thesis the design hangs off of:** a claim file is a
**chronological log of events** annotated with claim-level metadata,
written in prose. Parsing it = recovering the underlying event stream
into typed, dated rows. Once you have that stream, queries are just
aggregations over it — no semantic reasoning needed at query time.
Everything downstream (event-centric schema, hybrid extraction, per-note
prompts, the Resolver) is a consequence of this single framing.

The central design constraint, called out explicitly in the brief: *if we only
needed one claim, a single LLM call would do.* The value is in turning a large
body of messy claims into a stable, normalized dataset that many queries —
written by humans or AI — can run against cheaply and reliably.

That reframes the problem. We are not building a "question answerer." We are
building an **ETL pipeline**: extract structured facts from prose once, at
ingest time (the hard, expensive, LLM-assisted step), then store them so that
queries are cheap, deterministic, and reproducible.

```
  claim file ──> Load ──> Normalize ──> Extract ──> Resolve ──> Store ──> Query
  (unstructured)          (clean,        (events)   (dedup)    (SQLite)   (SQL / canned)
                           ISO dates)
```

The six stages are detailed in §4.

## 2. Working Backwards: Queries → Facts → Extraction

The brief recommends working backwards from the queries. Doing that for the four
sample queries tells us exactly which facts the schema must capture.

| # | Sample query | Facts required | Where it lives in the notes |
|---|--------------|----------------|------------------------------|
| 1 | How long to return to work? | `date_of_loss`, first **return-to-work** event (date + duty type), and an **rtw_terminal** event for claims that will never have an RTW (DD-011) | Claim header / "Description of loss"; "EE returned to modified duty on 11/10/25"; "light duty offer effective 8/25"; "she has not returned to work since the incident" |
| 2 | How many appointments attended? | **appointment** events with a `status` (attended / scheduled / missed / cancelled) | Scheduling notices, embedded medical records (`Date of Appointment:`), "she attended her follow-up", "unable to attend her appointments scheduled for 08/11 and 08/13" |
| 3 | How many reserve changes? By how much? | **reserve_change** events: bucket, previous amount, new amount, delta | `Activity: Reserving` notes — strict template |
| 4 | Time from scheduled to seen? | **appointment** events carrying `scheduled_notice_date` (when the schedule was created), `scheduled_for_date` (booked-for date — matching key), and `occurred_on` | Scheduling notice date vs. actual visit date — two separate notes about the same appointment |

Two conclusions fall out of this table:

1. **The atomic unit is a typed, dated `Event`.** Every query above is an
   aggregation over events of one type. A small, extensible event taxonomy is
   the core abstraction.
2. **Query 4 forces event *resolution*.** A single appointment is mentioned in
   multiple notes (booked here, attended there). We must merge those mentions
   into one event before we can measure the gap. Resolution is its own pipeline
   stage, not an afterthought.

## 3. Domain Model

Two persisted tables. `Claim` holds claim-level facts; `Event` is the derived,
queryable timeline. Both are populated at ingest time from the raw note entries,
which are parsed **in-memory and not persisted** — per DD-003, the scope is a
query-answering system, not an audit / re-extraction system.

### 3.1 Claim — one row per claim
```
claim_id            TEXT  PK     -- "1-29RT"
account             TEXT         -- "2100000000 - Company WW"
jurisdiction        TEXT         -- "NJ", "SC"
claim_type          TEXT         -- "injury" | "illness"  (DD-010; loss-neutral two-value split, sufficient for MVP per DD-012)
date_of_loss        DATE         -- injury date OR diagnosis/manifestation date
source_file         TEXT
ingested_at         TIMESTAMP
```

Trimmed to **only what the four queries need or what comes free from the
file header** (DD-012). `date_of_loss` is the loss-neutral anchor (DD-010);
`claim_type` is the injury-vs-illness corpus-query axis. Fields like
`loss_description`, `body_parts`, `diagnoses`, and wage/rate constants
appear in the source notes but are not extracted into the schema until a
query needs them — a pure JSON-attribute or column addition when that
happens, no migration required (DD-007).

### 3.2 Event — the queryable layer
```
event_id          TEXT  PK
claim_id          TEXT  FK
event_type        TEXT          -- see taxonomy below
event_date        DATE          -- the date the event semantically occurred
attributes        JSON          -- type-specific payload
extraction_method TEXT          -- "rule" | "llm" | "merged"  (debug aid)
```

**Event taxonomy** (start small, designed to grow — add a type without touching
the schema). Active types are built in v1; future types are designed-for but
not built (DD-012; mirrors `data-modeling.md §4.5`).

*Active in v1:*

| `event_type` | `attributes` payload | Drives query |
|--------------|----------------------|--------------|
| `reserve_change` | `bucket` ("Indemnity / Lost Time"), `previous_amount`, `new_amount`, `delta` | Q3 |
| `appointment` (medical only) | `parties` (DD-016), `specialty`, `scheduled_notice_date`, `scheduled_for_date`, `occurred_on`, `status`, `appointment_type`, `source_note_dates` (DD-017; tuple of all contributing notes) | Q2, Q4 |
| `return_to_work` | `duty_type` (modified / full), `role` | Q1 |
| `rtw_terminal` | `reason` (`ptd` / `deceased` / `separated` / `closed_no_rtw`) | Q1 (definitive negative) |

*Future — sketched, not built:*

| `event_type` | `attributes` payload | Would drive |
|--------------|----------------------|-------------|
| `work_status_change` | `status` (OOW / modified / full / MMI), `effective_on` | Q1 `pending` sub-reasons; corpus work-status trends |
| `mmi` | `provider`, `specialty` | MMI-timing corpus queries |
| `surgery` | procedure, CPT, date | Surgery-incidence queries |
| `diagnostic_study` | study type, body part, findings flag | Imaging-utilization queries |
| `litigation_update` | event type, date | Litigation-cycle queries |

Putting type-specific fields in a JSON `attributes` column keeps the relational
schema stable while the taxonomy expands. If a particular event type becomes
query-hot, it can be promoted to its own typed table later (see §9).

## 4. Architecture

A linear pipeline of small, independently testable components. Each stage has
one job and a clear input/output contract.

```
┌──────────┐  ┌────────────┐  ┌───────────┐  ┌──────────┐  ┌───────┐  ┌────────┐
│  Loader  │->│ Normalizer │->│ Extractor │->│ Resolver │->│ Store │->│ Query  │
└──────────┘  └────────────┘  └───────────┘  └──────────┘  └───────┘  └────────┘
 split into    clean text,     notes -> raw    merge dupe    write to   SQL / NL /
 claim+notes   ISO-ify dates   event candidates  events      SQLite     canned fns
```

**4.1 Loader.** Reads a claim file, extracts the claim header (`Claim #`,
`Account #`), and splits the body into raw note entries. A single tolerant
parser handles both sample file styles — they share the
`Date: … | Activity: … | Noted By: …` entry header, which is the split anchor.
Output: one `Claim` stub + a list of in-memory `Note` records.

**4.2 Normalizer.** Two concerns: (a) destructive text cleanup of the body
(mojibake `â€"` → `—`, line-ending normalization, whitespace) — lossless
character-level repairs that prevent rule extractors and the LLM's
`evidence_quote` check from silently failing on garbled characters; and
(b) header-date parsing into ISO-8601 typed fields on the `Note`, plus a
shared date-parsing utility that extractors call on body strings. Body dates
are deliberately **not** rewritten — fidelity is preserved so the
`evidence_quote` check works, and ambiguity (e.g., `5/4`) is surfaced as a
flag rather than papered over. The Normalizer is the centralization point
for the two-digit-year pivot, yearless-date inference, and date-sanity
flagging — all the silent-bug surfaces that bite when format handling is
scattered across extractors. **See `normalizer.md` for the full algorithm,
the date-format catalog, the mojibake table, failure-mode behavior, and the
date parser's interface.**

**4.3 Extractor.** The heart of the system — see §5. Each note in →
zero or more candidate `Event`s out.

**4.4 Resolver.** Deduplicates and merges the noisy candidate stream into
the final event set. A scheduling notice and a later "she attended" note
about the same visit become one `appointment` event with both scheduling
dates and `occurred_on` populated. Matching key (per event type) — for
`appointment` (DD-016): `(claim_id, encounter_date exact, parties_overlap ≥ 1)`,
where `parties` is the LLM-emitted set of named people/orgs and overlap
uses deterministic normalization (no fuzzy matching, no ±N window).
Status precedence on conflict is **asymmetric** (DD-017):
`missed`/`cancelled` beat `attended`/`scheduled`/`unknown`; within the
negative tier `missed > cancelled`; within positives `attended > scheduled
> unknown`; ties broken by the more recent `max(source_note_dates)`.
This stage also computes derived fields like `reserve_change.delta` once
events are ordered.

The Resolver is a pure module (no LLM, no I/O) with a five-pass algorithm
and a pluggable matching-strategy interface so per-event-type accuracy can
evolve without touching extractors. **See `resolver.md` for the full
algorithm, match-key construction, merge rules, failure modes, and
testability story.**

**4.5 Store.** Writes `Claim` and `Event` to SQLite. Idempotent: re-ingesting
a claim replaces its rows, keyed by `claim_id`.

**4.6 Query.** Three access paths — see §6.

## 5. Extraction Strategy — the hard part

The brief asks how to extract data points "as reliably as possible." The honest
answer is a **hybrid**: use deterministic rules where the text is structured,
and an LLM only where genuine language understanding is needed. This is the key
tradeoff in the system.

**The hybrid principle.** Rules and the LLM aren't competing approaches; they're
complementary tools applied to different surface forms within the same note.
Rules handle templated data where correctness is exact and reproducibility is
non-negotiable (financial amounts, dates inside structured headers); the LLM
handles prose where the fact requires reading comprehension ("did the return
happen, or are we discussing it?"). Some event types — `appointment` in
particular — use both, with the Resolver reconciling the candidates downstream.

**Per-note granularity (DD-006).** Extractors run per note, never per claim.
Prompts stay small, cacheable, parallelizable; cross-note merge is the
Resolver's job. The atomic contract: *one note in, zero or more typed
`CandidateEvent`s out.*

**The Extractor interface — extensibility.** Every extractor, rule or LLM,
implements the same shape so new event types are additive: an `event_type`
attribute, a cheap `can_handle(note) -> bool` prefilter that incurs no LLM
cost, and `extract(note) -> list[Event]`. The pipeline runs all registered
extractors over each note; matches are concatenated. Adding query support =
write one new `Extractor` + register it. See `src/claims/extractor/base.py`
for the live protocol.

**The LLM contract — four anti-hallucination principles.** Schema-constrained
output (tool-use / JSON mode, never free text); discriminated union for the
empty case (`{rtw: null | {...}}`, not `{return_date: string | null}`);
required `evidence_quote` from the note body verified by post-LLM substring
check; explicit negative rules in the prompt enumerating what NOT to extract
(offers, discussions, releases). The substring check is the highest-leverage
safety net — it catches hallucinations deterministically without the LLM's
cooperation.

**Why hybrid (the tradeoff).**

| | Pure rules | Pure LLM | **Hybrid (chosen)** |
|---|---|---|---|
| Cost at corpus scale | free | $$ per note | low — LLM only where needed |
| Reproducibility | perfect | varies | perfect where it matters (money) |
| Handles messy prose | no | yes | yes |
| Auditability | high | medium | high |

Putting reserve math through an LLM would be reckless — exact, templated
financial data. Forcing RTW detection into regex would be brittle. Hybrid gets
determinism where determinism is possible and intelligence where it's required.

**See `extractor.md` for the full catalog of active extractors
(`ReserveChangeExtractor`, `AppointmentMarkerExtractor` (rule fast-path), `AppointmentExtractor` (LLM general path),
`ReturnToWorkExtractor`, `RtwTerminalExtractor`), the LLM contract in detail
with worked examples, multi-extractor coordination on a single note, failure
modes, safety nets, and the testability story.**

## 6. Storage & Query Layer

**Store: SQLite.** A single file, zero setup, runs on any dev machine, and
speaks SQL — which is exactly the interface "AI-written queries" want. It
comfortably handles a corpus of thousands of claims. Swapping to Postgres later
is a connection-string change because all access goes through one repository
module.

**Two query access paths:**

1. **Canned query functions** — one tested function per sample query
   (`q1`, `q2`, `q3`, `q4` in `src/claims/query/queries.py`, each returning
   a typed structured result — discriminated union for Q1, per-bucket
   summary for Q3, per-visit distribution for Q4). These are the reliable,
   demoable deliverable.
2. **Raw SQL** — the event schema is simple enough to query directly, and this
   is the corpus-trend surface the brief asks for: *"average days-to-RTW by
   jurisdiction,"* *"reserve volatility across all claims"* are plain `GROUP BY`s
   over the `Event` table. No extra code — the schema *is* the corpus-query API.

A guiding principle throughout: **the LLM extracts facts at ingest; it never
computes query answers.** All arithmetic runs in SQL over the verified event
data, so results are deterministic and reproducible.

## 7. Evaluation (nice-to-have, not in MVP)

Evals are deferred from the MVP scope. The architecture is designed to slot them
in cleanly later — sketched here so the extension shape is on the record.

- **Gold dataset.** Hand-label the four query answers for both sample claims in
  `evals/gold.json`. Small, but turns "did we get it right" into a pass/fail
  signal.
- **Eval harness.** Runs all canned query functions against gold and reports
  per-query accuracy — a regression net as extractors evolve.

For the MVP, correctness is established by running the four canned queries on
the two sample claims and checking the answers against the source notes. Unit
tests on the date parser, rule extractors, and resolver catch mechanical
regressions.

## 8. Scope & Tradeoffs

The brief explicitly rewards a narrow, well-built slice over broad and shallow.

**In scope**
- The 4 sample queries, end to end, working on both sample claims.
- Event taxonomy covering the four active types: `reserve_change`,
  `appointment`, `return_to_work`, `rtw_terminal`. Other types (`mmi`,
  `surgery`, `diagnostic_study`, `litigation_update`, `work_status_change`)
  are sketched but not built — see DD-012 and `data-modeling.md §4.5`.
- Hybrid extraction; SQLite store; canned + raw-SQL query paths.
- Format-tolerant loader (handles both sample file styles).

**Out of scope (deliberate)**
- Full medical extraction — detailed diagnoses, PT exercise lists, imaging
  findings — not extracted into events until a query needs them.
- Natural-language → SQL query generation — the clean event schema makes it an
  easy future add (§9), but building it is not core to this exercise.
- Eval harness / gold dataset — sketched in §7 as a nice-to-have extension; not
  built for the MVP (see DD-009).
- Confidence scoring / human-review workflow — no review surface exists to
  consume it (see DD-003).
- A UI — a CLI is enough to demonstrate the system.
- Auth, multi-tenancy, a server DB, streaming ingest — premature at this stage.

**Key tradeoffs**
- *SQLite over Postgres* — dev-machine simplicity now; clean upgrade path later.
- *Hybrid over pure-LLM extraction* — determinism and cost control on
  structured data, intelligence only where earned (§5.4).
- *JSON `attributes` column over a table-per-event-type* — schema stability
  while the taxonomy is still moving; promote hot types later.
- *Per-note LLM extraction over whole-claim* — smaller, cacheable, parallel
  prompts; the Resolver stitches cross-note context back together.

## 9. Evolution Paths

The brief weights "clear evolution paths." The design has them built in:

- **New query** → write one `Extractor` + one canned function. No schema change.
- **New event type** → add a taxonomy entry; `attributes` JSON absorbs the
  payload with no migration.
- **Corpus scale** → swap SQLite→Postgres behind the repository module;
  parallelize the per-note extractors; add a batch LLM job.
- **Hot event type** → promote `appointment` from JSON `attributes` to a typed
  table + indexes once it's heavily queried — a localized migration.
- **Higher extraction accuracy** → once the eval harness (§7) lands, we can A/B
  prompts and models against gold and ship only on a measured win.
- **Natural-language queries** → add an LLM text-to-SQL layer over the event
  schema (kept deliberately small and legible for exactly this). Read-only and
  schema-constrained — the LLM writes SQL, it never computes answers.
- **Document parsing** → embedded sub-documents (PT notes, FCE reports) become
  first-class — add extractors that recognize their markers.

## 10. Summary

The system treats claim analysis as an **ETL problem, not a Q&A problem**: parse
each claim once into a normalized timeline of typed, dated `Event`s, store them
in SQL, and let cheap deterministic queries run over the corpus. A hybrid
extractor — rules for templated financial data, an LLM for semantic judgment —
gets reliability where it's non-negotiable and intelligence where it's required. The event taxonomy and `Extractor` interface make new
queries additive, with a small eval harness sketched as a nice-to-have
extension (§7) for follow-on work.
```
