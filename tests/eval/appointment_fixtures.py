"""Hand-labeled fixtures for the AppointmentExtractor eval.

Each `Fixture` is a (note, expected-candidates) pair. The eval runner
in `test_appointment_extractor.py` calls
`AppointmentExtractor.extract(note)` and compares emitted candidates
to `expected` per the matching rules below.

**Matching rules (see `_match_candidate` in test_appointment_extractor.py)**:
- date: exact match
- status: exact match
- parties: each substring in `expected.parties_substrings` must
  appear in some emitted candidate's parties (case-insensitive
  substring match). Empty list means "no party constraint".
  `parties_must_be_empty=True` asserts the candidate's parties is `()`.
- kind (`occurred` vs `scheduled`): exact when set; ignored when None

**Pass criteria per fixture (default):** all expected candidates
matched AND no extra candidates emitted. Override per-fixture with
`allow_extras` for cases where the LLM legitimately can pick up extra
mentions we don't want to over-constrain.

Most fixtures are real excerpts from the two sample claims. A few
synthetic fixtures cover edge cases the corpus doesn't have (clean
no-show, multi-clinician same-day, etc.). Marked `synthetic=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


Status = Literal["attended", "missed", "cancelled", "scheduled"]
Kind = Literal["occurred", "scheduled"]


@dataclass(frozen=True)
class ExpectedCandidate:
    """One candidate the LLM should emit for this note."""

    date: date
    status: Status
    parties_substrings: tuple[str, ...] = ()
    """Each entry must appear as a case-insensitive substring in some
    emitted candidate's parties. Empty tuple = no party constraint."""

    parties_must_be_empty: bool = False
    """If True, the emitted candidate's parties must be exactly []."""

    kind: Kind | None = None
    """Discriminates `occurred_on` vs `scheduled_for_date` on the
    emitted attributes. None = either is acceptable."""


@dataclass(frozen=True)
class Fixture:
    """One eval case: a note + expected extractor output."""

    name: str
    note_date: date
    body: str
    expected: tuple[ExpectedCandidate, ...] = ()
    activity: str = "Resolution Strategy"
    author: str | None = None
    synthetic: bool = False
    notes: str = ""
    allow_extras: bool = False
    """If True, emitted candidates beyond `expected` don't fail the
    test. Used for fixtures where the LLM legitimately can pick up
    extra mentions that aren't worth constraining."""


# --- Group A — Templated `Date of Appointment:` block recall ----


A1_OFFICE_VISIT_WITH_PLAN = Fixture(
    name="A1-office-visit-plan-header",
    note_date=date(2025, 6, 26),
    body=(
        "---Date of Appointment: 6-27-25\n"
        "Next Office Visit: 7-25-25 at 11am\n"
        "Name of physician: Dr. Harmon\n"
        "Clinic: Orthopedic & Spine Associates\n"
        "Specialty: Spine surgery\n"
        "Diagnosis: Central cord syndrome C4-5 and C5-6; s/p decompression and fusion. Prognosis: good.\n"
        "Assessment: Returns today for the first time ambulating out of the wheelchair with her walker. "
        "Strength is improving but still has some weakness.\n"
        "Plan: She is going to try cyclobenzaprine for lower back spasms. She will continue with her "
        "prescribed exercises as well as her own home exercises.\n"
        "Work Status: Out of work"
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 6, 27),
            status="attended",
            parties_substrings=("Harmon", "Orthopedic"),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 7, 25),
            status="scheduled",
            parties_substrings=("Harmon",),
            kind="scheduled",
        ),
    ),
    notes="Standard office-visit templated block with `Plan:` header.",
)


A2_PT_WITH_SUBJECTIVE_HEADERS = Fixture(
    name="A2-pt-subjective-objective-assessment",
    note_date=date(2025, 7, 28),
    body=(
        "---Date of Appointment: 7-17-25\n"
        "Clinic: Valley PT Group\n"
        "Specialty: Physical Therapy\n"
        "Subjective: Pt c/o pain and tightness in neck and lower back area, pt has difficulty with "
        "lifting her legs. Pt reports PS 5-6/10 on LS and Rt LE.\n"
        "Objective: Therapeutic exercise, manual therapy, therapeutic activity/kinetic, heat and E-stim.\n"
        "Assessment: Pt responded well to treatment."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 7, 17),
            status="attended",
            parties_substrings=("Valley PT",),
            kind="occurred",
        ),
    ),
    notes="PT note — uses Subjective/Objective/Assessment instead of Plan:.",
)


