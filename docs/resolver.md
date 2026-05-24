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
- **Identity reconciliation** — `Dr. Caldwell`, `Dr. Caldwell, MD`, the
  clinic name, "the neurosurgeon" all surface differently across notes
  for the same encounter; the match key has to bring them together
  without inflating from variation alone (DD-016).

These cannot live in the Extractor (one-note view) or the query layer
(needs deterministic, reproducible merges already in SQL). The Resolver
is the only place.

| Query | What it needs from the Resolver |
|---|---|
| Q1 | Dedup RTW restatements; drop relative-date / forward-confirmation duplicates of the explicit dated RTW |
| Q2 | Party-set merge (DD-016), asymmetric status precedence (DD-017), cross-note dedup of 5+ appointment surface forms |
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

Input and output are both the `Event` pydantic model
(`src/claims/models/event.py`). Inputs carry
`extraction_method ∈ {"rule", "llm"}`; resolved events produced from
>1 input get `"merged"`, with attribute nulls filled in from across
the group and a fresh `event_id`.

---

## 3. The five-pass algorithm

Sequential — each pass depends on the previous one having completed.

**Pass 1 — Normalize attributes.** In-place, no grouping. Anything that
affects the match key happens here.

- *Parties normalization* (`appointment`) — see §5 (DD-016).
- *Bucket canonicalization* (`reserve_change`) — `Indemnity for (2) Lost
  Time` → `Indemnity (2) Lost Time` (Q3 §3).
- *Status normalization* (`appointment`) — `no-show` / `DNA` / `didn't show`
  → `missed`.
