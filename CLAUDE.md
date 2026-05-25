# CLAUDE.md — orientation for new sessions

This repository is the **Adaptional Take-Home Exercise**: design and (eventually) build a system that ingests unstructured workers'-compensation claim notes, parses them into structured events with an LLM, stores them in SQLite, and answers four canned queries over a corpus.

**Status:** implementation complete; design docs under `./docs/` are the spec, not a retrospective. Several long md files capture deeply iterated design decisions. Do not bulldoze past them. Read in the order below before suggesting changes or writing code.

---

## Reading order

1. **`./docs/Exercise.md`** — the original take-home brief. The four queries and the input shape come from here.
2. **`./docs/DESIGN.md`** — master design document. Sections cover the queries → facts mapping, the domain model, the pipeline architecture (Ingest → Extract → Normalize → Resolve → Store → Query), and scope.
3. **`./design-decisions.md`** — numbered, accepted design decisions (**DD-001 … DD-017**). Each entry has a rationale and what it commits us to. If a fresh idea conflicts with one of these, the DD wins until explicitly re-litigated.
4. **`./docs/data-modeling.md`** — Claim and Event schema deep dive. Active event types, future-but-not-built event types, the JSON-attributes tradeoff, and the promotion path from hot JSON field to typed column.
5. **`./docs/normalizer.md`** — Normalizer stage deep dive. Two concerns: destructive text cleanup (mojibake repair, line endings, whitespace) and non-destructive date interpretation via a shared date parser. Why body dates are not rewritten in-place (the `evidence_quote` substring check depends on body fidelity), the two-digit-year pivot rule, yearless-date inference, the date-format and mojibake catalogs, and the "flag, don't paper over" failure-mode policy.
6. **`./docs/extractor.md`** — Extractor stage deep dive. The hybrid rule-vs-LLM principle, the `Extractor` interface, the catalog of all five active extractors (Reserve, Appointment Marker as fast-path, Appointment LLM as general path, RTW, RTW Terminal) with triggers / output schemas / rationale, the four LLM contract principles (schema-constrained output, discriminated-union empty case, required `evidence_quote` with substring check, explicit negative prompt rules), multi-extractor coordination on a single note, and failure modes.
7. **`./docs/resolver.md`** — Resolver algorithm deep dive. Five-pass algorithm (normalize → match-key → merge → derive → emit), match-key construction per event type, merge rules, appointment identity (DD-016) with its known same-facility-same-day failure mode, the rest of the resolver's failure-mode catalog, and testability. Load-bearing for Q1's dedup, Q2's status precedence (DD-017 asymmetric), Q3's `delta` derivation, and Q4's cross-note merge.
8. **`./query_feasibility_analysis/README.md`** — index for the per-query feasibility docs. Reading order inside that folder is Q3 → Q1 → Q2 → Q4 (by increasing difficulty):
   - `q3-reserve-changes.md` — pure rule extraction, never LLM
   - `q1-return-to-work.md` — LLM extractor with strict prompt contract; discriminated-union return
   - `q2-appointments-attended.md` — hybrid extraction; the dominant driver of the Resolver stage
   - `q4-schedule-to-seen.md` — cross-note merge; alone justifies the Resolver as a first-class stage
9. **`./docs/claims file analysis.md`** — analysis of the two sample claim files (`./sample_claim_notes/`). What real input looks like, what surface forms each query's facts appear in.

---

## Key conventions in force

These are non-obvious from any one file; together they prevent re-litigating decisions that were already made.