A3_FCE_WITH_FCE_SUMMARY = Fixture(
    name="A3-fce-with-fce-summary-header",
    note_date=date(2025, 10, 17),
    body=(
        "---Date of Appointment: 10-14-25\n"
        "Provider: Bridge Functional Capacity Evaluators\n"
        "Specialty: Occupational Therapy / FCE\n"
        "FCE Summary: Patient A was evaluated over a 4-hour period on 10/14/2025. Validity testing "
        "indicated a reliable effort. Results indicate the claimant is capable of sedentary to "
        "light-duty work. She is able to sit for up to 30 minutes at a time and stand briefly with "
        "assistive device. No lifting greater than 10 lbs from floor to waist."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 10, 14),
            status="attended",
            parties_substrings=("Bridge",),
            kind="occurred",
        ),
    ),
    notes="FCE block — uses FCE Summary: header (no Plan: section).",
)


A4_EMG_WITH_DICTATION_CONFUSION = Fixture(
    name="A4-emg-dictation-confusion",
    note_date=date(2025, 6, 25),
    body=(
        "---Date of Appointment: 6-23-25\n"
        "Provider:\n"
        "Specialty: Neurology\n"
        "EMG/NCS report:\n"
        "At your request, on the 18th of June, 2025 I evaluated Patient A in my office for EMG "
        "and nerve conduction studies. As you can see, the study was normal. There is no objective "
        "evidence on today's examination of a radicular or neurogenic etiology contributing to the "
        "reported symptoms.\n"
        "Interpretation: Normal study"
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 6, 23),
            status="attended",
            parties_substrings=(),  # provider field is blank in this note
            kind="occurred",
        ),
    ),
    notes=(
        "EMG block with `EMG/NCS report:` header AND a dictation artifact "
        "(`on the 18th of June, 2025 I evaluated...`). The 6-23 attended "
        "event must be emitted; the 6-18 dictation phrase must NOT produce "
        "a spurious second event."
    ),
)


A5_DOT_SEPARATED_DATE_BULLET_STYLE = Fixture(
    name="A5-dot-separated-bullet-style",
    note_date=date(2025, 5, 23),
    body=(
        "Note:\n"
        "o Date of Appointment: 5.21.25\n"
        "o Next Office Visit if Scheduled: not yet scheduled\n"
        "o Name of Physician/Clinic/Specialty: Dr. Farano / Pain Management Associates / Pain Management\n"
        "o Diagnosis & Assessment: Lumbar spondylosis. Examination reveals positive facet loading "
        "in the lumbar spine.\n"
        "o Plan: Bilateral L3-L5 MBB and RFA if positive results. HEP and PT evaluation encouraged.\n"
        "o Work Status and Restrictions: out of work"
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 5, 21),
            status="attended",
            parties_substrings=("Farano",),
            kind="occurred",
        ),
    ),
    notes=(
        "Bullet-style with `o` prefix and dot-separated date (5.21.25). "
        "Tests format-tolerance and that single-letter party 'Dr. F' is "
        "NOT what gets extracted (the full provider line names Farano)."
    ),
)


A7_RECORDS_RECEIPT_PREFIX_THEN_TEMPLATED = Fixture(
    name="A7-records-receipt-prefix-then-templated",
    note_date=date(2025, 11, 14),
    activity="Investigation",
    body=(
        "Note: TCM received medical records. Reviewed and saved to file.\n"
        "---Date of Appointment: 11-14-25\n"
        "Next Office Visit: PRN\n"
        "Name of physician: Dr. Harmon\n"
        "Clinic: Orthopedic & Spine Associates\n"
        "Specialty: Spine\n"
        "Diagnosis & Assessment: Anterior cord syndrome predominating "
        "over central cord syndrome at C4-5 and C5-6. She has reached "
        "a plateau in her neurological recovery.\n"
        "Plan: Placing her at maximum medical improvement effective today. "
        "Released to sedentary modified duty.\n"
        "Work Status: Modified duty\n"
        "MMI Date: 11-14-25"
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 11, 14),
            status="attended",
            parties_substrings=("Harmon", "Orthopedic"),
            kind="occurred",
        ),
    ),
    notes=(
        "Real claim 1 pattern. The 'TCM received medical records' preamble "
        "looks like a records-receipt notice (matches the prompt's "
        "DO NOT EXTRACT category), but is followed by a templated "
        "medical-record block that IS the actual encounter. The LLM "
        "currently returns empty for the whole note — the receipt preamble "
        "preempts the templated block. Fix requires an exception to the "
        "records-receipt rejection rule."
    ),
)


