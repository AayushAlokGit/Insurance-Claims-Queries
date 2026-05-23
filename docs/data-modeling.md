# Data Modeling — Exhaustive Reference

This document is the deep reference for **how unstructured claim notes become
rows in tables**. It exists alongside `DESIGN.md` (which keeps §3 short and
spec-like) as the long-form explanation of every modeling choice, with
worked examples drawn from the sample claim notes.

If you're reading the system for the first time, read this *after*
`claims file analysis.md` (what's in the data) and *before* the pipeline
sections of `DESIGN.md` (how the data flows). Decisions made here are
recorded in `design-decisions.md` (DD-002, DD-003, DD-007, DD-010, DD-011).

---

## 1. Why exactly two tables

A normalized first-instinct schema would have ~8 tables — `claims`, `notes`,
`providers`, `diagnoses`, `appointments`, `reserves`, `medications`,
`communications`. We deliberately don't do that. The chosen shape is **two
tables: `Claim` and `Event`**.

| Tables | Tradeoff |
|---|---|
| **1** — everything-on-claim, JSON blob | No relational queries; can't `COUNT()` reserve changes without parsing JSON in every row |
| **2** — Claim + Event (*chosen*) | Smallest schema where the four sample queries reduce to one-line aggregations; taxonomy grows without migrations |
| **5–10** — table per entity | Premature normalization; every new event type is a migration; cross-type timeline queries need many `UNION`s |

Two tables is the **minimum schema where the four sample queries reduce to
one-line aggregations**. Every query in the brief is "count / time-between /
sum over events of type X for claim Y" — that's the literal shape of the
Event table.

The split also encodes an ontological distinction:

- **`Claim`** — facts that describe **the claim itself**. Mostly static over
  the claim's lifetime (date of loss, jurisdiction, claimant wage, diagnosis
  list). Updates rare. Conceptually: *what is this claim about?*
- **`Event`** — facts that describe **things that happened on the timeline**.
  Each row has its own date. New rows added as time progresses; existing rows
  rarely change. Conceptually: *the queryable history.*

**Rule of thumb.** If you find yourself wanting to put something on `Claim`
that has its own date, it's an `Event`. If you want to put something on
`Event` that has no meaningful date, it's a `Claim` attribute.

---

## 2. The `Claim` table — column by column

Trimmed to **only what the four queries need or what comes free from the
file header** (DD-012). Anything that would require its own extraction
effort but no current query reads is deferred — adding it later is a
pure JSON-attribute or column addition (DD-007).

```
claim_id            TEXT  PK     -- "1-29RT"
account             TEXT         -- "2100000000 - Company WW"
jurisdiction        TEXT         -- "NJ", "SC"
claim_type          TEXT         -- "injury" | "illness"  (two-value enum per DD-010 / DD-012; sub-types split out later if a query needs the finer distinction)
date_of_loss        DATE         -- injury date OR diagnosis/manifestation date
source_file         TEXT
ingested_at         TIMESTAMP
```

### 2.1 `claim_id`
Primary key. Pulled directly from the file header (`Claim # 1-29RT`). Opaque
to us — we don't parse it, we don't generate it. Whatever the upstream
system uses, we use. Foreign-key target on every `Event` row.

### 2.2 `account`
`"2100000000 - Company WW"`. Stored as one string deliberately. The
dash-and-name pattern isn't worth splitting until a query needs it. If
"queries grouped by account number" becomes a use case, this becomes two
columns. Not now.

### 2.3 `jurisdiction`
`"NJ"`, `"SC"`. Important because workers' comp law is state-specific:
- SC is an MMI state (specific TTD-termination rules).
- NJ has formal reopener petitions for recurrences.
- Statutory rate caps differ by state.
- Different forms (e.g., SC Form 17 to terminate TTD).

Future queries absolutely filter by this — *"average days-to-RTW by
jurisdiction,"* *"reserve volatility in SC vs. NJ."*

