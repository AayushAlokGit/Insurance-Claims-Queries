# Q2 — How many appointments were attended?

> **Post-DD-016/DD-017 note.** This document was authored before
> DD-016 (parties-set merge key) and DD-017 (asymmetric status
> precedence). Treat references to `provider`, `canonical_provider`,
> "provider canonicalization", `± window` / "fuzzy date window", and
> the monotonic `attended > missed > cancelled > scheduled`
> precedence as historical context. The live design is:
> - Merge key = `(claim_id, encounter_date exact, parties_overlap ≥ 1)`
>   on a LLM-emitted `parties` list (DD-016).
> - Status precedence is **asymmetric**: `missed`/`cancelled` beat
>   `attended`/`scheduled`/`unknown`; ties broken by `source_note_date`
>   recency (DD-017).
> See `docs/resolver.md §5` and `design-decisions.md` DD-016/DD-017.

## 1. What the question is really asking

A count of distinct medical appointments where the claimant actually showed
up and was seen. Three things make this the hardest of the four queries:

1. The same appointment is mentioned **multiple times** across the file.
2. Attendance is **rarely stated explicitly** — it's usually inferred from
   the existence of a visit note.
3. **Non-attendance** (missed / cancelled / rescheduled) is signaled by
   different language than attendance and must not be confused with it.

Naively counting appointment-shaped mentions over-counts by ~4×.

## 2. Facts required

`appointment` events with:

- `provider` — name and/or specialty (matching key for dedup)
- `scheduled_notice_date` — when the schedule was created (note timestamp); drives Q4 lag math, not relevant to Q2's count
- `scheduled_for_date` — the date the visit was booked *for* (Resolver matching key)
- `occurred_on` — the date the visit actually happened (null if not attended)
- `status` — `attended | missed | cancelled | scheduled | unknown`
- `appointment_type` — `office_visit | ime | fce | procedure | phone` (optional; drives the Q2-vs-clinical-care distinction — see §6)

Q2 reduces to: count distinct events where `status = 'attended'`.

## 3. Where those facts live

Appointment information appears in at least six distinct forms in the
samples (verified against `sample_claim_notes1.md` and `_2.md`):

| Form | Where | Example | What it signals |
|---|---|---|---|
| Scheduling notice (single) | `Activity: Contact` / `Investigation` notes | `"Schedule Date Time: Apr 17 2025 9:00AM \| Service Requested: Physical Therapy \| Claim #: 1-29RT"` (claim 1, L470) | `scheduled` |
| Scheduling notice (block) | Resolution Strategy `NEXT APPOINTMENT DATE(S):` lists | `"1. 8/11/25 1:00pm with Dr. Caldwell (ortho); 2. 8/13 @ 2:45 apt with Dr. Farano"` (claim 2, L115–117) | `scheduled` — **multiple events from one block** |
| Embedded visit record | Post-visit notes with structured header | `"Date of Appointment: 11-14-25"` (claim 1) / `"o Date of Appointment: 5.21.25"` (claim 2 — note `.` separator + `o ` prefix) | `attended` (strong); often paired with a `Next Office Visit:` line that's a **separate `scheduled` event** |
| Narrative confirmation | Free prose | `"She attended her follow-up with Dr. Harmon yesterday 7-25-25"` (claim 1, L240); `"On 7-25-25, EE attended her appointment with Dr. Harmon"` (L176 — date before verb) | `attended` |
| Narrative non-attendance | Free prose | `"unable to attend her appointments scheduled for 08/11 and 08/13"` (claim 2, L87); `"NOV: Fwp w/ Dr. Farano was on 8/13 however clmt had to reschedule. NOV is set for 9/23"` (L27 — past missed + future scheduled in one line) | `missed` (and sometimes also a new `scheduled` event) |
| Resolution Strategy restatement | Snapshot summaries (Resolution Strategy `Activity`) | `"Fwp w/ Occupational Health Clinic on 1/6"`, `"Seen in fwp on 1/20"`, `"* 7/3/25 Dr. Caldwell apt - scheduled for injection"` (claim 2, L154–167) | **Mixed:** duplicates of prior attended visits **and** restated scheduled events — must dedupe on `(claim, provider, date ± window)` |

A few extraction concerns these surface forms surface:

- **Date format varies across both forms and claims** — `Apr 17 2025`,
  `11-14-25`, `5.21.25`, `08/11`, `7/3/25` all appear. The Normalizer
  (DESIGN §4.2) must handle all of these.