- **Strict YAGNI on the domain model (DD-012).** The `Claim` table has 7 columns, not 12; the event taxonomy has 4 active types, not 5+. Speculative fields (`loss_description`, `body_parts`, `diagnoses`, `avg_weekly_wage`, `comp_rate`, `work_status_change`) were explicitly removed. Do not propose adding them back without naming the query that needs them.
- **Loss-neutral vocabulary (DD-010).** Workers' comp covers **injury and occupational illness**. Use `date_of_loss`, not `date_of_injury`. `claim_type ∈ {injury, illness}` is a first-class column on `Claim`. Every query doc has an "Injury vs. illness applicability" section — keep that habit when adding new analysis.
- **Event-centric model.** Everything dated and meaningful is an `Event` row with `event_type`, `event_date`, `attributes` (JSON), `extraction_method`. The four active types are `reserve_change`, `appointment`, `return_to_work`, `rtw_terminal`. Future types are sketched in `data-modeling.md §4.5` but not built.
- **First-class negatives (DD-011).** "Never returned to work" is an `rtw_terminal` event with `reason ∈ {ptd, deceased, separated, closed_no_rtw}`, not a `NULL`. Generalize: when a meaningful negative outcome exists, model it as an event, not as the absence of one.
- **Discriminated unions over nullable scalars.** Q1 returns `{status: 'returned' | 'never_returned' | 'pending', ...}`, not `days_to_rtw: number | null`. A `WHERE NOT NULL` at corpus scale silently drops populations and lies.
- **Per-note extraction (DD-006).** Extractors run per note, not per whole claim. Dedup / cross-note merge happens in the **Resolver** stage where it can be unit-tested. Do not push merge logic into an LLM prompt.
- **JSON `attributes` with a promotion path (DD-007).** Hot fields stay JSON until a query needs index-grade performance, then graduate to typed columns. Schema-additive migrations only.
- **Evidence-only state promotion.** A `scheduled` appointment does **not** auto-promote to `attended` from silence. Q1's RTW extractor extracts only when the note explicitly states the return occurred — offers and discussion produce no event. Same principle applies everywhere: derived facts require positive evidence; default to the weaker state.
- **Status precedence for appointments (DD-017, asymmetric):** `missed`/`cancelled` beat `attended`/`scheduled`/`unknown`; within negatives `missed > cancelled`; within positives `attended > scheduled > unknown`; ties broken by recency of `max(source_note_dates)`. Replaces the earlier monotonic `attended > missed > cancelled > scheduled` rule, which silently upgraded `missed → attended`. Resolver merges by strongest evidence, not by chronology.
- **Appointment merge key (DD-016):** `(claim_id, encounter_date exact, parties_overlap ≥ 1)`. The LLM emits a `parties: tuple[str, ...]` per appointment (named people + orgs); resolver compares using deterministic normalization (strip honorifics + degree suffixes, lowercase, `&`→`and`). No fuzzy matching, no `± window`. Replaces the earlier `canonical_provider + anchor_date ± window` design.
- **Financial data never goes through the LLM.** Q3 (reserve changes) is pure regex against templated text — see `q3-reserve-changes.md`. Money + LLM = silent rounding errors.
- **SQLite via stdlib `sqlite3` (Python).** Date math uses `julianday(...) - julianday(...)`. JSON access uses `json_extract(attributes, '$.field')`. Expression indexes go on the hot `json_extract` paths.
- **One pipeline, not branched by claim type.** Claim-type-specific behavior lives in prompts and resolver parameters, not in a forked pipeline. (User explicitly considered and rejected a per-claim-type pipeline fork.)

---

## Where artifacts land on disk

Use these when grepping for traces of a run or comparing outputs across runs. Paths are relative to repo root.

| Artifact | Location | Notes |
|---|---|---|
| Ingestion run logs | `./logs/claims_ingest/<ts>-<file>-<provider>-w<workers>.log` | File handler is always DEBUG; console is INFO (or DEBUG with `--verbose`). One file per ingest invocation. |
| Per-query run logs | `./logs/claims_query/<ts>-<claim_id>-q<N>.log` | One file per `(claim, query)` pair. |
| Ad-hoc script logs | `./logs/<script_name>/...` | E.g. `logs/try_appointment_llm/`. Convention: never flat under `logs/`. |
| Sample claim notes (input) | `./sample_claim_notes/sample_claim_notes{1,2}.md` | The two sample inputs from `Exercise.md`. |
| Ingested DB | `./sample_claim_notes/query_outputs/sample.db` | SQLite; rebuilt by the ingest script. |
| Per-claim query output (JSON) | `./sample_claim_notes/query_outputs/<claim_id>.json` | One file per claim, containing all four queries. Sections delimited by `=== q1 ===` etc. Regenerate via `python scripts/write_query_outputs.py --claim-id <id>` — the `query` CLI prints to stdout only, it does not write these files. |

**Greppable log lines for debugging the appointment pipeline (DD-019):**

Per-note candidate generation (extractor):
- `extract note=<id> note_date=<d> by=<Extractor> :: appointment date=<d> kind=<scheduled|occurred> status=<s> parties=[...] evidence="..."` — INFO, one line per per-note candidate the LLM emitted. Grep by note_id, party, date, or status to trace where a candidate came from.

Per-claim reconciliation (DD-019):
- `reconcile claim=<id> candidates=<N>` — INFO, before the LLM call.
- `recon claim=<id> :: appointment date=<d> kind=<occurred|scheduled> status=<s> parties=[...] notice=<d> contrib=[cid,...] evidence="..."` — INFO, one line per canonical appointment the reconciliation LLM produced. `contrib` lists the candidate IDs (their indices in the per-claim candidate list) that contributed to this canonical entry.
- `recon claim=<id> dropped <N> candidate(s) (no contribution): [...]` — INFO, the candidates the reconciliation LLM chose to discard. Useful when something expected is missing from the canonical list.
- `reconcile claim=<id> candidates=<N> -> canonical=<M>` — INFO, summary count.
- `reconcile claim=<id> failed: ... -- falling back to raw candidates` — WARNING, the LLM call errored; raw candidates pass through.

