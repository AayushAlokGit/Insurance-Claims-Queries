# Normalizer — design and behavior

The pipeline stage between **Loader** (file → raw note blocks) and
**Extractor** (typed events per note). Its job: resolve the messy,
format-variant parts of the input into a single canonical form so
every downstream stage operates on clean, ISO-typed metadata and
character-clean text. DESIGN.md §4.2 is the summary; this file is the
algorithm, the contract, and the failure-mode catalog.

---

## 1. Why it exists as a separate stage

Dates and encoding are the slow, silent bugs in any text-processing
pipeline. A pipeline where every extractor parses dates independently
is a pipeline where every extractor has subtly different two-digit-year
rules, subtly different `5/4` disambiguation, subtly different mojibake
handling. Q1 says May 21 2025, Q4 says April 5 2025, and the bug is
invisible until a corpus-scale discrepancy surfaces and nobody knows
which extractor is right.

The Normalizer concentrates that work in one tested module so nine
extractors don't each re-implement (and re-break) it. The cognitive
load of format variance is paid once, at the start of the pipeline.

---

## 2. Inputs and outputs

```
RawNoteBlock  ──►  Normalizer  ──►  Note
(headers as raw       cleanup +     (typed metadata,
 strings, body as    date parsing    clean body text,
 raw bytes/text)                     data-quality flags)
```

`RawNoteBlock` (in `src/claims/loader/`) carries the raw header line,
raw body bytes/text, and a `source_offset` for traceability.
`Note` (in `src/claims/models/note.py`, pydantic) carries the typed
results: `note_id`, `claim_id`, `note_date: date` (ISO-parsed from
the header), `activity`, `author`, `body` (character-clean; body
dates left as-is), and `data_quality_flags: tuple[str, ...]`.

**Key invariant:** the Normalizer transforms the *shape* of the note
metadata (raw strings → typed fields) and the *encoding* of the body
text (broken → clean), but it does **not** transform the *meaning* of
the body content. The body still reads the way a human auditor would
read it.

---

## 3. The two concerns

The Normalizer's work splits cleanly into two concerns. Conflating
them is the source of most "should this stage do X" confusion.

### 3.1 Text cleanup (destructive, lossless)

Applied directly to the body. The corrected text means the same thing
as the broken text — these are character-level repairs.

| Symptom | Cause | Fix |
|---|---|---|
| `â€"` | UTF-8 em dash mis-decoded as Windows-1252 | `—` |
| `â€™` | UTF-8 right single quote mis-decoded | `'` |
| `â€œ` / `â€` | UTF-8 curly quotes mis-decoded | `"` / `"` |
| `Â ` | Stray non-breaking-space artifacts | regular space |
| `\r\n` line endings | Windows-style line breaks | `\n` |
| Tabs inside narrative text | OCR / paste artifacts | single space |
| Trailing whitespace on lines | Editor artifacts | stripped |

Without this, the rule extractors' regexes silently fail when the
marker is `Date of Appointmentâ€"` instead of `Date of Appointment—`,
and the LLM's `evidence_quote` substring check fails because the body
and the quote disagree on a character the model thinks is a dash.

### 3.2 Date interpretation (non-destructive, via a shared parser)

The subtler concern, and the one captured as **DD-013**. The Normalizer
parses the note's **header date** into a typed ISO field (`note_date`).
It does **not** rewrite the dates inside the note body. The body stays
as `Schedule Date Time: Apr 17 2025 9:00AM`, not rewritten to
`2025-04-17`.

**Why leave body dates alone:**

- **The `evidence_quote` substring check depends on body fidelity.**
  The LLM extractor copies a phrase verbatim into `evidence_quote`;
  the post-LLM safety net verifies the quote is a substring of the
  body (extractor.md §8). If the Normalizer rewrites the body, the
  LLM copies the rewritten form, and an auditor reading the original
  source can no longer trace the extraction.
- **Date detection is ambiguous.** `5/4` could be May 4 or April 5;
  `the 21st` is contextual; `4 weeks` is a duration, not a date.
  Rewriting in-place risks mangling text when the parser was wrong.
  Leaving the body alone means the worst case is "the extractor's
  call to the date parser returns null" — a recoverable miss, not a
  corrupted note.
- **The Normalizer doesn't have the context to disambiguate.** Some
  body dates need surrounding-sentence context (`see her again in 4
  weeks` is a relative date the LLM resolves against `note_date`).
  That belongs in the extractor, not the Normalizer.

The right mental model: **the Normalizer parses header dates into
structured fields, and exports a shared date-parsing utility that
extractors call on body strings.** Same code path, two call sites.
The "centralize date parsing" promise is fulfilled by shared code,
not by mass rewrite.

---

## 4. The shared date parser

One module with a comprehensive test suite. Every new date format
seen in the corpus extends the test suite, not the parsing logic in
multiple extractors. Surface forms it must handle:

| Surface form | Parses to | Notes |
|---|---|---|
| `4-17-25` | 2025-04-17 | |
| `4/17/2025` | 2025-04-17 | |
| `4.17.25` | 2025-04-17 | claim 2 L302 |
| `04-17-25` | 2025-04-17 | |
| `Apr 17 2025` | 2025-04-17 | |
| `April 17 2025` | 2025-04-17 | |
| `Apr 17 2025 9:00AM` | 2025-04-17 | time dropped — out of scope for current queries |
| `4/17` (no year) | needs `note_date` context | year inferred; flag emitted |
| `5.21.25` (two-digit year) | 2025-05-21 | pivot rule applied |

**Two policy decisions baked into the parser:**

