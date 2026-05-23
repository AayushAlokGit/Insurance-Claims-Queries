# Extractor — design and behavior

The pipeline stage between **Normalizer** (clean, ISO-dated notes) and
**Resolver** (cross-note merge). Its job: turn one note's prose into
zero or more typed `CandidateEvent`s. DESIGN.md §5 is the summary; this
file is the catalog, the LLM contract, and the rationale every Q1–Q4
doc points back to.

---

## 1. Why it exists as a separate stage

A single bag of "extraction logic" sprawled across the codebase would
mix three very different concerns: (a) cheap structural parsing of
templated text, (b) expensive LLM judgment over prose, (c) cross-note
merge. The Extractor owns (a) and (b); the Resolver owns (c). DD-006
nails this down: extractors run **per note**, never per claim — that
keeps prompts small, cacheable, parallelizable, and unit-testable, and
concentrates the messy cross-note work in one downstream module.

The stage answers exactly one question per note per registered
extractor: *does this note contain an event of type X, and if so, what
are its fields?* It does **not** decide whether two events are the
same, whether one supersedes another, or how to derive cross-event
fields. Those live in the Resolver.

---

## 2. Inputs and outputs

```
Note  ──►  [ Extractor₁, Extractor₂, ... ]  ──►  CandidateEvent[]
        (each extractor independently; outputs concatenated)
```

```ts
Note {
  note_id, claim_id, note_date,         // ISO; normalized upstream
  activity, author, body                // body still contains prose
}

CandidateEvent {
  claim_id, event_type, event_date,
  attributes,                            // type-specific JSON payload
  extraction_method: "rule" | "llm",
  source_note_id, source_note_date
}
```

**Key invariant:** an extractor cannot see other notes. Anything
requiring cross-note context (status promotion, delta derivation,
schedule-to-visit matching) is deferred to the Resolver.

---

## 3. The hybrid principle — rules vs LLM

The single most important design choice in this stage. Rules and the
LLM aren't competing approaches; they're complementary tools applied
to different surface forms within the same note.

| Use **rules** when | Use **LLM** when |
|---|---|
| Text follows a strict template | Text is free prose with no structural anchor |
| The fact is exact (money, dates, IDs) | The fact requires reading comprehension (did it happen vs. is it being discussed) |
| 100% reproducibility is non-negotiable | Some interpretation tolerance is acceptable |
| Failure mode = "no match" (silent, recoverable) | Failure mode = wrong answer (must be guarded against) |

**Money never goes through the LLM.** Q3's regex is exact; an LLM
rounding `$321,014.00` to `$321,000` is a catastrophic and silent
failure. Rules are the right tool when correctness is exact and the
template is stable.

**Judgment never goes through a regex.** Q1's "did the employee
actually return to work or are we discussing it" is unreachable by
pattern matching. The LLM is the right tool when correctness depends
on what the prose *means*.

Some event types use **both**: Q2's `appointment` extraction runs a
rule extractor over templated `Date of Appointment:` headers *and* an
LLM extractor over narrative status statements. Both emit candidates;
the Resolver reconciles them.

---

## 4. The Extractor interface

```ts
interface Extractor {
  readonly eventType: string;                       // for routing & telemetry
  canHandle(note: Note): boolean;                   // cheap pre-filter, no LLM cost
  extract(note: Note, ctx: ClaimContext): Promise<CandidateEvent[]>;
}
```

**`canHandle` is the cost gate.** Before paying LLM tokens (or even
allocating a prompt), each extractor inspects the note for a cheap
keyword/regex signature. A note with no `return(ed)? to work|RTW|
modified duty|start date` substring never reaches the Q1 LLM. Tight
prefilters cut corpus-scale LLM spend by an order of magnitude without
sacrificing recall — the failure mode is a missed extraction, which
the prefilter design makes inspectable.

