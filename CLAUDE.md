# CLAUDE.md — orientation for new sessions

This repository is the **Adaptional Take-Home Exercise**: design and (eventually) build a system that ingests unstructured workers'-compensation claim notes, parses them into structured events with an LLM, stores them in SQLite, and answers four canned queries over a corpus.

**The repo is currently design-complete and implementation-empty.** Several long md files capture deeply iterated design decisions. Do not bulldoze past them. Read in the order below before suggesting changes or writing code.

---

## Reading order

1. **`./Exercise.md`** — the original take-home brief. The four queries and the input shape come from here.
2. **`./DESIGN.md`** — master design document. Sections cover the queries → facts mapping, the domain model, the pipeline architecture (Ingest → Extract → Normalize → Resolve → Store → Query), and scope.
3. **`./design-decisions.md`** — numbered, accepted design decisions (**DD-001 … DD-012**). Each entry has a rationale and what it commits us to. If a fresh idea conflicts with one of these, the DD wins until explicitly re-litigated.
4. **`./data-modeling.md`** — Claim and Event schema deep dive. Active event types, future-but-not-built event types, the JSON-attributes tradeoff, and the promotion path from hot JSON field to typed column.
5. **`./normalizer.md`** — Normalizer stage deep dive. Two concerns: destructive text cleanup (mojibake repair, line endings, whitespace) and non-destructive date interpretation via a shared date parser. Why body dates are not rewritten in-place (the `evidence_quote` substring check depends on body fidelity), the two-digit-year pivot rule, yearless-date inference, the date-format and mojibake catalogs, and the "flag, don't paper over" failure-mode policy.
6. **`./extractor.md`** — Extractor stage deep dive. The hybrid rule-vs-LLM principle, the `Extractor` interface, the catalog of all five active extractors (Reserve, Appointment Marker as fast-path, Appointment LLM as general path, RTW, RTW Terminal) with triggers / output schemas / rationale, the four LLM contract principles (schema-constrained output, discriminated-union empty case, required `evidence_quote` with substring check, explicit negative prompt rules), multi-extractor coordination on a single note, and failure modes.
7. **`./resolver.md`** — Resolver algorithm deep dive. Five-pass algorithm (normalize → match-key → merge → derive → emit), match-key construction per event type, merge rules, the pluggable matching-strategy interface, provider canonicalization, fuzzy-date window rules, failure modes, and testability. Load-bearing for Q1's dedup, Q2's status precedence, Q3's `delta` derivation, and Q4's cross-note merge.
8. **`./query_feasibility_analysis/README.md`** — index for the per-query feasibility docs. Reading order inside that folder is Q3 → Q1 → Q2 → Q4 (by increasing difficulty):
   - `q3-reserve-changes.md` — pure rule extraction, never LLM
   - `q1-return-to-work.md` — LLM extractor with strict prompt contract; discriminated-union return
   - `q2-appointments-attended.md` — hybrid extraction; the dominant driver of the Resolver stage
   - `q4-schedule-to-seen.md` — cross-note merge; alone justifies the Resolver as a first-class stage
9. **`./claims file analysis.md`** — analysis of the two sample claim files (`./sample_claim_notes/`). What real input looks like, what surface forms each query's facts appear in.

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
- **Status precedence for appointments:** `attended > missed > cancelled > scheduled`. Resolver merges by strongest evidence, not by chronology.
- **Financial data never goes through the LLM.** Q3 (reserve changes) is pure regex against templated text — see `q3-reserve-changes.md`. Money + LLM = silent rounding errors.
- **SQLite via `better-sqlite3`.** Date math uses `julianday(...) - julianday(...)`. JSON access uses `json_extract(attributes, '$.field')`. Expression indexes go on the hot `json_extract` paths.
- **One pipeline, not branched by claim type.** Claim-type-specific behavior lives in prompts and resolver parameters, not in a forked pipeline. (User explicitly considered and rejected a per-claim-type pipeline fork.)

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

## How the user works

A few observed patterns. Honoring these saves rework:

- **Push back on speculative scope.** If a field, event type, or stage isn't required by a query in front of us, the default answer is "not yet" with an extension path noted. DD-012 was a deliberate trim of earlier scope creep.
- **Justify before adding.** New fields/events need a named query consumer. "We might want it later" is not sufficient.
- **Depth before breadth.** The user walks topics one at a time (Q1 fully, then Q2 fully, …) and rejects big-bang plans. Match that cadence — don't pre-emptively redesign three queries when one is being discussed.
- **Prefers discriminated unions, first-class negatives, and explicit evidence requirements** over `null`-laden return types and inferred state.
- **Iterates on docs.** Many md files have been rewritten multiple times. When changing one, check the others (especially `DESIGN.md`, `design-decisions.md`, `data-modeling.md`) for downstream inconsistencies.
- **Will ask for record-keeping when it matters.** New design decisions get a DD entry; existing DDs get cross-referenced. Don't invent a DD without asking.

---

## State of the repo

**Design-complete (committed in md):**
- Domain model (Claim + Event with 4 active types)
- Twelve accepted design decisions
- Per-query feasibility for Q1–Q4
- Pipeline architecture (5 stages: Ingest → Extract → Normalize → Resolve → Query)
- Source-data analysis on the two sample claims

**Not yet started (no code exists):**
- SQLite schema file
- Ingestion / extraction / normalization / resolver code
- LLM prompt library
- Canned query functions
- Eval harness (DD-009, deferred to post-MVP)
- Provider canonicalization layer

When implementation begins, the canned query function signatures already exist in the query feasibility docs — treat them as contracts.