- *Date sanity* — flag (don't silently drop) year < 2000 or > today + 5y.

**Pass 2 — Compute the match key.** Per event type:

| Event type | Match key |
|---|---|
| `reserve_change` | `(claim_id, canonical_bucket, source_note_id)` — each Reserving note is its own event |
| `appointment` | DD-016: `(claim_id, encounter_date exact, parties_overlap ≥ 1)`; `encounter_date = scheduled_for_date ?? occurred_on`; see §6 for the parties-overlap rule (supersedes the earlier `canonical_provider, anchor_date ± window` design) |
| `return_to_work` | `(claim_id, event_date, duty_type)` |
| `rtw_terminal` | `(claim_id, reason)` — at most one per claim |

**Pass 3 — Group and merge.** Per type:

- **`reserve_change`** — usually group size 1; merge rule trivial. The RS
  narrative restatements (claim 2 L32, L174, L789) are structurally
  different and Q3's rule extractor doesn't emit them — if a future
  change does, Pass 4's `delta = 0` drop catches them.
- **`appointment`** — most complex. (1) Union the three date fields.
  (2) Resolve status by DD-017 **asymmetric** precedence:
  `missed`/`cancelled` beat `attended`/`scheduled`/`unknown`; within
  negatives `missed > cancelled`; within positives
  `attended > scheduled > unknown`; ties broken by the more recent
  `source_note_date`. Replaces the earlier monotonic-up rule
  (`attended > missed > cancelled > scheduled`), which silently
  upgraded `missed → attended` — see DD-017.
  (3) Reschedules: take the **latest** `scheduled_notice_date` before
  the visit (Q4 §5); earlier notices stay in history but don't feed
  Q4's gap math. (4) Union `parties` across the group, deduped by
  normalized form, preserving first-seen order; longest surface form
  per identity wins as the display label. (5) Preserve
  `appointment_type` once set.
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

Each event type has its own resolver function in
`src/claims/resolver/` — `resolve_appointments`,
`resolve_reserve_changes`, etc. — composed in `resolver.py::resolve`.
A future per-event strategy registry is a refactor when matching
accuracy demands it; for the current four types, function-per-type
is enough.

Live shapes: `reserve_change` → identity by
`(claim_id, canonical_bucket, source_note)`; `appointment` →
parties-set merge (DD-016: `(encounter_date exact, parties_overlap)`,
single strategy, no Q2/Q4 split); `return_to_work` →
identity-with-form-priority; `rtw_terminal` →
identity-with-precedence.

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

## 5. Appointment identity — DD-016 in detail

Q2 and Q4 both hinge on collapsing every mention of one encounter
into a single row. The corpus-scale failure mode is that the *same*
person/place surfaces in many strings — `Dr. Caldwell`, `Dr.
Caldwell, MD`, `Caldwell`, `the neurosurgeon`, `Spine & Neurology
Group`, `Spine and Neurology` — and any merge rule has to bring
them together without inflating from variation.

The live design (DD-016, replacing an earlier
canonicalized-provider-string + `±N day` window scheme):

- **Party identity**: the LLM emits a `parties: tuple[str, ...]`
  per appointment containing every named individual and
  organization involved. The resolver applies deterministic
  lossless normalization (lowercase, strip honorifics/degree
  suffixes, `&`→`and`, collapse whitespace) and compares with
  **exact set intersection**. ≥1 shared entity → same encounter.
  An empty list on either side does not block the merge (date
  alone carries it). See `src/claims/resolver/parties.py`.
- **Date identity**: exact `encounter_date` equality. No `±N day`
  window. `encounter_date = scheduled_for_date or occurred_on`.
  Date-format ambiguity is the Normalizer's job (DD-013); when
  parsed dates differ, the resolver trusts them.

The full rejected-alternatives discussion (regex canonicalization,
fuzzy string matching, single `attending_party`, proximity
windows) lives in DD-016 in `design-decisions.md`.

### Known residual failure mode — same facility, same day, two different clinicians

When one claimant sees two **different** clinicians at the **same
facility** on the **same day** (e.g. Dr. Caldwell **and** Dr. Farano
both at Spine & Neurology Group on 4/22), both events carry the
same `encounter_date` and the shared facility name in their
`parties` lists overlaps — so the merge rule (≥1 shared entity)
incorrectly collapses the two encounters into one.

```
Note A: parties = ["Caldwell", "Spine & Neurology Group"]  4/22
Note B: parties = ["Farano",   "Spine & Neurology Group"]  4/22
                                  ↑
                       shared party → MERGE (incorrect)
```

The merged event's `parties` list contains both clinicians, so
the collapse is **auditable, not silent** — but Q2 undercounts
attended visits by 1 for that day, and Q4 may pair the wrong
(`scheduled_for_date`, `occurred_on`) dates if both encounters
had scheduling events.

**Why accepted for the MVP.** Pattern does not occur in either
sample claim. Same-day multi-specialist visits at one practice
are rare outside hospital inpatient stays. The artifact is
bounded (must be same claim, same date, same practice) and
visible in the merged record.

**Local fix when it matters.** Tighten `parties_overlap` from
"any shared party" to "shared **person** party":

```
# pseudo-Python — not implemented; defer until a real corpus
# shows the pattern matters.
person_overlap = {p for p in a.parties if is_person(p)} \
                  & {p for p in b.parties if is_person(p)}
```

With facilities treated as confirming evidence rather than
identifying evidence, the Caldwell/Farano case stays as two
distinct encounters. Trade-off: the cross-reference case where
one note names only the org and another names only the doctor
(rare in the samples; see DD-016) would stop merging — would
need an explicit alias from the doctor to their primary practice
to recover.

Contrast worth keeping in mind: the rejected **fuzzy-string**
failure mode (`"Harmon"` collapsing with `"Harman"`) is *silent
and unbounded* — any two notes with similar provider strings
might merge with no signal. The accepted **same-facility-same-day**
failure mode is *bounded and inspectable*. We keep the second; we
reject the first.

---

## 6. Failure modes and data-quality flags

The Resolver picks the conservative outcome and emits a flag — never
silent coercion.

| Failure | Behavior |
|---|---|
| Two `rtw_terminal` candidates, different `reason` | Keep most definitive; flag `rtw_terminal_conflict` |
| Hanging `scheduled` event (no visit within 60 days) | Kept as `scheduled` — evidence-only promotion rule forbids silent demotion; excluded from Q4 by lack of an `occurred_on` |
| `delta = 0` reserve change | Dropped silently — documented restatement behavior, not an error |
| Same-minute reserve updates, different buckets (claim 1 L392+L396) | Both kept — different match keys, never group |
| **Same claim, same date, two different clinicians at same facility** | **MERGED into one event (DD-016 known failure mode). Both clinicians stay in merged `parties` list — collapse is auditable. Q2 undercount = 1 for that day. See §5 above.** |

Flags stored as `attributes.data_quality_flags[]`. A corpus-wide health
report aggregating flag counts is out of MVP scope but easy to add.

---

## 7. What the Resolver is NOT

- **Does not classify events.** Extractor's job. `appointment` stays
  `appointment` — never re-typed.
- **Does not call the LLM.** Pure code, deterministic, unit-testable.
- **Does not compute query answers.** Produces resolved events; SQL and
  canned functions compute answers. Same events, many queries.

---

## 8. Testability

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