**`extract` returns a list, possibly empty.** Empty is a first-class
result, not an error. Multi-event notes (e.g., a record describing
both today's visit and the next scheduled one) return more than one
candidate. Extractors never raise on "found nothing."

**The pipeline runs all registered extractors over each note.** A
single note can match multiple `canHandle`s and produce candidates
of multiple types. Cross-extractor duplication is expected — the
Resolver merges.

---

## 5. The active extractor catalog

Five extractors in v1, covering the four active event types. Each
entry: what it triggers on, what it emits, why it exists.

### 5.1 `ReserveChangeExtractor` (rule)

- **Triggers on:** `note.activity === "Reserving"`.
- **Mechanism:** regex
  `/(?P<coverage>Indemnity|Expense-Litigation)\s+for\s+\((?P<n>\d+)\)\s+(?P<bucket>.+?)\s+Changed to \$(?P<amount>[\d,]+\.\d{2})/`
  (non-greedy bucket capture; `Expense-Litigation` explicit so it's
  not dropped by the `\s+for` requirement — see q3 §regex-fix).
- **Emits:** `reserve_change` with `canonical_bucket`, `new_amount`.
  `previous_amount` and `delta` are **null** at this stage —
  computed by the Resolver in Pass 4.
- **Why rules:** financial data, templated text, must be exact. DD-005.

### 5.2 `AppointmentMarkerExtractor` (rule, fast-path)

- **Role:** fast-path optimization for templated headers. Catches the
  common case without paying LLM tokens. The general path is the
  LLM-based `AppointmentExtractor` (§5.3) — anything the Marker
  misses, the LLM is responsible for catching.
- **Triggers on:** body contains `/Date of Appointment:|Next Office
  Visit:|NOV:|Schedule Date Time:|NEXT APPOINTMENT DATE\(S\):/i`.
- **Mechanism:** parser walks each marker line, captures provider
  (from nearby `Provider:` or inline phrase), date (already
  normalized).
- **Emits:** one or more `appointment` candidates. Crucially, a single
  note can emit **two**: a `Date of Appointment:` block (today's
  visit, `occurred_on` set) *and* a `Next Office Visit:` line
  (future visit, `scheduled_for_date` set, `scheduled_notice_date`
  = this note's date). Q4 §3 documents this multi-event pattern.
- **Status field is set to `"scheduled"` only for forward-dated
  scheduling lines.** For visit records (`Date of Appointment:`
  blocks), the status field is left null — the LLM extractor sets
  it from the surrounding prose.
- **Why rules:** the markers are templated; the structure is
  reliable; LLM tokens are unnecessary for the structural part.
  The Marker extractor is kept as a cost optimization, **not** as
  the only appointment path — novel headers / non-templated
  scheduling formats fall through to the LLM extractor.

### 5.3 `AppointmentExtractor` (LLM, general path)

The general path for appointment extraction. Fires on any note that
plausibly describes an appointment — whether or not a templated marker
is present. Closes the recall gap left by the Marker extractor's
limited keyword list.

- **Triggers on (the prefilter):**
  ```
  (status verbs)   /attended|missed|no-show|did not show|DNA|
                    cancell?ed|seen by|saw .* on|EE attended/i
    OR
  (date + appointment context)
                   hasDatePattern(body)
                     && /appointment|visit|follow.?up|scheduled|
                         seen|consult|exam|evaluation|referral/i
  ```
  The disjunction is the key: status verbs catch the
  attended/missed/cancelled cases; the date-plus-context branch
  catches scheduling-only events that use a non-templated header
  (`Visit Date:`, `Follow-up Visit Scheduled:`, etc.).
  **No provider-mention check.** Detecting "is a provider named in
  this note" at runtime requires fragile heuristics (regex on
  `Dr.` / `MD` / specialty keywords) that add code without buying
  meaningful precision — the (date + appointment-context) signal is
  already specific enough, and the LLM's prompt rejection rules
  are the real false-positive guard, not the prefilter.
- **Mechanism:** Q2's prompt contract (§6 below), widened to extract
  scheduled appointments alongside attended/missed/cancelled. The
  LLM extracts every appointment **explicitly described** in the
  note, with status ∈ `{attended, missed, cancelled, scheduled}`.
  Never infers status from silence.
- **Rejection rules baked into the prompt** (anti-false-positive
  for the wide prefilter):
  - Phone calls / outreach attempts that mention a provider and a
    date → not an appointment
  - Paperwork / mail events ("records request sent to Dr. X on 5/15")
    → not an appointment
  - General communications that happen to name a provider and a date
    → not an appointment
  - Past visits with no outcome mentioned → not extracted (avoids
    silent attended-promotion)
- **Emits:** `appointment` candidates with `status` and
  `evidence_quote` from the note.
- **Why LLM:** narrative status statements and novel scheduling
  formats are unreachable by regex. "EE was unable to attend her
  appointments scheduled for 08/11 and 08/13" requires reading
  comprehension to pair the negative with the dates;
  "Follow-up Visit Scheduled: 06/15/25 with Dr. Marek" uses a
  novel header the rule extractor doesn't know.

**Why "general path" not "prose path".** Earlier versions of this
design called this the `AppointmentProseExtractor` and prefiltered
on status verbs only. That left a real recall gap: scheduling-only
events with novel markers (no status verb, no known header) fell
through both extractors silently. Widening the prefilter and the
prompt contract closes that gap; the Marker extractor is then
correctly understood as a fast-path, not the primary path.

**Cost note.** The widened prefilter routes more notes to the LLM
than a status-verb-only prefilter would — roughly 1.5–2× depending on
the corpus, since the date+context branch picks up scheduling notes
and any narrative that mentions a visit alongside a date. The
mitigations: (a) prompt caching keeps per-call cost dominated by
note-body input tokens, not system prompt; (b) empty-return is cheap
— for a non-appointment note that matched date+context (paperwork,
authorizations, phone calls), the LLM correctly returns
`{appointments: []}` with minimal output tokens; (c) the Marker
extractor still short-circuits the templated common case.
False-positive control depends on the rejection rules in the prompt
holding up at corpus scale — DD-009's eval harness is the lever for
tuning that.

### 5.4 `ReturnToWorkExtractor` (LLM)

- **Triggers on:** body contains `/return(ed)? to work|RTW|light duty
  |modified duty|full duty|released to|start date/i`.
- **Mechanism:** Q1's strict-evidence prompt. Returns
  `{rtw: null | {return_date, duty_type, evidence_quote,
  evidence_category}}` where `evidence_category ∈ {explicit_dated,
  forward_confirmation, relative_date}`.
- **Critical rejection rules baked into the prompt** (Q1 §3 catalog):
  - Offers without acceptance → no event
  - Discussions / settlement plans → no event
  - Releases without confirmed return → no event
  - Future-dated start dates after note date → no event (defer
    until a later note confirms)
- **Why LLM:** the gap between "she returned to work on 11/10" and
  "we will explore RTW options" cannot be regex'd. Evidence-only
  extraction is the difference between a Q1 answer and a Q1 lie.

### 5.5 `RtwTerminalExtractor` (LLM)

- **Triggers on:** body contains `/never returned|permanent total|
  PTD|deceased|passed away|terminated|separated|claim closed/i`.
- **Mechanism:** prompt asks for `{rtw_terminal: null | {reason}}`
  where `reason ∈ {ptd, deceased, separated, closed_no_rtw}`.
  Requires positive evidence of a terminal state (DD-011).
- **Emits:** at most one `rtw_terminal` per note.
- **Why LLM and why a separate extractor:** terminal states are
  expressed in many surface forms ("she has not returned to work
  since the incident", "claimant deceased 09/2024", "PTD per Dr.
  Marek"). A single extractor with one schema is more reliable
  than coercing this into the RTW extractor's `rtw: null` branch
  — they're semantically distinct events, and DD-011 treats
  "never returned" as a first-class positive event, not the
  absence of an RTW.

---

## 6. The LLM contract — four principles that keep it honest

Every LLM extractor follows the same contract shape, regardless of
event type:

**1. Schema-constrained output, not free text.** Anthropic SDK tool-use
/ JSON mode. The LLM **cannot** return prose; it can only fill a
typed structure. If the schema says `status: "attended" | "missed" |
"cancelled"`, the model cannot invent `"probably attended"`.

**2. Discriminated union for the empty case.** `{rtw: null | {...}}`,
not `{return_date: string | null}`. "No event found" is a first-class
value the prompt asks for explicitly. This kills the
`null`-as-ambiguity failure mode that bites pure-LLM pipelines.

**3. Required `evidence_quote` (the anti-hallucination lever).** The
LLM must copy the verbatim phrase from the note that justifies the
extraction. A post-LLM safety check verifies that
`evidence_quote ⊆ note.body` (substring match, normalized for
whitespace). If the quote is not in the body, the candidate is
**rejected** — never reaches the Resolver. This catches the failure
mode where the model invents events that sound plausible but aren't
in the source.

**4. Explicit negative rules in the prompt.** The prompt enumerates
what NOT to extract, with examples. Q1's prompt lists offers,
discussions, releases, future-dated starts as rejection categories.
This is the single highest-leverage prompt design move — the model
is far more reliable at "extract only X, reject everything else
including Y, Z, W" than at "extract X" alone.

A reference prompt skeleton (Q1, paraphrased):

```
You are extracting return-to-work events from one workers' comp
claim note. Extract only when the note explicitly states the return
occurred (past tense, confirmed start date on or before the note
date, or other positive evidence).

DO NOT extract for:
- Offers without stated acceptance
- Discussions or settlement plans
- Medical releases without confirmed return
- Start dates after this note's date

NOTE DATE: {{note.note_date}}
NOTE BODY: {{note.body}}

Return JSON matching:
{ "rtw": null | {
    "return_date": "YYYY-MM-DD",
    "duty_type": "modified" | "full",
    "evidence_quote": string,
    "evidence_category": "explicit_dated" | "forward_confirmation" | "relative_date"
}}
```

---

## 7. Multi-extractor coordination on a single note

A note like:

> Date of Appointment: 4-11-25. Provider: Dr. Harmon. Outcome: EE
> attended; reports continued back pain. Next Office Visit: 5-16-25.

triggers **three** extractors:

| Extractor | Emits |
|---|---|
| `AppointmentMarkerExtractor` | Two `appointment` candidates (the 4/11 visit; the 5/16 scheduling) |
| `AppointmentExtractor` (LLM) | One `appointment` candidate with `status="attended"` for 4/11 (status verbs in body trigger the prefilter) |
| `ReturnToWorkExtractor` | `canHandle` fails (no RTW keywords); no LLM call |

The Marker output for 4/11 and the Prose output for 4/11 are both sent
to the Resolver, which merges them by `(claim_id, canonical_provider,
4/11 ± 7d)` into one event with both `occurred_on` and `status` set.

**The Extractor stage never tries to be smart about this overlap.**
Duplication is expected and cheap. Pushing dedup into the Extractor
would either require a stateful pass (breaking the per-note
contract) or an LLM round-trip to ask "is this the same appointment
as that one" (slow, expensive, fragile).

---

## 8. Failure modes & safety nets

| Failure | Detection | Behavior |
|---|---|---|
| LLM returns schema-invalid JSON | Tool-use / Zod parse fails | Reject candidate; log; do not retry blindly (a flaky note is a real signal) |
| LLM hallucinates an event not in the note | `evidence_quote ⊄ note.body` substring check | Reject candidate before Resolver; emit data-quality counter |
| LLM extracts a discussion as a real event (Q1 false positive) | Caught at eval time, not at runtime | DD-009 eval harness; iterate the prompt's rejection list |
| Rule regex over-matches | Unit test against the gold sample notes | Tightening the regex is cheap; rule extractors have full test coverage |
| `canHandle` false negative — note matched no extractor that should have | Eval recall metric per event type | Loosen the prefilter; recall over precision at the prefilter stage |
| Note date inconsistent with note body content | Normalizer flagged it upstream | Extractor consumes the flag; passes it through `attributes.data_quality_flags` |

The two highest-leverage safety nets: **(a) the `evidence_quote`
substring check** (catches hallucinations deterministically without
the LLM's cooperation), and **(b) tight schema constraints** (the LLM
cannot produce out-of-domain values for enum fields).

---

## 9. What the Extractor is NOT

- **Not a deduplicator.** Two notes mentioning the same visit produce
  two candidates. The Resolver merges.
- **Not a deriver.** `delta`, `lag_days`, status precedence — all
  Resolver responsibilities. The Extractor stops at "what does this
  one note say".
- **Not a classifier of note type.** `Activity:` is already on the
  note; the Loader/Normalizer set it. Extractors use it for
  routing, they don't recompute it.
- **Not a date parser.** All dates are already ISO by this stage.
- **Not a place for cross-claim logic.** The Extractor sees one note
  in one claim. Corpus-level extraction (trends, comparisons) is a
  query-layer concern over already-extracted events.

---

## 10. Testability

Per-note isolation is the testability win. Each extractor is a pure
function of `(note, ctx)`:

- **Rule extractors**: full unit-test coverage against the actual
  sample-note text. Add a new templated form → add a fixture → red →
  green. No LLM involvement, no fixtures need regeneration.
- **LLM extractors**: harder to unit-test deterministically. The
  approach: (a) mock the LLM in unit tests, asserting prompt shape
  and that the post-LLM safety nets fire correctly on stubbed
  responses; (b) eval at integration level against a gold-labeled
  set of notes (DD-009 harness), with per-extractor precision/recall.
- **Cross-extractor**: an integration test takes a representative
  note, runs all registered extractors, and asserts the candidate
  set. Catches regressions in `canHandle` overlap and ordering.

---

## 11. Adding a new extractor

The interface is the extension contract (DESIGN.md §9). To support a
new query:

1. Define the event type and its `attributes` payload in
   `data-modeling.md §4.5`.
2. Decide rule, LLM, or hybrid based on §3 above.
3. Implement `Extractor` — `eventType`, `canHandle`, `extract`.
4. If LLM: write the prompt with the four-principle contract (§6),
   add the `evidence_quote` check.
5. Register it in the extractor list. The pipeline picks it up
   automatically; no other stage changes.
6. Add unit tests against sample notes.
7. (Optional, post-MVP) Add labeled examples to the eval gold set.

The Resolver may need a matching strategy registered for the new
event type — see resolver.md §4.