A6_MULTI_BLOCK_SCHEDULED_AND_OCCURRED = Fixture(
    name="A6-multi-block-occurred-and-next",
    note_date=date(2025, 4, 10),
    body=(
        "Date of Appointment: 4-11-25\n"
        "Next Office Visit: 5-16-25 at 11am\n"
        "Name of physician: Dr. Harmon\n"
        "Clinic: Orthopedic & Spine Associates\n"
        "Specialty: Spine\n"
        "Diagnosis: Cervical cord compression syndrome.\n"
        "Plan: Continue outpatient PT, follow up in 4 weeks."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 4, 11),
            status="attended",
            parties_substrings=("Harmon",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 5, 16),
            status="scheduled",
            parties_substrings=("Harmon",),
            kind="scheduled",
        ),
    ),
    notes=(
        "Templated visit-record block followed by `Next Office Visit:`. "
        "Both should be emitted — the visit (occurred) and the next "
        "scheduled appointment."
    ),
)


# --- Group B — Prose attendance ----------------------------------


B1_PAST_TENSE_PROSE = Fixture(
    name="B1-past-tense-prose",
    note_date=date(2025, 6, 26),
    body=(
        "Note: She attended her follow-up with Dr. Harmon. She arrived at this appointment "
        "without a wheelchair and was ambulating with the assistance of a walker. Dr. Harmon "
        "reviewed the EMG findings."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 6, 26),
            status="attended",
            parties_substrings=("Harmon",),
            kind="occurred",
        ),
    ),
    notes=(
        "Past-tense 'attended' without explicit date; resolves to "
        "note_date. Real claim 1 example — known to misalign with the "
        "actual 6/27 visit (source data inconsistency, see "
        "design-decisions.md); we accept the per-note extractor output."
    ),
)


B2_FIRST_PERSON_FIELD_NURSE = Fixture(
    name="B2-first-person-field-nurse",
    note_date=date(2025, 5, 30),
    body=(
        "Note: I traveled to Dr. Vega's office today, June 2nd to attend the scheduled follow-up "
        "appointment with Patient A. She presented with continued lower extremity weakness."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 6, 2),
            status="attended",
            parties_substrings=("Vega",),
            kind="occurred",
        ),
    ),
    notes=(
        "Field nurse first-person narrative with an explicit date "
        "('June 2nd') that resolves forward from the note_date. "
        "Tests date resolution from 'today, June 2nd' phrasing."
    ),
)


B3_RETROSPECTIVE_OMNIBUS = Fixture(
    name="B3-retrospective-omnibus-recap",
    note_date=date(2025, 8, 9),
    body=(
        "SINCE LAST ACTION PLAN: An appointment was scheduled for 2-5-25. EE attended her "
        "appointment with Dr. Harmon on 2-5-25. Care was directed to Dr. Vega, and the "
        "appointment took place on 2-28-25. EE attended her appointment with Dr. Harmon on 3-6-25. "
        "Ophthalmology evaluation with Dr. Sinclair on 3-14-25. EE attended an appointment with "
        "Dr. Vega on 3-28-25."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 2, 5),
            status="attended",
            parties_substrings=("Harmon",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 2, 28),
            status="attended",
            parties_substrings=("Vega",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 3, 6),
            status="attended",
            parties_substrings=("Harmon",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 3, 14),
            status="attended",
            parties_substrings=("Sinclair",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 3, 28),
            status="attended",
            parties_substrings=("Vega",),
            kind="occurred",
        ),
    ),
    notes=(
        "Retrospective omnibus recap with 5 attended visits in one paragraph. "
        "Tests that the extractor splits multi-event prose into discrete "
        "candidates per the 'one entry per visit' prompt rule."
    ),
    allow_extras=True,  # LLM might also pick up the 2-5 scheduled mention
)


