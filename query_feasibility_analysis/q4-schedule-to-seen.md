# Q4 — How long does it take to see a provider once scheduled?

## 1. What the question is really asking

For each appointment that was *both* scheduled and attended: the gap, in
days, between the schedule notice and the actual visit. Per claim, the
output is most usefully a **distribution** (per-appointment list + summary),
not a single mean — corpus-scale consumers care about variance.

Operationally, this measures **access-to-care lag**, a real ops metric: if
the average lag is 6 weeks, that's a treatment-delay signal worth surfacing.

## 2. Facts required

`appointment` events carrying **three** date fields once merged:

- `scheduled_notice_date` — the date the *schedule notice was written / received*
  (e.g., the timestamp of the note containing `Schedule Date Time: ...` or
  `Next Office Visit: ...`). This is the "moment scheduling happened" from
  the patient's perspective.
- `scheduled_for_date` — the date the appointment was *booked for*
  (the value inside the `Schedule Date Time:` / `Next Office Visit:` line).
- `occurred_on` — the date the visit actually happened.
- `provider` (matching key).
- `status = 'attended'` (gap is undefined for missed visits).

**Why three dates, not two.** §1 says Q4 measures *access-to-care lag* —
how long the patient waited from booking to visit. That requires
`occurred_on − scheduled_notice_date`. The booked-for date is also needed,
but as a **matching key** (does this schedule notice pair with that
visit?), not as a gap endpoint. Concrete sample: claim 1 L468 (notice
04/14/25) announces a PT visit booked for 04/17/25 (L470), and the visit
record (L447) shows it occurred on 04/17/25. With *booked-for* as the
gap endpoint, gap = 0 — the visit happened on time. With
*notice-date* as the gap endpoint, gap = 3 days — the access-to-care lag.
The query needs the latter; we keep both so the consumer can also report
on-time-ness if useful.

## 3. Where the facts live — across different notes

The fundamental shape of Q4: the facts it needs are **never in the same
note**. Schedule and visit each live in their own note; the merge step
puts them on one row. Verified against `sample_claim_notes1.md` and `_2.md`.

### Two distinct scheduling-source forms