- **One note can produce multiple events.** An embedded visit record
  with a `Next Office Visit:` line emits one `attended` event and one
  `scheduled` event. An inline `NOV:` past+future combo emits one
  `missed` and one `scheduled`. A `NEXT APPOINTMENT DATE(S):` block
  can emit 3+ scheduled events. Extractors return a list, not a single
  event.
- **Resolution Strategy restatements use the asterisk-bullet format**
  (`* M/D/YY Dr. X apt - [outcome]`) heavily. This is rule-extractable
  with a date+provider regex and saves LLM calls.
- **Provider canonicalization** — Dr. Harmon appears as "Dr. Harmon,"
  "Harmon," "the spine doctor," etc. across these forms; the Resolver's
  match key has to canonicalize before merging (§7).

**Critical observation:** *the absence of a visit record is not proof of
non-attendance.* The notes may simply not have ingested the visit yet. The
default for a scheduled-but-unconfirmed appointment must therefore be
`scheduled`, not `attended` — and Q2 only counts `attended`.

## 4. Extraction approach — hybrid

**Rule-based candidate generation.** Anchor markers in templated text
(forms catalogued in §3):

- `Date of Appointment:` → strong candidate, status `attended`
- `Next Office Visit:` / `NOV:` → status `scheduled`
- `Schedule Date Time:` → status `scheduled`
- `NEXT APPOINTMENT DATE(S):` block (Resolution Strategy) → one
  `scheduled` event per numbered entry
- Resolution Strategy asterisk-bullet (`* M/D/YY Dr. X apt - [outcome]`)
  → status inferred from the trailing text (`scheduled for…` →
  `scheduled`; default → `attended`)
- `cancelled` / `rescheduled` / `no-show` keywords in same note → status
  flag applied to the matching candidate

These rules produce *candidate* events with provisional status. **A single
note can yield multiple candidates** — an embedded visit record with a
`Next Office Visit:` line emits one `attended` + one `scheduled`; an inline
`NOV: Fwp w/ X was on M/D however clmt had to reschedule. NOV is set for M/D`
emits one `missed` + one `scheduled`; a `NEXT APPOINTMENT DATE(S):` block
can emit 3+. The `Extractor` interface (DESIGN §5.3) already returns
`CandidateEvent[]` for this reason.

**LLM judgment on the residual.** Free-prose mentions (e.g., *"she attended,"*
*"unable to attend"*) require reading comprehension. The LLM extractor for
appointments is called on notes where:

- Appointment-shaped keywords appear (`appointment`, `visit`, `f/u`,
  `follow-up`, provider names from earlier in the claim), AND
- No rule-based candidate has already been produced for that
  (provider, date) pair.

Prompt contract: *"Extract appointments mentioned in this note. For each,
state whether the note indicates it was attended, missed, cancelled, or only
scheduled. If unclear, return `unknown` — do not guess."*

## 5. Edge cases & ambiguities — Resolver responsibilities

| Situation | Resolver behavior |
|---|---|
| Same appointment mentioned in 4 notes | Merge by `(claim_id, provider, date ± window)` into one event |
| Single note emits multiple events (e.g., `NOV:` past missed + future scheduled in one line; embedded visit record with `Next Office Visit:` trailer) | Extractor returns each as a separate candidate; Resolver merges across notes as usual — no special handling |
| One note says `scheduled`, a later one says `attended` | Status precedence: `attended > missed > cancelled > scheduled` |
| One note schedules a date, no follow-up exists | Stays `scheduled` — **does not count** for Q2 |
| Two providers same day (e.g., PT + ortho) | Two separate events (provider differs) |
| Provider name varies (`Dr. Caldwell` vs `Dr C` vs `neurosurgeon`) | Normalize on (last-name, specialty); flag uncertain matches |
| Date drift (scheduled for 9/15, attended 9/16) | Match within ±N days window (N=7 default); both dates preserved |
| Phone-only check-in mislabeled as "appointment" | `appointment_type = phone` → exclude from Q2 (clinical-only count) |
| FCE / IME — are these "appointments"? | Counted by default; `appointment_type` field lets callers filter (clinical-only, IME-only, etc.). Lumping is honest to the literal question; differentiation is policy that lives at the caller. |