B4_OHC_FOLLOW_UP_SENTENCE = Fixture(
    name="B4-ohc-followup-sentence",
    note_date=date(2025, 1, 28),
    body=(
        "Note: She followed up with Occupational Health Clinic on 1/6/25 where she was referred "
        "to physical therapy, which she is currently completing. She had a follow-up at "
        "Occupational Health Clinic on 1/20 and we are still working to obtain those notes."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 1, 6),
            status="attended",
            parties_substrings=("Occupational Health",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 1, 20),
            status="attended",
            parties_substrings=("Occupational Health",),
            kind="occurred",
        ),
    ),
    notes="Two distinct OHC follow-ups in one note. Real claim 2 content.",
)


# --- Group C — Scheduling notices --------------------------------


C1_NEXT_APPOINTMENT_SCHEDULED = Fixture(
    name="C1-next-appointment-scheduled",
    note_date=date(2025, 6, 26),
    body=(
        "Note: Her next appointment is scheduled on 7-25-25 at 11am. Dr. Harmon will follow "
        "up regarding the EMG results."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 7, 25),
            status="scheduled",
            parties_substrings=("Harmon",),
            kind="scheduled",
        ),
    ),
    notes="Forward-dated scheduling sentence — should emit a `scheduled` candidate.",
)


C2_TEMPLATED_SCHEDULE_HEADER = Fixture(
    name="C2-templated-schedule-header",
    note_date=date(2025, 4, 14),
    body=(
        "Note: outpatient PT has been scheduled at Valley PT Group: Schedule Date Time: "
        "Apr 17 2025 9:00AM. Transportation arranged."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 4, 17),
            status="scheduled",
            parties_substrings=("Valley PT",),
            kind="scheduled",
        ),
    ),
    notes=(
        "Templated 'Schedule Date Time: Apr 17 2025 9:00AM' header — "
        "non-standard date format ('Apr 17 2025') must resolve."
    ),
)


C3_TWO_SCHEDULED_IN_ONE_NOTE = Fixture(
    name="C3-two-scheduled-list",
    note_date=date(2025, 2, 27),
    body=(
        "Note: NEXT APPOINTMENTS:\n"
        "Dr. Vega (Neurology) 3-28-25\n"
        "Dr. Harmon (Spine) 4-11-25"
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 3, 28),
            status="scheduled",
            parties_substrings=("Vega",),
            kind="scheduled",
        ),
        ExpectedCandidate(
            date=date(2025, 4, 11),
            status="scheduled",
            parties_substrings=("Harmon",),
            kind="scheduled",
        ),
    ),
    notes=(
        "Two scheduled visits in a templated list. Specialty in parens "
        "(`Neurology`, `Spine`) must NOT land in parties — those are "
        "specialty words, not provider names."
    ),
)


# --- Group D — Missed / cancelled --------------------------------


D1_RESCHEDULED = Fixture(
    name="D1-rescheduled-as-cancelled",
    note_date=date(2025, 8, 29),
    body=(
        "Note: NOV: Fwp w/ Dr. Farano was on 8/13 however clmt had to reschedule. NOV is set for "
        "9/23. She is on the cancellation list. No fwp w/ Dr. Caldwell at this time."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 8, 13),
            status="cancelled",
            parties_substrings=("Farano",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 9, 23),
            status="scheduled",
            parties_substrings=("Farano",),
            kind="scheduled",
        ),
    ),
    notes=(
        "Rescheduled 8/13 = cancelled (per DD-017 precedence vocabulary). "
        "Also picks up the 9/23 rescheduled-to slot as `scheduled`."
    ),
    allow_extras=True,  # status="missed" for 8/13 is also defensible
)


D2_MULTIPLE_MISSED_IN_ONE_NOTE = Fixture(
    name="D2-multiple-missed",
    note_date=date(2025, 8, 29),
    body=(
        "Note: I wanted to follow up on the claimant's case. Unfortunately, she was unable to "
        "attend her appointments scheduled for 08/11 and 08/13, and the rescheduled appointment "
        "has been moved to sometime next month."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 8, 11),
            status="missed",
            parties_substrings=(),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 8, 13),
            status="missed",
            parties_substrings=(),
            kind="occurred",
        ),
    ),
    notes=(
        "Two missed appointments in one sentence. Tests negation-with-list "
        "comprehension: 'unable to attend' applies to both dates."
    ),
)