1. **Two-digit year pivot.** Anything `00–69` → `2000–2069`;
   `70–99` → `1970–1999`. Configurable per deployment if the corpus
   spans older claims; the pivot lives in one constant in one file.
2. **Yearless dates.** `4/17` alone has no year. The parser takes
   `note_date` as context and assumes the body date is the same year
   unless the inferred date would land more than ~6 months in the
   future, which usually means "previous year." The output carries
   `data_quality_flags: ["year_inferred"]` so audits can find these.

The parser's interface is intentionally narrow:
`parse_date(input: str, *, reference_date: date | None = None) -> DateParseResult`
in `src/claims/normalizer/date_parser.py`, returning `iso: str | None`
plus a tuple of flags. `iso` is `None` on parse failure — never a
default value, never a guess silently encoded. Failure is a
first-class outcome that extractors can flag and pass downstream.

---

## 5. Worked example

**Input (raw, from Loader):**
```
Date: 4-14-25 | Activity: Investigation | Noted By: Jane Adjuster
Received notice that outpatient PT has been scheduledâ€"
Schedule Date Time: Apr 17 2025 9:00AM
Provider: ATI Physical Therapy
```

**Output (`Note` object):**
```python
Note(
    note_id="c1-n023",
    claim_id="1-29RT",
    note_date=date(2025, 4, 14),                 # header date → typed
    activity="Investigation",                     # header parsed
    author="Jane Adjuster",
    body=(
        "Received notice that outpatient PT has been scheduled—\n"
        "Schedule Date Time: Apr 17 2025 9:00AM\n"
        "Provider: ATI Physical Therapy"
    ),
    data_quality_flags=(),
)
```

What changed and what didn't:

- Header `Date: 4-14-25` parsed into `note_date: "2025-04-14"` — typed.
- `â€"` repaired to `—` in the body — destructive cleanup.
- `Apr 17 2025 9:00AM` left exactly as it was — body fidelity preserved.
  The Marker extractor will call `parse_date("Apr 17 2025 9:00AM")` when
  it processes that line and get `2025-04-17` back.

---

## 6. Failure modes — flag, don't paper over

The Normalizer is conservative. It does not silently throw away data,
and it does not silently invent data. Either it fixes losslessly, or
it parses and flags, or it surfaces a parse failure for an audit.

| Situation | Behavior |
|---|---|
| Header date parses but is implausible (year < 2000, > today + 5y) | Parse it; `data_quality_flags += "header_date_implausible"` |
| Header date can't parse at all | Note rejected; logged with `source_offset`; pipeline continues with the rest of the claim |
| Body contains mojibake the cleanup map doesn't cover | Leave it; `data_quality_flags += "unhandled_encoding"`; extractor will likely miss that line, which is the right outcome (better than silently mangling) |
| Date in body parses ambiguously (e.g., `5/4` with no MDY hint) | Parser returns `iso: null` with `flags: ["ambiguous_date_format"]`; the calling extractor decides whether to skip or flag |
| Encoding clearly broken at the file level (mixed UTF-8 / Windows-1252 in one note) | Best-effort cleanup; flag the note |
| Header missing `Activity:` field | `activity: null`; downstream extractors that rely on it (`ReserveChangeExtractor` checks `activity === "Reserving"`) won't fire — correct conservative behavior |

The flags propagate on the Note through to the resolved events
(`attributes.data_quality_flags[]`) — see resolver.md §6.

---

## 7. What the Normalizer is NOT

- **Not an extractor.** It doesn't pull events. It doesn't know what
  an appointment is. The `Date of Appointment:` line in a body is
  just text to the Normalizer.
- **Not a body-date rewriter.** Header dates become typed fields;
  body dates stay as text. The shared parser is the centralization
  mechanism, not in-place rewriting.
- **Not an LLM caller.** Pure code, deterministic, fast, fully
  unit-testable.
- **Not a financial-data parser.** Money amounts (`$321,014.00`) are
  left exactly as-is for Q3's regex. The Normalizer never touches
  numbers.
- **Not an identity canonicalizer.** That's the Resolver's job
  (resolver.md §5 — DD-016 parties-set merge), at a stage where
  cross-note context exists.
- **Not a derived-metadata enricher.** No `weeks_since_loss`, no
  inferred claim phase. Outputs are at the same semantic level as
  inputs — just cleaner.
- **Not stateful across notes.** Each note is normalized in isolation.
  Cross-note concerns live in the Resolver.

---

## 8. Testability

The Normalizer is the second-easiest stage to test exhaustively
(after the rule extractors), because it's pure, deterministic, and
operates on small inputs.

- **Date parser unit tests.** Every surface form in §4's table has at
  least one test. Boundary cases: pivot years (`69` → 2069, `70` →
  1970), Feb 29 in non-leap years, dates outside the plausibility
  window. The test fixture is the corpus's most valuable artifact —
  it grows whenever a new format shows up in production.
- **Mojibake repair tests.** Each entry in §3.1's table has a
  before/after fixture. New entries are added when a real claim
  exposes a new encoding pattern.
- **Note-level integration tests.** Real `RawNoteBlock`s from the
  samples are pinned to expected `Note` outputs. Catches regressions
  in header parsing, cleanup ordering, and flag emission.
- **Failure-mode tests.** Each row in §6 has a fixture asserting the
  exact flag(s) emitted and that no exception is raised.

The operational payoff: when an extractor downstream produces a wrong
date, the diagnosis is fast — either the date parser is wrong (failing
test in the Normalizer suite) or the extractor called it on the wrong
substring (failing test in the extractor suite). The bug never lives
in a third, ambiguous place.