### 2.4 `claim_type`
Injury-vs-illness axis from **DD-010**. Both samples are `injury`; the
column exists so the schema doesn't lie about what it can hold. Critical
for corpus aggregations — mixing injury and occupational-disease claims in
a single RTW-duration average is misleading because the populations have
structurally different timelines.

### 2.5 `date_of_loss`
Neutral name from DD-010 (matches the notes' `DOL` / `FOL` vocabulary, and
covers both injury date *and* diagnosis/manifestation date for illness).
Anchor for Q1's day count. Stored ISO-8601 (`2024-12-21`); the Normalizer
funnels the messy input formats (`12/21/24`, `12-21-2024`, `Dec 21 2024`)
through one parser.

### 2.6 `source_file`, `ingested_at`
Light operational provenance. Per DD-003 we're not building an audit /
re-extraction layer, but knowing *which file produced this row* and *when
we ingested it* is essentially free and catches "did this even ingest?"
questions during development.

### 2.7 Notably *absent* from `Claim` (deliberately deferred)

Two categories of "absent":

**(a) Deferred because no current query reads them.** Per DD-012, these
appear in the source notes and could be extracted, but the MVP doesn't:
- **`loss_description`** — prose mechanism narrative ("tripped on a rubber
  floor mat…"). Free text, no query reads it.
- **`body_parts`** (JSON array) — would be queried as a corpus axis
  ("claims involving the spine"); no current query needs it.
- **`diagnoses`** (JSON array of ICD-10 code+desc) — same. Code ranges are
  rich signal (`S`/`M` injury vs. `J`/`L`/`H`/`G` illness) but speculative.
- **`avg_weekly_wage`, `comp_rate`** — claim-level financial constants,
  cheap to capture but no current query uses them.

Each of these is a pure JSON-attribute or column addition when a query
asks for it — no schema migration, no re-architecting (DD-007).

**(b) Always absent from `Claim` (architectural, not deferred).**
- **Employer name / contacts** — not query-driven; would clutter the
  claim row with strings nobody filters on.
- **Claimant identity** — anonymized in samples; a separate privacy-aware
  concern in production.
- **Attorney info** — operational, not query-driven.
- **Treating providers** — these belong on the **events** that reference
  them, not on the claim header. A single claim can involve 6+ providers
  across specialties; flattening to claim-level loses the
  appointment-level provider-of-record information.

---

## 3. The `Event` table — the queryable layer

```
event_id          TEXT  PK
claim_id          TEXT  FK
event_type        TEXT          -- see taxonomy in §4
event_date        DATE          -- the date the event semantically occurred
attributes        JSON          -- type-specific payload
extraction_method TEXT          -- "rule" | "llm" | "merged"  (debug aid)
```

Five columns. That's it. Every query in the system reads from this table.
Keeping it small is the whole point: adding columns is a schema change;
adding **types** is just appending to a taxonomy that lives in code.

### 3.1 `event_id`
UUID, generated at extraction time. The Resolver may merge multiple
candidate events into one; the survivor keeps its `event_id`.

### 3.2 `claim_id`
FK back to `Claim`. Every event belongs to exactly one claim.

### 3.3 `event_type`
The discriminator. A small enum-like vocabulary (§4). This is what
`WHERE` clauses filter on. Index on `(claim_id, event_type)` is the
single most useful index in the system.

### 3.4 `event_date` — the critical distinction
**The date the event semantically occurred, NOT the date the note was
written.** A Resolution Strategy snapshot dated 09/01 mentioning surgery
on 12/22/24 produces an event with `event_date = 2024-12-22`, not
`2025-09-01`. The note timestamp is *evidence*; the event date is *the
fact*.

This is the single most error-prone modeling distinction. Get it wrong
and Q1's RTW duration is wildly off, Q3's reserve-change timeline is
scrambled, and Q4's schedule-to-seen gap is meaningless. Extractors must
return the **occurrence date**, not the note date.

### 3.5 `attributes` — JSON
Type-specific payload. See §4 for what each event_type expects.

### 3.6 `extraction_method`
`"rule"` | `"llm"` | `"merged"`. Pure debugging aid. If a `reserve_change`
ever shows `"llm"`, that's a regression (rule should always win on
templated notes). Cheap to store, valuable when something looks wrong.