D3_NO_SHOW = Fixture(
    name="D3-no-show-synthetic",
    note_date=date(2025, 4, 16),
    body=(
        "Note: Clmt was a no-show for her appointment with Dr. Caldwell on 4/15/25. "
        "Clinic staff confirms she did not arrive. Will reach out to reschedule."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 4, 15),
            status="missed",
            parties_substrings=("Caldwell",),
            kind="occurred",
        ),
    ),
    synthetic=True,
    notes="Clean no-show. Corpus only has rescheduled-as-cancelled; synthetic adds the no-show variant.",
)


# --- Group E — Negative cases (should emit ZERO candidates) ------


E1_PHONE_CALL_ABOUT_VISIT = Fixture(
    name="E1-phone-check-on-visit",
    note_date=date(2025, 1, 6),
    body=(
        "Note: Text to clmt to see if she attended her fwp at Occupational Health Clinic on 1/6. "
        "Awaiting response."
    ),
    expected=(),
    notes=(
        "A text message asking whether the patient attended — NOT itself "
        "an appointment. The visit it asks about has no outcome confirmed "
        "in this note; the prompt's 'past visits with no outcome → not "
        "extracted' rule applies."
    ),
)


E2_RECORDS_REQUEST = Fixture(
    name="E2-records-request",
    note_date=date(2025, 3, 14),
    body=(
        "Note: TCM emailed PT script to OCCM for scheduling. Records request sent to Dr. Caldwell's "
        "office for the 3/8 visit notes."
    ),
    expected=(),
    notes=(
        "Records-request action + a date-mentioning records reference. "
        "Neither is an appointment encounter."
    ),
)


E3_FUTURE_INTENT_NO_DATE = Fixture(
    name="E3-future-intent-no-date",
    note_date=date(2025, 11, 14),
    body=(
        "Plan: Follow-up on a PRN basis for acute flares. May follow up with neurology as needed. "
        "She will see ortho if symptoms worsen."
    ),
    expected=(),
    notes="Future intent without a calendar date — undated future plans don't qualify.",
)


E4_CARE_ASSIGNMENT = Fixture(
    name="E4-care-assignment-no-visit",
    note_date=date(2025, 1, 22),
    body=(
        "Note: As Dr. Ruiz is not in network, Dr. Harmon agreed to assume care as the treating "
        "provider. Referral to Dr. Vega (neurology) placed for post-concussion syndrome evaluation. "
        "Authorization for outpatient PT pending."
    ),
    expected=(),
    notes=(
        "Care assignment, referral order, and authorization status — "
        "establishes treatment plan but no dated visit. Prompt explicitly "
        "rejects these."
    ),
)


E5_STATUS_NOTATION_IN_RECAP = Fixture(
    name="E5-status-recap-no-visit",
    note_date=date(2025, 12, 1),
    body=(
        "Note: Status update: Dr. Vega (Neurology) — MMI from neurological standpoint. "
        "Dr. Sinclair, ophthalmology: visual changes pre-existing, no further care. Dr. Harmon "
        "(Spine) — continuing post-op management."
    ),
    expected=(),
    notes=(
        "Status notations / recap list with no dated visit. Should emit "
        "nothing — these record clinical state, not encounters."
    ),
)


# --- Group F — Edge cases & known limitations --------------------


F1_DATE_OF_INJURY_UNDATED = Fixture(
    name="F1-date-of-injury-undated",
    note_date=date(2025, 4, 23),
    body=(
        "Medical treatment to date:\n"
        "1. Initial treatment: Occupational Health Clinic on the date of injury. An exam was "
        "conducted. A muscle relaxer and naproxen were prescribed. IE was also referred to PT. "
        "She went to the ED the following day as she could barely walk."
    ),
    expected=(),
    notes=(
        "Two indirect references: 'on the date of injury' and 'the following "
        "day'. Neither pins to a calendar date in the body. Per prompt: "
        "undated mentions DO NOT emit. Known to fail intermittently — when "
        "this eval starts passing, we've fixed the date-hallucination "
        "class. See design-decisions.md."
    ),
    allow_extras=False,  # strict — these MUST be rejected
)