Other resolver stages (non-appointment):
- `resolve <event_type>: N in -> M out` — DEBUG, reserve/RTW dedup counts.
- `resolve: N appointment event(s) reached the resolver — should have gone through reconcile_appointments (DD-019)` — WARNING, defensive — appointments are not supposed to flow through the resolver under DD-019.

---

## The four canned queries

| ID | Question | Extraction | Doc |
|---|---|---|---|
| Q1 | How long did it take the employee to return to work? | LLM with strict "explicitly occurred" prompt | `./query_feasibility_analysis/q1-return-to-work.md` |
| Q2 | How many appointments were attended? | Hybrid: rules on templated headers, LLM on free prose | `./query_feasibility_analysis/q2-appointments-attended.md` |
| Q3 | What were the reserve changes? | Pure regex against `Indemnity for ... Bucket Changed to $X` | `./query_feasibility_analysis/q3-reserve-changes.md` |
| Q4 | How long does it take to see a provider once scheduled? | Two one-sided events merged by Resolver | `./query_feasibility_analysis/q4-schedule-to-seen.md` |

Each canned query returns either a structured object or a distribution — never a bare number when the negative cases are meaningful.

---

## Evals

Two-layer test pyramid, both opt-in via `pytest --eval` (gated by `@pytest.mark.eval`; default `pytest` skips them so unit tests stay fast).

| Layer | Path | Scope | Latency | Purpose |
|---|---|---|---|---|
| Layer 1 — golden regression | `tests/eval/test_golden.py` | Full pipeline on both sample claims, all 4 queries | ~85s when API is fresh; hangs on Gemini rate-limit (15 RPM free tier) | End-to-end signal; catches reconciliation + cross-stage interactions |
| Layer 2 — per-extractor | `tests/eval/test_appointment_extractor.py` + `appointment_fixtures.py` | One note → extractor → expected candidates | sub-10s | Fast inner loop for prompt iteration |

**Golden outputs** live in `tests/golden/<claim_id>.json` and are sectioned by `=== qN ===` delimiters. **Layer 1 pass criteria** mix aggregate recall/precision floors with a hard `REQUIRED_APPOINTMENTS` list (in `tests/eval/comparators.py`) — specific known-must-pass cases that aggregate metrics could otherwise mask.

**Layer 2 fixtures** are named with an issue prefix (A* = "attended" recall cases, B* = bare past tense, F* = false-positive scenarios). Each fixture documents the real-note pattern it captures and references the issue ID from `docs/extraction-improvement-roadmap.md`.

**Workflow when a Layer 1 failure surfaces:**
1. Diagnose against the source notes (`sample_claim_notes/sample_claim_notes{1,2}.md`).
2. Write a Layer 2 fixture that reproduces the failure on a single note.
3. Fix the prompt/code; confirm the fixture passes.
4. Re-run Layer 1 to verify the downstream metric moves.

**Caveats baked into the setup:**
- **N=2 claims.** Both Layer 1 goldens are from the same author. There is no held-out set; Layer 2 fixtures are authored from the same notes Layer 1 judges. Passing fixtures ≠ generalization; treat them as regression tests, not evidence of robustness.
- **Goldens are curator-chosen.** When sources contradict (e.g. claim 1's 8-22 Harmon vs "yesterday" email on 8/21), the golden picks one. A Layer 1 failure is sometimes the golden being wrong, not the system.
- **temp=0 ≠ bit-deterministic.** Both LLM clients are pinned to `temperature=0.0`, which reduces but does not eliminate stochasticity. A "passing" fixture passes with high probability, not certainty.
- **Rate limits.** Gemini free-tier flash-lite is 15 RPM. Back-to-back Layer 1 runs will hang; space them out.

**Known issue backlog:** `docs/extraction-improvement-roadmap.md` catalogs extractor issues (E1-E11) and reconciliation issues (R1-R8) with win priorities. Attack the highest-ranked item that has a clean fixture path.

---

## How the user works

A few observed patterns. Honoring these saves rework:

- **Push back on speculative scope.** If a field, event type, or stage isn't required by a query in front of us, the default answer is "not yet" with an extension path noted. DD-012 was a deliberate trim of earlier scope creep.
- **Justify before adding.** New fields/events need a named query consumer. "We might want it later" is not sufficient.
- **Depth before breadth.** The user walks topics one at a time (Q1 fully, then Q2 fully, …) and rejects big-bang plans. Match that cadence — don't pre-emptively redesign three queries when one is being discussed.
- **Prefers discriminated unions, first-class negatives, and explicit evidence requirements** over `null`-laden return types and inferred state.
- **Iterates on docs.** Many md files have been rewritten multiple times. When changing one, check the others (especially `DESIGN.md`, `design-decisions.md`, `data-modeling.md`) for downstream inconsistencies.
- **Will ask for record-keeping when it matters.** New design decisions get a DD entry; existing DDs get cross-referenced. Don't invent a DD without asking.

---

