# Golden outputs

Frozen, hand-verified outputs of the four canned queries for the two
sample claims. Used by the Layer 1 regression eval at
`tests/eval/test_golden.py` (run with `pytest --eval`).

**These files do not byte-match any single ingest run.** They are
manually curated to remove known-bad LLM artifacts from the
otherwise-imperfect raw output. Hand-verification was performed once
to establish a defensible "expected" baseline; future updates follow
the same discipline.

## Layout

```
tests/golden/
  1-29RT.json     # Q1, Q2, Q3 — Q4 excluded by design
  2-248KR.json    # Q1, Q2, Q3 — Q4 excluded by design
```

Same `=== qN ===` block structure as
`sample_claim_notes/query_outputs/*.json`, minus the `=== q4 ===`
section. Q4 is a distribution derived from Q2; setting a defensible
golden bar on a distribution from a 2-claim corpus is unreliable, so
Layer 1 evals ignore Q4 entirely. Add Q4 back if the corpus grows or
a consumer needs distribution-level regression detection.

## Manual edits made at freeze time

### Claim 1 (1-29RT)

Raw ingest produced 25 attended appointments in Q2. Three are
artifacts of known bugs and were removed:

| Removed entry | Why |
|---|---|
| `2025-02-05 Parkview Rehab Center / Ruiz` | Date hallucination — evidence is about the 1/17 discharge to Parkview Rehab, the LLM tagged it as 2/5 (the date of the adjacent Harmon visit). |
| `2025-06-26 Harmon` | Date attribution error — the 6/26 note used present tense ("she attended her follow-up") about the 6/27 visit (7 other notes confirm 6/27). The real 6/27 Harmon visit thus appears absent from the golden, but the false 6/26 entry is gone. |
| `2025-08-20 Harmon` | Date drift — evidence is about an 8/22 scheduled visit, mistagged as 8/20. |

One party label was corrected:

| Fix | Why |
|---|---|
| `2025-06-23` parties `['OCCM']` → `[]` | The 6/23 EMG is real (templated `Date of Appointment: 6-23-25` block with an `EMG/NCS report:`). The "OCCM" label is wrong — OCCM appears elsewhere in the corpus as a PT-scheduling clearinghouse, not as the EMG provider. Real EMG provider was unnamed. |

### Claim 2 (2-248KR)

Raw ingest produced 11 attended appointments. Two are artifacts and
were removed:

| Removed entry | Why |
|---|---|
| `2025-05-21 Occupational Health Clinic` | Date hallucination — evidence is from the 4/22 Caldwell IME description, the LLM tagged it as 5/21 (adjacent to the real Farano 5/21 visit). |
| `2025-08-13 Farano` | Status error — evidence shows the 8/13 appointment was **missed/rescheduled** ("Fwp w/ Dr. Farano was on 8/13 however clmt had to reschedule"). Q2 filters on `status='attended'`, so a missed appointment should not appear here. |

## What's preserved without comment

- `2025-01-03 Regional Medical Center` in claim 1 — an inpatient
  evaluation. The appointment-extractor prompt says don't extract
  inpatient days, but this one slipped through. Left in goldens to
  document current behavior (not a regression to fix unless prompt
  policy changes).
- `2025-05-05` MRI in claim 2 — empty parties, but the procedure is
  real and there is no clinician to name.
- Some events that the raw extraction should have caught (e.g. the
  real `2025-06-27` Harmon visit, the real `2025-08-22` Harmon visit
  in claim 1) are **not** in the golden, because they were displaced
  by the bad entries we just removed and a fresh ingest only produces
  one or the other on any given run. The `REQUIRED_APPOINTMENTS` list
  in `tests/eval/comparators.py` does not encode these as required
  for that reason. If you ever curate the goldens to a state where
  these are reliably present, promote them.

## Updating goldens after an intentional improvement

1. Run `pytest --eval` and observe failures.
2. Read the diff carefully — is the new output **better** than the
   golden, or worse?
3. If better: re-run ingest, copy
   `sample_claim_notes/query_outputs/<id>.json` → `tests/golden/<id>.json`,
   re-apply the curation above (remove known-bad entries by inspection,
   strip Q4), commit with a message describing the improvement.
4. If worse: revert your change.

The curation step in (3) is unavoidable while the underlying LLM has
the failure modes documented above. Once those are fixed (or the
model is upgraded such that they disappear), goldens can be a raw
copy of the ingest output.