The precedence rule is the single most important Resolver decision for Q2:
later, stronger evidence wins. A scheduling notice cannot demote an
"attended" record.

## 6. Computation

```sql
SELECT
  json_extract(attributes, '$.occurred_on')       AS date,
  json_extract(attributes, '$.provider')          AS provider,
  json_extract(attributes, '$.specialty')         AS specialty,
  json_extract(attributes, '$.appointment_type')  AS appointment_type
FROM event
WHERE claim_id = ?
  AND event_type = 'appointment'
  AND json_extract(attributes, '$.status') = 'attended'
ORDER BY date;
```

Canned function returns the list, with count as a derived field — matching
the Q1/Q4 idiom of "always return enough to verify, never a bare scalar":

```ts
appointmentsAttended(claimId): {
  appointments: Array<{ date, provider, specialty, appointment_type }>,
  count: number
}
```

IME / FCE / phone-tagged appointments are **included by default**; the
`appointment_type` field on each row lets callers filter at the call site
(`.filter(a => a.appointment_type === 'office_visit')` for a clinical-only
count). Differentiation is policy that lives at the caller, not in the
canned function — Q2 stays faithful to the literal question.

## 7. Reliability assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Over-count from un-deduped mentions | **High** without Resolver, low with | Resolver merge key + precedence rule |
| Under-count: visit happened but only narrated in passing | Medium | LLM extractor on narrative-prose pass |
| Miscount: `scheduled` silently flips to `attended` | Low | Conservative default; status only promoted by explicit evidence |
| Phone calls mis-tagged as office visits | Medium | Extractor tags `appointment_type = phone`; caller filters. **Gold-set eval candidate when DD-009 lands** — silent mis-tagging is the failure mode. |
| Same provider, different specialty days collapsed | Low–medium | Include specialty in match key when present |

**Corpus-scale failure mode:** provider name normalization. The same person
appears as `Dr. Caldwell`, `Dr. Caldwell, MD`, `Caldwell`, `the neurosurgeon`,
`Dr C`. Without normalization, the merge key fragments and the count inflates.
A small canonicalization layer (string-normalize + last-name + specialty
fallback) is enough for MVP; entity-resolution at scale would graduate to a
proper provider directory.

## 8. Design implications

This query is the **dominant driver of the Resolver stage** (§4.4). Without
Q2, the system could arguably skip explicit cross-note dedup and just
`SELECT DISTINCT` at query time. With Q2, the merge logic is intricate
enough (status precedence, fuzzy date window, provider canonicalization) to
deserve its own pipeline stage with its own tests.

It also drives:

- The **`status` enum** on `appointment` and its precedence rule.
- **Conservative defaults** as a system-wide principle — never promote
  evidence; only demote ambiguity to `unknown`.
- Why per-note extraction beats whole-claim extraction (DD-006): a
  whole-claim prompt would have to invent its own dedup logic inside the
  model, where it can't be tested.

## 9. Injury vs. illness applicability

The six appointment surface-forms catalogued in §3 are **mostly
format-driven**, so the extraction approach transfers cleanly to
occupational illness claims (see DD-010). The deltas worth noting:

- **New appointment types appear.** Illness claims add **occupational
  medicine evaluations**, **industrial hygiene assessments**, **audiology /
  pulmonology / dermatology testing**, and **causation IMEs** that don't
  look like an ortho follow-up. The rule-based anchors (`Date of
  Appointment:`, `Schedule Date Time:`) still match — these notes use the
  same scheduling infrastructure — so MVP extraction holds. The
  `appointment_type` enum needs new values; Q2's counting logic doesn't
  change.
- **Causation IMEs may legitimately be excluded.** An "appointment
  attended" count for clinical-care monitoring shouldn't include a
  one-off causation IME the defense ordered. The `appointment_type` flag
  lets the canned function filter; the underlying event is still recorded.
- **Multi-year cadence.** Some illness claims (silicosis, asbestos) have
  decade-long surveillance schedules with yearly pulmonary function tests.
  The Resolver's `date ± window` matching scales fine; no change.
- **Provider canonicalization gets harder.** Specialty range widens from
  ortho/neuro to include pulmonology, audiology, occupational medicine,
  toxicology. The (last-name + specialty) fallback still works; the
  specialty vocabulary just grows.
- **Status semantics are identical.** Attended / missed / cancelled /
  scheduled doesn't change with claim type. The precedence rule transfers
  unchanged.
