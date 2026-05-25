# Extraction & Reconciliation — improvement roadmap

Open issues and the eval investments that attack them. Scope: the
appointment-event pipeline. Q1 (RTW), Q3 (reserves), Q4 (distribution)
are out of scope — Q3 is deterministic, Q1 is small-surface, Q4's
n=2 corpus can't ground distribution metrics.

---

## Appointment extractor (per-note LLM)

| # | Issue | Eval |
|---|---|---|
| E2 | Date hallucination from indirect references ("date of injury", "the following day", "she then attended") | `F1` fails |
| E3 | Single-letter party preserved (`Dr. F` → `parties=['F']`) despite explicit prompt rule | `F3` fails |
| E4 | Multi-block notes (`Date of Appointment:` + `Next Office Visit:`) lose the occurred half | `A1`, `A6` fail |
| E5 | Dot-separated dates with bullet prefix (`o Date of Appointment: 5.21.25`) — zero recall | `A5` fails |
| E6 | Phone/text outreach extracted as attended visit ("Text to clmt to see if she attended on 1/6") | `E1` fails |
| E7 | Rescheduled visits emitted as `attended` instead of `cancelled`/`missed` | `D1` fails |
| E8 | Bare past tense with no date — inconsistent: sometimes resolves to note_date, sometimes empty | `B1` fails |
| E9 | Inpatient daily evals borderline-extracted despite "don't extract inpatient" prompt rule | `F2` documents current behavior |
| E10 | LLM stochasticity (~15% inter-run variance); not fixable in our code | all evals see drift |
| E11 | `specialty` / `appointment_type` rarely populated | none |

**Already fixed:** Templated blocks without `Plan:` (PT, FCE, EMG) → broader clinical-content enumeration in commit `4d11d55` (Fix 1). `A2`, `A3`, `A4` pass.

### Win priority

1. **E3** — mechanical filter in `_normalize_party` OR stronger prompt example. ~30 min.
2. **E6** — enumerate phone-outreach patterns in the negative-cases section. ~30 min.
3. **E4** — prompt rule: "if a note has both a `Date of Appointment:` block AND a `Next Office Visit:` line, emit two candidates." ~1 hr.
4. **E5** — add a dot-separated date example to the prompt. ~15 min.
5. **E7** — prompt rule mapping "rescheduled" → cancelled. ~30 min.
6. **E2** — hardest. Two paths: pass DOL into the per-note prompt so "date of injury" resolves, OR add a quote-has-digit/relative-phrase mechanical check.
7. **E8** — decide intended behavior, fix prompt or fixture.
8. **E10 / E11** — accept.

### Eval investment

- **Mine ~75 fixtures from `tests/golden/*.json`** by walking each Q2 appointment's `evidence` list and producing one fixture per `(claim, note_date)`. Broader corpus coverage on top of the 25 hand-curated.
- **Iterate prompts against the suite** until each priority item passes. Promote fixed appointments to `tests/eval/comparators.py::REQUIRED_APPOINTMENTS` to lock in Layer 1.

---

## Reconciliation (per-cluster LLM + deterministic clustering)

Most reconciliation bugs are cross-note — invisible at the per-note
extractor level.

| # | Issue | Eval |
|---|---|---|
| R2 | No cross-note date arbitration. 7 notes say 6-27 + 1 note says "she attended today" on 6-26 → two separate canonical events instead of voting on the majority date. | Layer 1 only (indirect) |
| R3 | Same-facility-same-day, different-clinician + empty-party retrospective absorbs into wrong cluster (DD-016 known limitation). | none |
| R5 | Party union too loose. If one contributing note misattributes a party (OCCM on the 6-23 EMG), reconciliation accepts it. No cross-note validation. | none |
| R6 | Retrospective-recap status promotion vulnerability — a retrospective saying "attended" can promote a cluster whose real status was missed. | Layer 1 only |
| R7 | Per-cluster LLM stochasticity (same E10 root cause). | all |

**Already fixed:** R1 — empty-party-on-both-sides absorption in `_can_join` over-merged 1/3 + 1/6 inpatient evals. Tightened to require exact same date when both candidates have empty parties.

### Win priority

1. **R2** — highest impact, hardest. Options: mode-vote on candidate dates within tolerance, down-weight retrospective sources, or hand the band to a single LLM call. Worth a separate design doc.
2. **R5** — require party to appear in ≥2 contributing notes' quotes (or be from a templated-record source) before promoting.
3. **R6** — prompt-tweak to the per-cluster LLM: discount retrospective sources for status decisions.
4. **R3** — accept the limitation. `appointment_type` as secondary discriminator was explored and deferred (DD-012 YAGNI).
5. **R7** — accept.

### Eval investment

Build reconciliation-specific fixtures: hand-constructed candidate
lists (Python dataclasses → `Event` objects) with expected canonical
events. Runs only the per-cluster LLM, not per-note — cheaper than
Layer 1, more targeted than extractor evals.

**Scenario sketches:**

```
R2: 7 candidates say 6-27, 1 says 6-26 → expect one event on 6-27.
R3: 5-16 Harmon + 5-16 Vega + 5-16 empty → 2 events; empty stays singleton.
R5: 3 say [Neurology Partners], 1 says [OCCM] → exclude OCCM from canonical.
R6: 4 say missed, 1 retrospective says attended → missed wins per DD-017.
```

---

## Plan

**Phase 1 — extractor (do first).**
- [ ] Mine ~75 fixtures from goldens into `tests/eval/appointment_fixtures_mined.py`
- [ ] Fix E3, E4, E5, E6, E7 in priority order
- [ ] Decide on E8
- [ ] Address E2 (hardest)
- [ ] Promote each fixed appointment to `REQUIRED_APPOINTMENTS`

**Phase 2 — reconciliation.**
- [ ] Build `tests/eval/reconciliation_fixtures.py` with R2/R3/R5/R6 scenarios
- [ ] Pick a design for R2 (cross-note date arbitration) and ship it
- [ ] Fix R5 if a query consumer needs it
- [ ] Document R3 / R7 as accepted limitations

**Phase 3 — ratchet Layer 1.**
- [ ] Tighten `Q2_RECALL_FLOOR` 0.85 → 0.90 → 0.95 as recall improves
- [ ] Tighten `Q2_PRECISION_FLOOR` 0.80 → 0.85 → 0.90
- [ ] Once R2 lands, add 2025-06-27 Harmon and 2025-08-22 Harmon to `REQUIRED_APPOINTMENTS`
- [ ] Re-curate goldens to remove now-fixed manual exclusions

---

## Pointers

- DD-016 / DD-017 / DD-019 / DD-022 / DD-023 — `design-decisions.md`
- Layer 1 eval — `tests/eval/test_golden.py`
- Extractor fixtures — `tests/eval/appointment_fixtures.py`
- Golden curation log — `tests/golden/README.md`
- Appointment prompt — `src/claims/extractor/appointment.py`
- Reconciliation logic — `src/claims/extractor/appointment_reconciliation.py`