Both feed `scheduled_notice_date` (from the note's own timestamp) and
`scheduled_for_date` (from the line's date payload):

| Form | Where | Example |
|---|---|---|
| **Standalone `Schedule Date Time:` notice** | `Activity: Investigation` notes whose body is just the notice | Claim 1 L468–470 (note dated 04/14/25): `"Received notice that outpatient PT has been scheduled: Schedule Date Time: Apr 17 2025 9:00AM"` |
| **`Next Office Visit:` / `NOV:` line inside a previous visit record** | Embedded in the prior `Date of Appointment:` block | Claim 1 L489–490 (note dated 04/10/25): `"Date of Appointment: 4-11-25 ... Next Office Visit: 5-16-25 at 11am"` (one note emits both an `attended` and a `scheduled` event — see Q2 §3) |

Claim 1's ortho/spine cadence is **mostly the `Next Office Visit:` form**,
not standalone schedule notices — there's only one `Schedule Date Time:`
notice in claim 1 (L470, the PT one). Q4 cannot afford to model only the
standalone form.

### Concrete Q4 pairs in the samples

| Pair | Schedule source | Visit source | Notice → Booked-for | Booked-for → Occurred |
|---|---|---|---|---|
| **Claim 1 PT** | L468–470 (notice 04/14/25, for 04/17/25) | L447 (Date of Appointment: 4-17-25, dated 04/24/25 in the visit-record note) | 3 days lag | 0 days |
| **Claim 1 Dr. Harmon follow-up** | L489–490 (NOV inside 04/10/25 note, booked for 5-16-25) | L173 narrative confirmation: `"EE attended her appointment on 5-16-25 with Dr. Harmon"` | ~36 days lag | 0 days |

The booked-for-to-occurred number is consistently 0 for cleanly-attended
visits, which is why §2 stores both dates and §6 reports the lag from the
notice date.

### Per-claim narrative

- **Claim 1**: Most extractable pairs use the `Next Office Visit:` form
  (ortho with Dr. Harmon, neuro with Dr. Vega). One standalone notice
  pair (PT). Visit-to-visit cadence is regular — typical post-acute care.
- **Claim 2**: Pain-management cycles with Dr. Farano are the dominant
  pattern, with **multiple missed/rescheduled events** (L27, L87) that
  Q4 correctly excludes — only attended visits count. The `NEXT
  APPOINTMENT DATE(S):` block form (L115–117, L206) is heavily used
  here, in addition to standalone `Schedule Date Time:` (L861).

## 4. Extraction approach — extractor + Resolver (the cross-note merge)

Each per-note extractor produces a single-sided event:

- **Scheduling source** (standalone `Schedule Date Time:` notice, embedded
  `Next Office Visit:` line, or `NEXT APPOINTMENT DATE(S):` block) →
  `appointment` event with `scheduled_notice_date` = the note's own
  timestamp, `scheduled_for_date` = the date inside the line,
  `occurred_on = null`, `status = 'scheduled'`.
- **Visit record** → `appointment` event with `occurred_on` set,
  scheduling fields null, `status = 'attended'`.

The **Resolver** is what makes Q4 answerable. Without a merge stage, the
two halves remain two unrelated rows and the gap is uncomputable.

**Matching strategies, in increasing order of robustness:**

| Strategy | Logic | When it wins | When it fails |
|---|---|---|---|
| 1. Provider + date proximity | `(claim, provider, |scheduled_for_date - occurred_on| ≤ N days)` | Most cases (samples: 0 days off — visits attended exactly on the booked-for date) | Same provider seen repeatedly within window |
| 2. Provider + specialty + reason | Adds discriminator | Mixed pre-op / post-op with same surgeon | Reason field often missing |
| 3. Sequence pairing | Within `(claim, provider)`, pair each scheduled event with the next occurred event chronologically | Robust to date drift | Hanging schedules need a timeout rule (→ `missed` after N weeks) |

MVP picks **strategy 1 with N=14 days** as the default window, falling back
to strategy 3 when proximity is ambiguous. The Resolver interface is
designed so 2 / 3 can be swapped in without touching extractors.

## 5. Edge cases & ambiguities

| Situation | Resolver behavior |
|---|---|
| `scheduled_for_date` and `occurred_on` match exactly | Trivial merge (samples: this is the common case) |
| Visit attended on a date close to but not equal to the booked-for date | Merge within window. Lag = `occurred_on - scheduled_notice_date`; on-time delta = `occurred_on - scheduled_for_date` (slightly negative if rescheduled earlier; report as-is — do not clamp). |
| Visit happened but no scheduling notice was ingested | One-sided event; not included in Q4 |
| Scheduling notice exists but no visit within window | Stays `scheduled`; after a long timeout (say 60 days), Resolver promotes to `missed`/`unknown`; excluded from Q4 |
| Multiple schedule notices for the same visit (rescheduling) | Use the **latest** `scheduled_notice_date` before the visit — that's the booking that actually held. Earlier notices stay in the event history but don't feed Q4. |
| Same provider, two visits within window | Strategy 3 pairs each schedule with its nearest unmatched visit |
| Schedule says "TBD" / no date | Not an event — needs a date to count |
| Schedule source is a `Next Office Visit:` line inside a prior visit record (claim 1 L490) | Extractor emits two events from one note (per Q2 §3): one `attended` for the current visit, one `scheduled` whose `scheduled_notice_date` = the *containing* note's timestamp |

The "latest scheduled_for before visit" rule for reschedules is important:
the original schedule date is operationally less interesting than the one
that actually produced the visit.

## 6. Computation

```sql
SELECT
  claim_id,
  json_extract(attributes, '$.provider')                              AS provider,
  json_extract(attributes, '$.scheduled_notice_date')                 AS scheduled_notice_date,
  json_extract(attributes, '$.scheduled_for_date')                    AS scheduled_for_date,
  json_extract(attributes, '$.occurred_on')                           AS occurred_on,
  julianday(json_extract(attributes, '$.occurred_on'))
    - julianday(json_extract(attributes, '$.scheduled_notice_date'))  AS lag_days,
  julianday(json_extract(attributes, '$.occurred_on'))
    - julianday(json_extract(attributes, '$.scheduled_for_date'))     AS on_time_delta_days
FROM event
WHERE event_type = 'appointment'
  AND json_extract(attributes, '$.status') = 'attended'
  AND json_extract(attributes, '$.scheduled_notice_date') IS NOT NULL
  AND json_extract(attributes, '$.occurred_on') IS NOT NULL;
```

Canned function:

```ts
scheduleToVisitLag(claimId): {
  perVisit: Array<{
    provider, scheduled_notice_date, scheduled_for_date, occurred_on,
    lag_days,            // primary access-to-care metric (§1)
    on_time_delta_days   // 0 for on-time; negative if seen early; positive if late
  }>,
  lag: { median, p90, mean }
}
```

`lag_days` is the headline number — the access-to-care lag §1 calls out.
`on_time_delta_days` is reported per-visit but not summarized; it answers
a different question (on-time-ness, not lag). The distribution is the
deliverable; the mean alone hides outliers (one 3-month lag inside
otherwise normal access).

## 7. Reliability assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Wrong merge pairs (same provider, multiple visits) | Medium | Tighter window; fall back to sequence pairing |
| Schedule notice extracted from a Resolution Strategy restatement (`* 7/3/25 Dr. Caldwell apt - scheduled for injection` — claim 2 L167) | Medium | Resolver: scheduling events whose `scheduled_for_date` is earlier than the note date are discarded as restatement (the booking already happened) |
| Visit attended on a date the schedule didn't anticipate (walk-in) | Low | One-sided `attended` event; correctly excluded |
| Provider name mismatch breaks the merge | Medium | Same canonicalization as Q2 |
| `lag_days` negative (notice written after visit) | Very low | Data-quality flag — should not happen if `scheduled_notice_date` comes from a note that announces a *future* visit. Surface, don't clamp. |
| `on_time_delta_days` negative (visit attended before booked-for date) | Low | Report as-is; legitimate when rescheduled earlier |

**Corpus-scale failure mode:** the window parameter. N=14 works in the
samples; at scale, some practices schedule 6+ weeks out. Make N a
configurable parameter on the Resolver; consider a per-specialty default
(PT = 7 days, neurosurgery = 30 days) once enough data exists to tune it.

## 8. Design implications

**Q4 alone justifies the Resolver as a first-class pipeline stage.** Q1 and
Q3 don't need cross-note merge. Q2 needs status promotion but could
plausibly be handled with `SELECT DISTINCT` at query time. Q4 cannot — the
gap computation **requires** the two halves to be on one row before SQL ever
runs, because the date math has to happen *after* the merge decision.

It also drives:

- The triple `scheduled_notice_date` / `scheduled_for_date` / `occurred_on`
  fields on `appointment` attributes — the notice date measures lag, the
  booked-for date is the matching key, the occurred date is the visit. No
  one of them alone is sufficient.
- The Resolver's matching-key interface being **swappable** (proximity vs.
  sequence vs. specialty-augmented) — Q4's accuracy will keep evolving as
  the corpus grows.
- A general principle: **events have multiple possible dates** (scheduled,
  occurred, recorded). Conflating them is the easiest way to produce
  plausible-looking but wrong answers. The schema makes them explicit.

## 9. Injury vs. illness applicability

The cross-note merge mechanics transfer to occupational illness claims
unchanged — but the **interpretation** of the lag carries different
operational meaning, and the matching parameters need rethinking.

- **What the lag measures changes.** In injury claims, schedule-to-seen
  lag is largely a **treatment-delay signal** — every day the claimant
  waits is a day of impairment continuing. In illness claims, the same lag
  often delays **causation determination** — the claim can't move toward
  acceptance or denial until specialists (pulmonologist, audiologist,
  occupational med) have weighed in. Same number, very different ops
  meaning. Worth surfacing as a corpus query split by `claim_type`.
- **Window parameters need per-specialty defaults.** The MVP uses a flat
  N=14-day window. This is roughly right for ortho follow-ups (the
  injury-claim majority) and wildly wrong for specialties common in
  illness claims — occupational pulmonology often books 6–8 weeks out;
  causation IMEs can be 3+ months. The `Resolver` interface already
  supports per-specialty windows (§5 of this doc); illness corpora are
  the use case that forces tuning them.
- **Rescheduling is more common.** Causation IMEs frequently get bumped
  while medical records are gathered. The "use the latest `scheduled_for`
  before the visit" rule still applies, but a higher reschedule rate
  means more candidate matches per visit — strategy 3 (sequence pairing)
  becomes the better default for illness-heavy corpora.
- **One-sided events are more legitimate.** An illness claim may show
  many scheduled-then-cancelled events as the workup design evolves.
  These should *not* be coerced into matches; they're correctly excluded
  from Q4. The conservative default (no match → stay one-sided) protects
  this.
