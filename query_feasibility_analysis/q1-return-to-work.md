# Q1 — How long did it take for the employee to return to work?

## 1. What the question is really asking

On the surface: *days between injury and return to work.* The trap is that
"return to work" can mean an **offer**, an **actual return**, or a
**full-duty release** — and the notes record all three in similar prose.
Assuming the query means the **actual return**; everything else (offers, plans,
medical releases without confirmation) is noise the extractor must reject.

## 2. Facts required

- `Claim.date_of_loss` — the anchor (already on the claim row).
- A `return_to_work` event — `event_date`, `duty_type` (modified / full),
  optional `role`.
- An `rtw_terminal` event for claims that will **never** have an RTW —
  `reason` ∈ `ptd | deceased | separated | closed_no_rtw`. Makes "never
  returned" a recorded fact, not the absence of one (DD-011).

Offers and other non-RTW states are rejected inside the LLM prompt and
produce no event (DD-012).

## 3. Where the facts live in the sample notes

Verified against `sample_claim_notes1.md` and `_2.md`.

**Claim 1 — clean positive case.** DOL `12/21/2024` (L593).
- L9, L31: `"EE returned to modified duty on 11/10/25"` — explicit, dated,
  and **restated across multiple Resolution Strategy snapshots** (same
  pattern as Q2's RS restatement form). Resolver merges by
  `(claim_id, event_date, duty_type)`.
- L67 (dated `10/29/25`): `"EE start date confirmed for 11/10/25"` —
  **forward-looking confirmation** dated *before* the return. The LLM
  prompt must reject this; otherwise the same RTW gets extracted twice
  (once prematurely, once correctly).
- L48 (dated `11/21/25`): `"She has been in her modified duty role as a
  scheduling coordinator for approximately 10 days"` — a **relative-date**
  RTW reference. The exact date is derivable by arithmetic
  (11/21 − ~10 days ≈ 11/10) but the LLM is unreliable at this. Better to
  let the explicit dated restatement (L9/L31) carry the event and let the
  Resolver dedupe.

**Claim 2 — RTW never occurred during file window.** DOL `12/30/2024`
(L529, L864).
- L28: `"the employer has extended a light duty offer effective 8/25"` —
  an **offer**, no confirmed acceptance. Rejected by the prompt.
- L64: `"HR Director has requested clmt RTW on Monday, 8/25"` — a
  **request** paired with the offer. Also rejected.
- L368: `"IE released to light duty..."` — a **release** without
  confirmation of return. Rejected.
- L877: `"She has not returned to work since the incident."` — **explicit
  negative RTW evidence** in an *open* claim. Not an `rtw_terminal` (the
  claim isn't closed / PTD / deceased / separated — she's still in active
  treatment). The LLM emits-only-positives contract correctly ignores
  this; Q1 returns `pending` and the negative language stays out of the
  event store.
- L33/L175: `"Claim will ultimately be settled when she is placed at
  MMI"` — settlement plan, not an RTW signal.

Across a corpus the phrasings are unbounded — which is why a regex-only
approach fails Q1. The two-claim sample already exercises explicit,
restated, forward-looking, relative-date, offer, request, release, and
explicit-negative phrasings — most of the failure modes a real corpus
would amplify.

## 4. Extraction approach — LLM-assisted with strict contract

Per DD-005, the LLM handles the *"did this happen, or is it being
discussed?"* judgment that a rule can't generalize over phrasings it
hasn't seen.

```ts
// LLM extractor for return_to_work
{
  eventType: "return_to_work",
  prompt: "Extract a return-to-work event ONLY if the note explicitly
           states the claimant has actually started working (full or
           modified duty). Do NOT extract from discussion of future RTW,
           MMI plans, releases without confirmation, or unaccepted offers.",
  schema: {
    event_date: "ISO date the return occurred",
    duty_type:  "modified | full",
    role:       "string (optional)"
  }
}
```

The extractor emits **only positive** events. Anything that fails the
"actually occurred" test produces no event — the negative judgment is not
stored (DD-012). The samples confirm at least four rejection categories
the prompt must handle: offers (claim 2 L28), requests (L64), releases
without confirmation (L368), and forward-looking confirmations of a
future start date (claim 1 L67 — the trickiest, since it *is* dated and
*does* name a real return). Explicit negative language in still-open
claims (claim 2 L877: `"She has not returned to work since the
incident"`) is correctly ignored — Q1 returns `pending` from the absence
of a positive event, not from extracting the negative.

A rule-based prefilter skips notes lacking `return` / `RTW` /
`light duty` / `modified duty` / `released` / `start date` keywords,
cutting LLM calls by ~80% on the sample claims.

## 5. Edge cases & ambiguities

| Situation | Decision |
|---|---|
| Multiple RTW events (modified → full duty) | Q1 uses the **first** (any duty type) |
| Offer made, no confirmed acceptance | LLM emits no event; Q1 returns `pending` |
| Returned then went back out on the **same claim** (recurrence / planned surgery) | First RTW still answers Q1; later RTW events power future recurrence-duration queries |
| **New** discrete injury (different incident, different DOL) | New claim file with its own `claim_id` and own Q1 — out of this claim's scope |
| "Released to return" with no date | Not extractable — events need a date |
| Same RTW restated across many notes | Resolver merges by `(claim_id, event_date, duty_type)` |
| RTW stated as a range ("week of 11/10") | Use earliest plausible day; flag `attributes.date_precision` |
| RTW referenced by **relative phrasing** ("for approximately 10 days" as of note date 11/21 — claim 1 L48) | Do **not** infer the date by arithmetic; rely on the dated restatement (claim 1 L9/L31) that the Resolver will merge. If no dated form exists in the file, drop with low-confidence flag rather than guess. |
| Forward-looking confirmation of a **future** start date ("EE start date confirmed for 11/10/25" — claim 1 L67) | Prompt rejects — extract only when the return has actually occurred relative to the note date. Otherwise the same RTW gets double-extracted. |
| Explicit negative RTW language in an **open** claim ("She has not returned to work since the incident" — claim 2 L877) | Not an `rtw_terminal` (claim is not closed / PTD / deceased / separated). LLM emits no event; Q1 returns `pending`. Negative language is not stored as evidence. |
| Claimant will never return — PTD / deceased / resigned / settled with no RTW | Each case is an `rtw_terminal` event with the matching `reason`; Q1 returns `never_returned` |

## 6. Computation

Two lookups: the first `return_to_work` event, and if absent, the
`rtw_terminal` event that closes the question definitively.

```sql
-- Positive case
SELECT c.claim_id,
       julianday(MIN(e.event_date)) - julianday(c.date_of_loss) AS days,
       MIN(e.event_date)                          AS rtw_date,
       json_extract(e.attributes, '$.duty_type')  AS duty_type
FROM claim c JOIN event e ON e.claim_id = c.claim_id
WHERE e.event_type = 'return_to_work'
GROUP BY c.claim_id;

-- Terminal case
SELECT claim_id, event_date,
       json_extract(attributes, '$.reason') AS reason
FROM event WHERE event_type = 'rtw_terminal';
```

The canned function returns a **discriminated union**, not a nullable
number — so the negative cases stay distinguishable downstream:

```ts
returnToWorkDays(claimId):
  | { status: 'returned',       days, rtwDate, dutyType }
  | { status: 'never_returned', reason, terminalDate }
  | { status: 'pending',        daysOpen }
```

The union forces the consumer to decide how each population is treated.
A `WHERE NOT NULL` over a nullable number would silently drop
PTD/deceased/separated claims and understate recovery time at corpus
scale.

### 6.1 The `pending` case is intentionally undifferentiated

For MVP, `pending` returns only `{ status, daysOpen }` — **no sub-reason**.
A claim can be pending because of an outstanding offer, ongoing treatment,
disputed compensability, scheduled surgery, etc. The MVP does not infer
which; the honest answer is "no RTW event yet, claim open N days."

**Extension path** (out of MVP scope per DD-012, recorded for reversibility):

1. Add `work_status_change` to the event taxonomy (sketched in
   `data-modeling.md` §4.5).
2. Derive a `pending_reason` on Q1's return shape from the most recent
   such event.
3. Backfill via extractor re-run; schema unchanged (DD-007).

## 7. Reliability assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| LLM misclassifies offer as RTW | Low with strict prompt | "Explicitly occurred" instruction; eval gold set later |
| Wrong RTW picked when multiple exist | Medium | Canned function commits to *first*, documented |
| RTW buried in long paragraph missed | Medium | Per-note granularity + keyword prefilter |
| Date format variance | Low | Centralized Normalizer (DESIGN §4.2) |

**Corpus failure mode:** phrasings the LLM hasn't seen (*"reactivated
employment"*) get silently dropped — the claim appears as `pending` when
it should be `returned`. The eval harness (DD-009, deferred) is the only
honest fix.

## 8. Design footprint

Q1 alone is responsible for:

- **`return_to_work` as a distinct event type** — Q1 needs *the* RTW
  date, not a stream of status transitions.
- **`rtw_terminal` as a first-class definitive negative** (DD-011) —
  "this claim will never have an RTW" lives in the timeline, not in
  query logic. Generalizes to any query with a meaningful negative.
- **The LLM extractor existing at all** — Q3 doesn't need one; Q2/Q4
  could limp by with rules; Q1 cannot.
- **The "explicit vs. discussed" prompt-contract pattern** that
  generalizes to every other LLM extractor.
- **The discriminated-union return shape** — making the negative
  outcome a peer of the positive one instead of a `null`.

## 9. Injury vs. illness applicability

The analysis is framed off the two **injury** samples. For
**occupational illness** claims (DD-010), Q1 carries real differences:

- **The anchor shifts.** `date_of_loss` becomes a date of diagnosis or
  last exposure. Days-to-RTW measures recovery from *diagnosis*, not
  from a trauma moment — and the claimant may have been working with
  the condition long before. Corpus aggregations should filter by
  `claim_type`.
- **What "RTW" means changes.** Illness claims often involve a
  permanent *removal from exposure* ("transferred off the assembly
  line") — working again, but not "recovered." `claim_type` separates
  the populations at the claim level for MVP; a finer event-level
  field can be added later as a JSON addition (DD-007).
- **The prompt contract generalizes unchanged** — discussed vs.
  occurred is the same problem.
- **The anchor date is mutable mid-claim.** Date of last exposure can
  be revised as work history surfaces. Q1 should recompute on read,
  not cache.
