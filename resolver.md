# Resolver — design and behavior

The pipeline stage between **Extractor** (per-note candidates) and
**Store** (SQLite). Its job: turn a noisy, restated, multi-note candidate
stream into a deduplicated, merged event stream the query layer can run
cheap deterministic SQL against. DESIGN.md §4.4 is the summary; this file
is the algorithm and the rationale every Q1–Q4 doc points back to.

---

## 1. Why it exists

The Extractor sees one note at a time (DESIGN §5.3). That's right for
extraction but produces noise no single extractor can fix:

- **Restatement** — same RTW phrase appears in multiple Resolution
  Strategy snapshots (claim 1 L9, L31); same appointment is mentioned
  3–5 times. Counts inflate without dedup.
- **Cross-note completion** — schedule notice and visit record live in
  *different notes by definition* (Q4's whole problem).
- **Status promotion** — "scheduled" then "attended" must collapse to one
  status, and the winner is *strongest evidence*, not latest write.
- **Derived fields** — `reserve_change.delta` needs the previous amount
  for the same bucket; only computable once events are ordered.
- **Canonicalization** — `Dr. Caldwell` / `Dr. C` / `the neurosurgeon` are
  the same person; the match key has to canonicalize before grouping.

These cannot live in the Extractor (one-note view) or the query layer
(needs deterministic, reproducible merges already in SQL). The Resolver
is the only place.

| Query | What it needs from the Resolver |
|---|---|
| Q1 | Dedup RTW restatements; drop relative-date / forward-confirmation duplicates of the explicit dated RTW |
| Q2 | Provider canonicalization, status precedence, cross-note dedup of 5+ appointment surface forms |
| Q3 | Chain reserve changes by bucket to derive `delta`; drop `delta=0` restatements |
| Q4 | Merge schedule + visit events into one row with three date fields; latest-notice rule for reschedules |

---

## 2. Inputs and outputs

```
CandidateEvent[]   ──►   Resolver   ──►   ResolvedEvent[]
(per-note, typed,         5 passes        (deduplicated, merged,
 possibly redundant,                       derived fields computed,
 one-sided)                                provenance preserved)
```

```ts
CandidateEvent {
  claim_id, event_type, event_date, attributes,
  extraction_method: "rule" | "llm",
  source_note_id, source_note_date
}
```

`ResolvedEvent` has the same shape; `extraction_method = "merged"` when
produced from >1 candidate, attribute nulls filled in from across the
group, `source_note_ids[]` recorded for audit.

---

## 3. The five-pass algorithm

Sequential — each pass depends on the previous one having completed.

**Pass 1 — Normalize attributes.** In-place, no grouping. Anything that
affects the match key happens here.

- *Provider canonicalization* (`appointment`) — see §5.
- *Bucket canonicalization* (`reserve_change`) — `Indemnity for (2) Lost
  Time` → `Indemnity (2) Lost Time` (Q3 §3).
- *Status normalization* (`appointment`) — `no-show` / `DNA` / `didn't show`
  → `missed`.
- *Date sanity* — flag (don't silently drop) year < 2000 or > today + 5y.

**Pass 2 — Compute the match key.** Per event type:

| Event type | Match key |
|---|---|
| `reserve_change` | `(claim_id, canonical_bucket, source_note_id)` — each Reserving note is its own event |
| `appointment` | `(claim_id, canonical_provider, anchor_date ± window)`; `anchor_date = scheduled_for_date ?? occurred_on`; see §6 for window |
| `return_to_work` | `(claim_id, event_date, duty_type)` |
| `rtw_terminal` | `(claim_id, reason)` — at most one per claim |

**Pass 3 — Group and merge.** Per type:

- **`reserve_change`** — usually group size 1; merge rule trivial. The RS
  narrative restatements (claim 2 L32, L174, L789) are structurally
  different and Q3's rule extractor doesn't emit them — if a future
  change does, Pass 4's `delta = 0` drop catches them.
- **`appointment`** — most complex. (1) Union the three date fields.
  (2) Resolve status by precedence: `attended > missed > cancelled >
  scheduled` — *strength*, not chronology; a scheduling notice cannot
  demote an "attended" record. (3) Reschedules: take the **latest**
  `scheduled_notice_date` before the visit (Q4 §5); earlier notices
  stay in history but don't feed Q4's gap math. (4) Take the most
  specific provider/specialty form (`"Dr. Harmon, Spine"` over `"the
  spine doctor"`). (5) Preserve `appointment_type` once set.
- **`return_to_work`** — if the group has an explicit dated form (claim 1
  L9: `"EE returned to modified duty on 11/10/25"`) and a relative-date
  or forward-confirmation form, keep the dated one and drop the others.
  The Q1 prompt contract should reject the weaker forms upstream; this
  is a safety net.
- **`rtw_terminal`** — if multiple `reason`s appear, keep the most
  definitive (`deceased` > `ptd` > `separated` > `closed_no_rtw`) and
  flag as a data-quality conflict.

**Pass 4 — Derive cross-event fields.** Computable only post-merge:

- `reserve_change.delta` — order by `(event_date, source_note_minute)`
  within each `(claim_id, canonical_bucket)`; `delta = new - previous`;
  first-ever amount → `previous = 0`. **Drop** any event with `delta = 0`.
- `appointment.status` final value — already set in Pass 3; reaffirmed.

**Pass 5 — Emit.** Write to Store. `extraction_method` = `"rule"` /
`"llm"` / `"merged"`. `attributes.source_note_ids[]` carries provenance
for merged events — preserves the data for a future `--explain` flag
(out of MVP scope per DD-003).

---

## 4. The pluggable matching-strategy interface

The Resolver is built **per event type with a swappable matcher** — the
core routes by event type, each strategy owns its own matching logic.

```ts
interface MatchStrategy<E extends CandidateEvent> {
  readonly name: string;                  // e.g. "appointment.proximity-N=14"
  matchKey(event: E): string;             // cheap bucketing
  shouldMerge(a: E, b: E): boolean;       // finer pairwise check
}

interface Resolver {
  register<E>(eventType: string, strategy: MatchStrategy<E>): void;
  resolve(candidates: CandidateEvent[]): ResolvedEvent[];
}
```

MVP registers: `reserve_change` → identity; `appointment` → proximity
(two strategies registered, N=7 for Q2 dedup, N=14 for Q4 merge);
`return_to_work` → identity-with-form-priority; `rtw_terminal` →
identity-with-precedence. Per-specialty windows (Q4 §9) drop in as one
new strategy class.

**Why this pattern:**

- **Different event types genuinely need different matching.** A
  hard-coded matcher becomes a giant `switch(event_type)` inside the
  Resolver core. The strategy interface moves that switch to where
  behavior is *added* (a new class), not where it's *executed* (every
  resolve call).
- **Matching accuracy will keep evolving; extraction won't.** Q4 §4
  catalogues three strategies of increasing sophistication; Q4 §9 calls
  out per-specialty windows for illness corpora. Putting the volatile
  part behind an interface keeps the stable part stable.
- **One event type, multiple downstream needs.** Q2 wants tight dedup
  (N=7); Q4 wants drift tolerance (N=14). Pluggable strategies let
  both coexist without compromise.
- **Testability.** Each strategy is two methods, pure, independently
  unit-testable. The Resolver core's only test obligation is routing.
- **Reversibility.** Wrong N or wrong strategy = one `register()` call
  to fix. No migration, no schema change, no re-extraction.

---

## 5. Provider canonicalization

The corpus-scale failure mode for Q2 and Q4. Same person appears as
`Dr. Caldwell`, `Dr. Caldwell, MD`, `Caldwell`, `the neurosurgeon`,
`Dr C` — without canonicalization, merge keys fragment and counts
inflate.

MVP algorithm:

1. Strip degree suffixes (`, MD` / `, DO` / `, NP`) and honorifics
   (`Dr.` / `Dr`).
2. Extract last name (longest contiguous letter sequence; titlecase).
3. **Specialty fallback** — if no last name (`"the spine doctor"`),
   key on `(canonical_specialty, claim_id)`, resolving against earlier
   candidates in the same claim. Two same-specialty providers in one
   claim → don't merge, flag low-confidence.
4. Emit `canonical_provider`; keep raw form in `attributes.provider_raw`
   for audit.

Deliberately **not** entity-resolution-against-a-directory. A real
provider directory is the corpus-scale upgrade once failure modes are
observed in practice.

---

## 6. The fuzzy-date matching window — appointments only

Why `appointment` uses `anchor_date ± window` instead of exact equality:

- Visits sometimes occur a day or two off the booked-for date.
- Resolution Strategy restatements summarize at slightly drifted dates
  (e.g. RS `"Dr. Farano on 5/21"` vs. visit record `"Date of
  Appointment: 5.21.25"`).

| Use | Window | Rationale |
|---|---|---|
| Q2 cross-note dedup | **N=7 days** | Tight enough to avoid false merges of repeat visits; loose enough for drift |
| Q4 schedule-to-visit merge | **N=14 days** | The whole point of Q4 is realistic booked-to-occurred drift |
| Future per-specialty | configurable | PT/ortho tight (5–14d); causation IMEs and pulmonology 6–8 weeks (Q4 §9) |

Only `appointment` uses windowing. `reserve_change` is exact
date+bucket; `return_to_work` is exact `(date, duty_type)`; `rtw_terminal`
is claim-level unique.

---

## 7. Failure modes and data-quality flags

The Resolver picks the conservative outcome and emits a flag — never
silent coercion.

| Failure | Behavior |
|---|---|
| Two `rtw_terminal` candidates, different `reason` | Keep most definitive; flag `rtw_terminal_conflict` |
| Two providers, same canonical name, different specialties | Don't merge; flag low-confidence canonicalization |
| Hanging `scheduled` event (no visit within 60 days) | Promote to `unknown`/`missed`; excluded from Q4 |
| `delta = 0` reserve change | Dropped silently — documented restatement behavior, not an error |
| Same-minute reserve updates, different buckets (claim 1 L392+L396) | Both kept — different match keys, never group |
| Canonicalized provider name not seen elsewhere in the claim | Emit with low-confidence flag |

Flags stored as `attributes.data_quality_flags[]`. A corpus-wide health
report aggregating flag counts is out of MVP scope but easy to add.

---

## 8. What the Resolver is NOT

- **Does not classify events.** Extractor's job. `appointment` stays
  `appointment` — never re-typed.
- **Does not call the LLM.** Pure code, deterministic, unit-testable.
- **Does not compute query answers.** Produces resolved events; SQL and
  canned functions compute answers. Same events, many queries.

---

## 9. Testability

Pure module → exhaustive unit tests independent of the rest of the
pipeline:

- **Synthetic candidate streams** covering each merge rule, precedence
  case, window edge, and conflict.
- **Round-trip tests** — real extracted candidates from the two sample
  claims, asserted against a gold resolved-event set.
- **Property tests** — resolved cardinality ≤ candidate cardinality
  (never invents); every merged event has non-empty `source_note_ids`.

This is the operational payoff of DD-006 (per-note extraction): the
messy work concentrates in one pure module with a clear contract,
hardened independently of LLM behavior.
