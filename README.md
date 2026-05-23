# Claims — workers' comp note parsing

Ingests unstructured workers'-compensation claim notes (markdown
files), parses them into structured `Claim` + `Event` rows in
SQLite, and answers four canned queries over the result.

The system is built around the principle that **per-note
extraction + cross-note resolution** is more reliable than
asking an LLM to summarize a whole claim. Each note is processed
in isolation; a separate Resolver stage handles dedup, status
promotion, and cross-event derivations.

---

## Quick start

Requirements: Python 3.12, [`uv`](https://github.com/astral-sh/uv).

```powershell
# 1. Install uv (one-time)
py -3.12 -m pip install --user uv

# 2. Sync the project (creates .venv, installs deps)
py -3.12 -m uv sync

# 3. Configure secrets
Copy-Item .env.example .env
#   Edit .env: set GOOGLE_API_KEY (free tier from AI Studio works)
#   or LLM_PROVIDER=openai + OPENAI_API_KEY (encrypted key at the
#   bottom of docs/Exercise.md, password "adaptional")

# 4. Ingest a sample claim
py -3.12 -m uv run python -m claims ingest `
    --file sample_claim_notes/sample_claim_notes1.md `
    --db demo.db

# 5. Answer a query
py -3.12 -m uv run python -m claims query q1 --claim-id 1-29RT --db demo.db
```

Sample output for step 5:

```json
{
  "status": "returned",
  "days": 324,
  "rtw_date": "2025-11-10",
  "duty_type": "modified"
}
```

That's the full happy path. Drop in a notes file, ingest, query.

---

## The four queries

The `claims query` subcommand runs one of four canned queries.
Each returns a typed object — never a bare scalar — so the
negative cases (no RTW, no attended visits, no merge candidates)
stay distinguishable.

| Query | Question | Return shape |
|---|---|---|
| `q1` | How long did it take to return to work? | Discriminated union: `returned` \| `never_returned` \| `pending` |
| `q2` | How many appointments were attended? | List of attended appointments + count |
| `q3` | What were the reserve changes? | Per-bucket list of swings with `previous → new → delta` |
| `q4` | How long does it take to see a provider once scheduled? | Per-visit lag list + median / p90 / mean distribution |

Verified outputs against both real samples are committed under
[`src/claims/query/query_outputs/`](src/claims/query/query_outputs/).
Headline numbers:

| | Claim 1 (`1-29RT`, NJ injury) | Claim 2 (`2-248KR`, SC injury) |
|---|---|---|
| Q1 | `returned`, 324 days, modified | `pending`, 509 days open |
| Q2 | 26 attended | 19 attended |
| Q3 | 9 changes / 3 buckets | 4 changes / 2 buckets |
| Q4 | 12 merged visits, median lag 5d | 7 merged visits, median lag 11d |

---

## How it works — pipeline at a glance

```
sample_claim_notes/X.md
        │
        ▼
   Loader              splits the file into RawNoteBlocks
        │              + extracts claim header (id, account)
        │
        ▼
  Normalizer           parses the header date, cleans mojibake,
        │              emits Note objects with data-quality flags
        │
        ▼              ── per-note metadata: claim_id, date_of_loss,
        │                  jurisdiction inferred via regex
        ▼
  Extractors (ThreadPool, default 4 workers)
   ├─ ReserveChangeExtractor       (rule, regex)
   ├─ AppointmentMarkerExtractor   (rule, regex)
   ├─ AppointmentExtractor         (LLM, structured outputs)
   ├─ ReturnToWorkExtractor        (LLM, strict evidence-only)
   └─ RtwTerminalExtractor         (LLM, first-class negative)
        │
        ▼
   Resolver            dedup + status promotion + delta derivation
        │              (provider canonicalization, proximity merge)
        │
        ▼
   SQLite (claims.db)  Claim + Event with JSON1 attributes
        │
        ▼
   Query layer         Q1 – Q4 canned functions
```

Every stage emits structured log lines into
`logs/<timestamp>-<claim_id>.log` for traceability. The terminal
gets INFO; the file gets DEBUG.

---

## Design

The design docs are the spec, not a retrospective. Read them in
this order (also tracked in `CLAUDE.md`):

1. [`docs/Exercise.md`](docs/Exercise.md) — the brief.
2. [`docs/DESIGN.md`](docs/DESIGN.md) — master design.
3. [`design-decisions.md`](design-decisions.md) — DD-001 … DD-015.
4. [`docs/data-modeling.md`](docs/data-modeling.md) — `Claim` + `Event` schema.
5. [`docs/normalizer.md`](docs/normalizer.md) — Normalizer deep dive.
6. [`docs/extractor.md`](docs/extractor.md) — Extractor deep dive (LLM contract + prompts).
7. [`docs/resolver.md`](docs/resolver.md) — Resolver algorithm.
8. [`query_feasibility_analysis/README.md`](query_feasibility_analysis/README.md) — per-query feasibility (Q3 → Q1 → Q2 → Q4).
9. [`docs/claims file analysis.md`](docs/claims%20file%20analysis.md) — sample-data analysis.
10. [`docs/implementation-plan.md`](docs/implementation-plan.md) — phased roadmap.

A few load-bearing decisions worth highlighting:

- **DD-005**: hybrid rule/LLM extraction. Financial data is rule-extracted (Q3 is pure regex); narrative status statements are LLM-extracted.
- **DD-006**: per-note extraction. Cross-note merge happens in the Resolver, never inside an LLM prompt.
- **DD-007**: JSON `attributes` column with a promotion path. Schema-additive migrations only.
- **DD-010**: loss-neutral vocabulary (`date_of_loss`, `claim_type ∈ {injury, illness}`).
- **DD-011**: first-class negatives (`rtw_terminal` event, not a NULL).
- **DD-013**: Normalizer preserves body text verbatim — the LLM's `evidence_quote` substring check depends on it.
- **DD-014**: bounded retry with ±50% jitter on LLM calls.
- **DD-015**: per-note extraction parallelized (default 4 workers; honors RPM ceiling on Gemini free tier).

---

## A worked example

Take the line from `sample_claim_notes1.md`:

```
Date: 11/12/2025 02:14pm CT | Activity: Resolution Strategy | Noted By: M.H.
Note: ...
SINCE LAST ACTION PLAN: EE returned to modified duty on 11/10/25 as a
scheduling coordinator, working 4 hours/day. ...
Indemnity for (2) Lost Time Changed to $321,014.00 ...
```

This single note produces (after the full pipeline) at least
two events:

1. **`return_to_work`** with `event_date=2025-11-10`,
   `duty_type=modified`, `role="scheduling coordinator"` —
   extracted by the LLM RTW extractor on the explicit return
   statement. Q1 reads this row.
2. **`reserve_change`** with `bucket="Indemnity (2) Lost Time"`,
   `new_amount=321014.00`, `delta=…` (derived by the Resolver) —
   extracted by the pure-regex Reserve extractor. Q3 reads this row.

The note's own `2025-11-12` timestamp is **not** any event's
date. Events carry the date the *thing happened*, not when the
note was written. See `docs/data-modeling.md §3.4` for why this
matters.

---

## CLI reference

```
claims ingest --file PATH [--db PATH] [--no-llm] [--workers N]
              [--provider {google,openai}] [--verbose]
              [--date-of-loss YYYY-MM-DD]  # override regex inference
              [--jurisdiction CODE]         # override regex inference
              [--claim-type {injury,illness}]  # override default
```

```
claims query {q1,q2,q3,q4} --claim-id ID [--db PATH]
```

Defaults:
- `--db claims.db`
- `--workers 4` (parallel extraction across notes)
- `--provider` → `LLM_PROVIDER` env var → `google`

Metadata flags are overrides — by default the regex inference
in `src/claims/loader/metadata.py` pulls `date_of_loss` from
`Date of Injury: …` lines and `jurisdiction` from
`Jurisdiction: <CODE>` lines.

---

## Layout

```
sample_claim_notes/        # the two real samples
docs/                      # design docs (DESIGN, normalizer, extractor, resolver, ...)
design-decisions.md        # DD-001 ... DD-015
query_feasibility_analysis/  # per-query feasibility (Q1-Q4)

src/claims/
  loader/         # file → RawNoteBlock + metadata inference
  normalizer/     # RawNoteBlock → Note (header parsed, body cleaned)
  extractor/      # Note → Event (rules + LLM extractors)
  resolver/       # Events → resolved events (dedup, merge, derive)
  store/          # SQLite schema + repository
  query/          # Q1-Q4 canned query functions
  llm/            # provider-agnostic StructuredLLM (Google + OpenAI)
  log_config.py   # per-run logging setup
  __main__.py     # `claims` CLI

tests/                     # mirrors src/claims/ layout
scripts/                   # opt-in smoke tests (hit the live LLM)
logs/                      # per-run pipeline logs (gitignored)
```

---

## Development

```powershell
# Run the full test suite (150 tests, no network)
py -3.12 -m uv run pytest

# Smoke-test the appointment LLM extractor against a real sample
# (hits the live LLM — burns quota)
py -3.12 -m uv run python scripts/try_appointment_llm.py --claim 1 --limit 3

# Smoke-test the RTW + RTW-terminal extractors similarly
py -3.12 -m uv run python scripts/try_rtw_llm.py --claim 1 --limit 5

# Summarize all four queries against a populated DB in human format
py -3.12 -m uv run python scripts/summarize_queries.py --claim-id 1-29RT --db demo.db
```

---

## Provider configuration

The system is provider-agnostic at the `StructuredLLM` Protocol
level (`src/claims/llm/base.py`). Both Google Gemini and OpenAI
implementations ship; pick via `LLM_PROVIDER` in `.env`.

| Provider | SDK | Default model | Notes |
|---|---|---|---|
| `google` (default) | `google-genai` | `gemini-2.5-flash-lite` | Free tier covers full ingest of both samples. |
| `openai` | `openai` | `gpt-4o-2024-08-06` | Requires structured-outputs (json_schema strict) model. |

To swap: change `LLM_PROVIDER=openai` in `.env` and re-run any
`claims ingest`. Each extractor depends on the abstract Protocol;
no code changes.