---

## 4. Event taxonomy — every type with concrete examples

All examples are extracted from the sample notes.

### 4.1 `reserve_change` — Q3, pure rule

Source: `Activity: Reserving` note containing the line
`Indemnity for (2) Lost Time Changed to $321,014.00`.

```json
{
  "bucket": "Indemnity (2) Lost Time",
  "previous_amount": 280000.00,
  "new_amount": 321014.00,
  "delta": 41014.00,
  "author": "M.H."
}
```

**Notes:**
- `bucket` is canonicalized to the controlled vocabulary on parse
  (`Indemnity (1) Medical Details`, `Indemnity (2) Lost Time`,
  `Expense-Litigation`). String deviations are a data-quality signal,
  not silent merge.
- `previous_amount` and `delta` are computed by the Resolver after events
  are ordered by `event_date`, not extracted directly.
- `extraction_method` should always be `"rule"`; any `"llm"` here is a bug.

### 4.2 `appointment` — Q2 & Q4, hybrid

Source: a scheduling notice + a later visit record, **merged** by the
Resolver.

```json
{
  "provider": "Dr. Caldwell",
  "specialty": "neurosurgery",
  "scheduled_notice_date": "2025-08-29",
  "scheduled_for_date": "2025-09-15",
  "occurred_on": "2025-09-15",
  "status": "attended",
  "appointment_type": "office_visit"
}
```

**Conventions:**
- Three date fields live in `attributes` to support Q4 (see
  `query_feasibility_analysis/q4-schedule-to-seen.md §2`):
  `scheduled_notice_date` (when the schedule was created — drives
  access-to-care lag), `scheduled_for_date` (the booked-for date — used
  as the Resolver matching key), and `occurred_on` (when the visit
  happened). The top-level `event_date` is set to `occurred_on` if
  present, else `scheduled_for_date`. So `ORDER BY event_date` reads
  chronologically the way a human would describe it.
- `status` enum: `attended | missed | cancelled | scheduled | unknown`.
  Resolver precedence on merge: `attended > missed > cancelled > scheduled`.
- `appointment_type`: `office_visit | follow_up | ime | fce | procedure |
  phone | telehealth`. Lets Q2's canned function filter (e.g., exclude
  phone calls from clinical visit counts).
- Provider canonicalization happens here — `Dr. Caldwell, MD`,
  `Caldwell`, `the neurosurgeon` all normalize to one string before the
  merge key is computed.

### 4.3 `return_to_work` — Q1 positive case

Source: *"EE returned to modified duty on 11/10/25 as a scheduling
coordinator."*

```json
{
  "duty_type": "modified",
  "role": "scheduling coordinator"
}
```

**Notes:**
- `duty_type` enum: `modified | full`.
- A claim can have multiple `return_to_work` events (recurrence,
  post-surgical period, modified → full). **Q1 uses the first**;
  subsequent ones power future recurrence-duration queries.
- Injury-claim vs. illness-claim semantic differences (recovery vs.
  permanent accommodation vs. moved-away-from-exposure) are tracked at
  the **claim level** via `Claim.claim_type`. If a finer event-level
  distinction is ever needed, it's a pure JSON `attributes` addition,
  no schema change (DD-007).

### 4.4 `rtw_terminal` — Q1 definitive negative (DD-011)

Source: *"Claim closed via lump-sum settlement on 4/12/26; no RTW
recorded. PPD 35% awarded."*

