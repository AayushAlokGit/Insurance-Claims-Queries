# Query Feasibility Analysis

Per-query deep dives for the four sample queries in `Exercise.md`. Each file
takes one query and works backwards through it: what the question actually
*means*, which facts it needs, where those facts live in the notes, how
reliably each can be extracted, and what edge cases the canned implementation
must commit to.

This folder is the bridge between `claims file analysis.md` (the raw data
inventory) and `DESIGN.md` (the polished spec). The data inventory says *what
exists*; this folder says *what we can answer with it, and how*; DESIGN.md
distills that into the chosen architecture.

| # | File | Query |
|---|------|-------|
| Q1 | [`q1-return-to-work.md`](./q1-return-to-work.md) | How long did it take for the employee to return to work? |
| Q2 | [`q2-appointments-attended.md`](./q2-appointments-attended.md) | How many appointments were attended? |
| Q3 | [`q3-reserve-changes.md`](./q3-reserve-changes.md) | How many times did our reserve change? By how much? |
| Q4 | [`q4-schedule-to-seen.md`](./q4-schedule-to-seen.md) | How long does it take to see a provider once scheduled? |

## Reading order

Each file is self-contained, but they get progressively heavier:

- **Q3** is the easiest — pure regex, anchors the system's credibility.
- **Q1** introduces the "discussed vs. occurred" semantic problem that
  motivates LLM-assisted extraction.
- **Q2** is the hardest single-claim extraction problem — appointment
  attendance is rarely stated as such and must often be inferred.
- **Q4** is the easiest single-note problem but the hardest *cross-note*
  problem — it alone justifies the Resolver stage in the pipeline.

## What each file contains

A consistent structure:

1. **The literal question** and what it's really asking
2. **Facts required** to answer it
3. **Where those facts live** in the sample notes (with evidence)
4. **Extraction approach** — rule, LLM, or hybrid, and why
5. **Edge cases & ambiguities** the canned function must resolve
6. **Computation** — the exact arithmetic / SQL shape
7. **Reliability assessment** — what would break this at corpus scale
8. **Design implications** — what this query forces into the architecture