F2_INPATIENT_DAILY_EVALS = Fixture(
    name="F2-inpatient-daily-evals",
    note_date=date(2025, 2, 6),
    body=(
        "Note: At the 1/3/25 inpatient follow-up, the claimant reported that right lower extremity "
        "symptoms had decreased relative to the left. Another evaluation was completed on 1/6/25. "
        "Improvement noted. The claimant was medically cleared for discharge as of 1/8/25 with the "
        "recommendation of being discharged to a rehabilitation facility."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 1, 3),
            status="attended",
            parties_substrings=(),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 1, 6),
            status="attended",
            parties_substrings=(),
            kind="occurred",
        ),
    ),
    notes=(
        "Documents current behavior. Per prompt, inpatient daily evals "
        "are one stay, not many appointments — but the extractor emits "
        "1/3 and 1/6 as discrete evaluations and skips 1/8 'cleared for "
        "discharge'. Frozen here so a future prompt change forces a "
        "deliberate decision."
    ),
    allow_extras=True,
)


F3_SINGLE_LETTER_PARTY = Fixture(
    name="F3-single-letter-party-strip",
    note_date=date(2025, 5, 23),
    body=(
        "Note: o Date of Appointment: 5.21.25\n"
        "o Name of Physician/Clinic/Specialty: Dr. F\n"
        "o Plan: Bilateral L3-L5 MBB and RFA if positive results."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 5, 21),
            status="attended",
            parties_must_be_empty=True,  # 'Dr. F' should be filtered
            kind="occurred",
        ),
    ),
    synthetic=True,
    notes=(
        "Single-letter provider 'Dr. F' must be stripped per the prompt's "
        "anti-placeholder rule. Visit is real (5-21), but no party should "
        "be recorded since 'F' is a single-letter abbreviation. Synthetic "
        "trim of the real claim 2 note to isolate the behavior."
    ),
)


F4_MULTI_CLINICIAN_SAME_DAY = Fixture(
    name="F4-multi-clinician-same-day",
    note_date=date(2025, 5, 17),
    body=(
        "Note: She attended her follow-up appointment yesterday 5-16-25 with Dr. Harmon at "
        "Orthopedic & Spine Associates. Same day she also had an evaluation with Dr. Vega at "
        "Neurology Partners — both visits in the morning."
    ),
    expected=(
        ExpectedCandidate(
            date=date(2025, 5, 16),
            status="attended",
            parties_substrings=("Harmon",),
            kind="occurred",
        ),
        ExpectedCandidate(
            date=date(2025, 5, 16),
            status="attended",
            parties_substrings=("Vega",),
            kind="occurred",
        ),
    ),
    synthetic=True,
    notes=(
        "Two distinct clinicians, same date — should emit two candidates "
        "with disjoint parties. Per DD-016 they must remain separable in "
        "reconciliation, but at the extractor level both must be visible."
    ),
)


# --- Registry -----------------------------------------------------


ALL_FIXTURES: tuple[Fixture, ...] = (
    A1_OFFICE_VISIT_WITH_PLAN,
    A2_PT_WITH_SUBJECTIVE_HEADERS,
    A3_FCE_WITH_FCE_SUMMARY,
    A4_EMG_WITH_DICTATION_CONFUSION,
    A5_DOT_SEPARATED_DATE_BULLET_STYLE,
    A6_MULTI_BLOCK_SCHEDULED_AND_OCCURRED,
    A7_RECORDS_RECEIPT_PREFIX_THEN_TEMPLATED,
    B1_PAST_TENSE_PROSE,
    B2_FIRST_PERSON_FIELD_NURSE,
    B3_RETROSPECTIVE_OMNIBUS,
    B4_OHC_FOLLOW_UP_SENTENCE,
    C1_NEXT_APPOINTMENT_SCHEDULED,
    C2_TEMPLATED_SCHEDULE_HEADER,
    C3_TWO_SCHEDULED_IN_ONE_NOTE,
    D1_RESCHEDULED,
    D2_MULTIPLE_MISSED_IN_ONE_NOTE,
    D3_NO_SHOW,
    E1_PHONE_CALL_ABOUT_VISIT,
    E2_RECORDS_REQUEST,
    E3_FUTURE_INTENT_NO_DATE,
    E4_CARE_ASSIGNMENT,
    E5_STATUS_NOTATION_IN_RECAP,
    F1_DATE_OF_INJURY_UNDATED,
    F2_INPATIENT_DAILY_EVALS,
    F3_SINGLE_LETTER_PARTY,
    F4_MULTI_CLINICIAN_SAME_DAY,
)