```json
{
  "reason": "closed_no_rtw",
  "context": "lump-sum settlement; PPD 35% awarded"
}
```

**Notes:**
- `reason` enum: `ptd | deceased | separated | closed_no_rtw`.
- At most one per claim. Mutually exclusive with `return_to_work` for
  most consumers — Q1's canned function checks for either, returning a
  discriminated union (`returned | never_returned | pending`).
- Modeled as an event, not a Claim column, so every future query
  (*"PTD rate by jurisdiction,"* *"average time-to-settlement for
  non-returners"*) reads it as a normal aggregation.

### 4.5 Future event types — sketched, not built in MVP

These appear in the taxonomy with `attributes` shapes documented, but no
extractor is built for them in v1. Adding them later is **purely
additive** — no schema change.

| `event_type` | Sample `attributes` |
|---|---|
| `work_status_change` | `status`, `duty_detail`, `effective_date`, `source_authority` |
| `mmi` | `provider`, `specialty`, `body_parts_covered` |
| `surgery` | `procedure`, `cpt_codes`, `facility`, `surgeon` |
| `diagnostic_study` | `study_type`, `body_part`, `facility`, `impression` |
| `litigation_update` | `action`, `court`, `judge`, `next_hearing_date` |
| `medication_change` | `drug`, `dosage`, `action` (start / stop / change) |
| `causation_opinion` | `opinion`, `opinion_holder`, `opinion_date` (illness-claim relevant) |

`work_status_change` is the most likely first add-back — it would capture
RTW offers and restriction changes that the MVP's LLM extractor currently
rejects with no event emitted. See `q1-return-to-work.md` for the
extension path.

---

## 5. The JSON `attributes` column tradeoff

This is the single design choice that does the most work in the model.
Pressure-testing it from three angles.

### 5.1 Why JSON, not typed columns on `events`?
If every possible event-attribute were its own column, we'd have ~30
columns, mostly NULL on any given row. Adding a new event type would
require `ALTER TABLE` for every attribute it needs. Cross-type queries
(timeline by date) would work, but type-specific queries
(`WHERE bucket = '...'`) would still need NULL-aware filtering.

### 5.2 Why JSON, not a table per event type?
`reserve_changes`, `appointments`, `work_status_changes` as their own
tables would be cleaner ontologically. But:
- Every new event type is a schema migration.
- Cross-type timeline queries (*"chronological timeline of claim X"*)
  need `UNION ALL` across N tables.
- The taxonomy is still evolving — locking it into table-per-type before
  we know what types we'll need is premature.

### 5.3 Why JSON works
- SQLite has first-class JSON functions since 3.9 (`json_extract`,
  `json_each`, `json_path`).
- The `event_type` column tells you what to expect inside the JSON —
  there's no JSON-schema mystery at query time.
- **Expression indexes** are supported:
  `CREATE INDEX idx_appt_status ON events(json_extract(attributes, '$.status'))`
  if a particular path becomes query-hot.

### 5.4 The escape hatch — promotion to a typed table
If `appointment` becomes the dominant event type and JSON lookups become
the bottleneck, we **promote** it:

1. Create an `appointments` table with typed columns.
2. Migrate rows from `events.attributes` into it.
3. Update queries (the repository module hides the swap from callers).
4. Every other event type stays in the JSON column.

This is a localized refactor, not a system rewrite. DD-007 records this
as the explicit evolution path.

---

## 6. Worked example — note text to rows

Take this snippet from sample claim 1:

```
Date: 11/12/2025 02:14pm CT
Activity: Resolution Strategy
Noted By: M.H.

EE returned to modified duty on 11/10/25 as a scheduling coordinator,
working 4 hours/day. Dr. Harmon released to modified duty effective 11/10.
Next office visit 12/05/25. Indemnity (2) Lost Time reserve currently
$321,014.00 (unchanged from last review).
```

After loading and normalization, the extractors emit:

| event_type | event_date | attributes (key fields) | extraction_method |
|---|---|---|---|
| `return_to_work` | 2025-11-10 | `duty_type: modified, role: scheduling coordinator` | `llm` |
| `appointment` | 2025-12-05 | `provider: Dr. Harmon, specialty: spine, scheduled_notice_date: 2025-11-14, scheduled_for_date: 2025-12-05, status: scheduled` | `rule` (NOV anchor) |

**Note what's deliberately NOT emitted:**
- The Reserving line says "unchanged" — the rule pattern requires
  `Changed to $X`. Restating the current value is **not a change**.
  (Q3's `delta = 0` rule in action.)
- The note timestamp (11/12) is **not** any event_date — the events
  occurred on 11/10 and 12/05 respectively.

**Then the Resolver:**
- Looks for a later visit note about Dr. Harmon near 12/05 to promote the
  appointment's status from `scheduled` to `attended`.

Two rows land in `events`. Q1 reads the `return_to_work` row. Q2 will
read the `appointment` row once an attendance confirmation arrives. The
"unchanged" Reserving line in the source note produces no event (Q3's
`delta = 0` rule).

---

## 7. What is deliberately *not* modeled

Equally important — what we chose **not** to extract into events for v1:

- **Medications** — listed in notes (gabapentin, tizanidine, etc.) but no
  sample query needs them. `medication_change` slots in trivially when
  needed.
- **Imaging / diagnostics** — MRI/CT reports with IMPRESSION text. Rich
  data, no current query.
- **PT session detail** — exercise lists, frequency, HEP. Aggregatable
  later, not now.
- **Communications log** — emails/calls/texts. Operational metadata,
  not query-driven.
- **Surveillance / SIU findings** — sensitive and complex; explicit
  out-of-scope.
- **Witness statements** — relevant for compensability investigations,
  no current query.
- **Comorbidities / medical history** — captured in prose but not
  queried.

**The principle:** a piece of information is extracted into events only
when a query (current or near-term planned) needs it. Everything else
stays in the source files on disk. This keeps extraction cost down and
the schema honest about what's actually queryable. DD-003 codifies this.

---

## 8. How the model maps back to the four queries

Final sanity check — does this schema actually answer the four queries
cleanly?

| Query | What it reads | Shape |
|---|---|---|
| Q1 | `claim.date_of_loss`, `events WHERE event_type IN ('return_to_work', 'rtw_terminal')` | Per-claim lookup; discriminated union return |
| Q2 | `COUNT(*) FROM events WHERE event_type = 'appointment' AND json_extract(attributes,'$.status') = 'attended'` | Per-claim count |
| Q3 | Window function `LAG` over `events WHERE event_type = 'reserve_change'` ordered by `(claim_id, bucket, event_date)` | Per-claim, per-bucket series |
| Q4 | `events WHERE event_type = 'appointment'` with both date attributes populated; `julianday()` diff | Per-appointment list + distribution summary |

Every query is a **single-table aggregation**, possibly with a join back
to `Claim` for the anchor date. No `UNION`s. No recursive CTEs. No
extraction at query time. **The schema *is* the query API.**

That's the validation. The two-table model is small on purpose, but each
choice (Claim vs. Event split, JSON attributes, taxonomy-as-data, event
date vs. note date) is doing real work — and you can see it pay off when
each query reduces to a one-line SQL aggregation.

---

## 9. Decisions feeding this document

For traceability:

- **DD-002** — Event-centric data model (typed, dated `Event` as atomic unit)
- **DD-003** — Scope to query-answering; no raw-note preservation
- **DD-007** — JSON `attributes` column for type-specific payloads
- **DD-010** — Loss-neutral vocabulary; `claim_type` axis from day one
- **DD-011** — `rtw_terminal` as first-class definitive negative
- **DD-012** — Trim `Claim` and event taxonomy to query-minimum (YAGNI)
