# Claims — workers' comp note parsing

Parses unstructured workers' compensation claim notes into a
queryable SQLite dataset and answers four canned queries.

> **Status: Phase 1 (domain model).** Pipeline scaffold + pydantic
> models for Claim / Note / Event. See `docs/implementation-plan.md`
> for the phased roadmap.

## Design docs

Read in order (also tracked in `CLAUDE.md`):

1. `docs/Exercise.md` — the brief.
2. `docs/DESIGN.md` — master design.
3. `design-decisions.md` — DD-001 … DD-013.
4. `docs/data-modeling.md` — `Claim` + `Event` schema.
5. `docs/normalizer.md` — Normalizer deep dive.
6. `docs/extractor.md` — Extractor deep dive.
7. `docs/resolver.md` — Resolver algorithm.
8. `query_feasibility_analysis/` — per-query feasibility (Q3 → Q1 → Q2 → Q4).
9. `docs/claims file analysis.md` — sample-data analysis.

## Setup

Requirements: Python 3.12, [`uv`](https://github.com/astral-sh/uv).

```powershell
# Install uv if needed
py -3.12 -m pip install --user uv

# Create venv and install deps
py -3.12 -m uv sync

# Configure secrets
Copy-Item .env.example .env
# Decrypt the OpenAI key from docs/Exercise.md and paste into .env
```

## Develop

```powershell
# Run tests
py -3.12 -m uv run pytest
```

## Layout

```
src/claims/
  loader/       # file → RawNoteBlock
  normalizer/   # RawNoteBlock → Note (header parsed, body cleaned)
  extractor/    # Note → Event (rule + LLM extractors)
  resolver/     # Events → ResolvedEvents (dedup, merge, derive)
  store/        # SQLite persistence
  query/        # Q1–Q4 canned queries
tests/          # mirrors src/claims/ layout
```
