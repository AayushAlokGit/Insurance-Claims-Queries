"""LLM-based appointment extractor (general path).

The marker extractor (Phase 6) catches templated headers. This
extractor handles everything else — status statements in prose
("EE attended PT on 6/3", "missed the 8/13 follow-up"), novel
scheduling formats, and the wide tail of language that doesn't
match a known marker. See extractor.md §5.3.

The prompt obeys the four LLM-contract principles from
extractor.md §6:

1. Schema-constrained output — `AppointmentExtractionResponse`
   is passed to the structured-outputs API; the provider
   refuses anything that doesn't validate.
2. Discriminated-union empty case — "no appointment in this
   note" is `appointments: []`, not a None field.
3. Required evidence_quote with substring check — every
   candidate must include a verbatim quote; the post-LLM check
   drops any that don't substring-match the body.
4. Explicit negative prompt rules — phone calls, paperwork,
   general communications, and silent past visits are
   explicitly rejected (the prefilter is loose, so the prompt
   is the real false-positive guard).
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from claims.extractor._evidence import EVIDENCE_QUOTE_GUIDANCE
from claims.llm import StructuredLLM, quote_in_body
from claims.llm.base import LLMError
from claims.models import (
    AppointmentAttributes,
    EventEvidence,
    Event,
    Note,
)


# --- Prefilter (DD-019) ------------------------------------------
#
# Per-note extraction is a permissive CANDIDATE GENERATOR under
# DD-019: false positives are fine here because the per-claim
# reconciliation pass cleans them up. The prefilter is just a
# cheap "looks like it could mention an appointment" gate.

_RE_STATUS_VERBS = re.compile(
    r"\b(attended|missed|no[- ]show|did not show|DNA"
    r"|cancell?ed|seen by|EE attended)\b",
    re.IGNORECASE,
)
_RE_SAW_ON = re.compile(r"\bsaw\b.{0,40}\bon\b", re.IGNORECASE)
_RE_APPT_CONTEXT = re.compile(
    r"\b(appointment|visit|follow[- ]?up|scheduled"
    r"|seen|consult|exam|evaluation|referral)\b",
    re.IGNORECASE,
)
_RE_HAS_DATE = re.compile(
    r"\d{1,2}[-/.]\d{1,2}"
    r"|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(uary|ruary|ch|il|e|y|ust|ember|tober)?\b",
    re.IGNORECASE,
)


def _passes_prefilter(body: str) -> bool:
    if _RE_STATUS_VERBS.search(body) or _RE_SAW_ON.search(body):
        return True
    return bool(
        _RE_HAS_DATE.search(body) and _RE_APPT_CONTEXT.search(body)
    )


# --- LLM response schema -----------------------------------------


class _ExtractedAppointment(BaseModel):
    """One appointment the model claims is described in the note.
    `appointment_date` is the date the appointment occurred or
    is set to occur (NOT the note's own date). `evidence_quote`
    must be a verbatim substring of the note body — extractor
    drops any candidate that fails the check.

    `parties` per DD-016: a list of every named individual and
    organization party to *this* appointment, with honorifics
    and location suffixes stripped. Empty list if no identifiable
    party appears."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["attended", "missed", "cancelled", "scheduled"]
    appointment_date: date | None = Field(
        default=None,
        description=(
            "The date the appointment happened (for attended/missed/"
            "cancelled) or is scheduled to happen (for scheduled). "
            "Null if the note does not name a date."
        ),
    )
    parties: list[str] = Field(
        default_factory=list,
        description=(
            "Every named individual or organization that is party to "
            "this specific appointment. Strip honorifics ('Dr.'), "
            "degree suffixes (', MD'), and location suffixes "
            "(\"'s office\", 'at <X>'). Empty list if none identifiable."
        ),
    )
    evidence_quote: str = Field(
        description=(
            "A verbatim substring of the note body that justifies "
            "this appointment. Required."
        ),
    )


class AppointmentExtractionResponse(BaseModel):
    """Wrapper enforcing the discriminated-union empty case —
    `appointments=[]` is the canonical 'no event' response."""

    model_config = ConfigDict(extra="forbid")

    appointments: list[_ExtractedAppointment] = Field(default_factory=list)


# --- Prompt ------------------------------------------------------
#
# Per-note extraction is a permissive candidate generator under
# DD-019: the per-claim reconciliation pass dedups and re-aligns
# parties across notes. The prompt's job is precision of evidence,
# not deduplication.

_SYSTEM_PROMPT = """You extract APPOINTMENT events from a workers'-comp claim note. In this system, an appointment is a MEDICAL encounter only — past, future, or missed — between the claimant and a healthcare provider. Workers'-comp claims also produce legal events (depositions, mediations, hearings, settlement conferences), administrative events (claim-acceptance meetings, adjuster reviews, employer RTW meetings), and vocational events (voc-rehab evaluations, ergonomic assessments). These are real dated events but they are OUT OF SCOPE for this extractor — do not emit them as appointments.

STATUS:
- attended  — the visit happened. A medical-record block (`Date of Appointment: X` followed by `Plan:` / clinical content) counts as attended even without an "attended" verb.
- missed    — claimant did not attend / no-show / DNA / "unable to attend".
- cancelled — cancelled or rescheduled before it could happen.
- scheduled — a future appointment with a specific date.

If a note recaps multiple visits (e.g. a Resolution Strategy `SINCE LAST ACTION PLAN` or `NEXT APPOINTMENTS` section), emit one entry per visit — each entry stands on its own under the proximity rule below.

DATE: explicit date → `appointment_date`. A relative phrase that resolves from the note date ("yesterday", "this morning", "last Friday") → compute it. If you cannot pin the appointment to a specific calendar date ("soon", "next month", "PRN", "around <date>"), DO NOT emit it — undated appointments are useless downstream and will be dropped.

PROXIMITY: one `evidence_quote` per event must contain ALL of (a) the appointment date (literal, relative, or a templated `Date of Appointment:` / `Next Office Visit:` header) AND (b) at least one named party OR the templated medical-record block (which implies the visit) AND (c) an action verb or status word (`attended`, `missed`, `cancelled`, `scheduled`, `visited`, `saw`, `follow-up`, or the templated header). If you cannot find ONE verbatim substring containing all three, emit nothing for that mention — do NOT reach across sentences to assemble (date, party, status) from disjoint clauses. Under-emission is recoverable downstream; over-emission with cross-attributed parties is not.

DO NOT EXTRACT:
- Pure communications without a visit-action verb (emails, texts, fax cover sheets, records-receipt notices).
- Care assignments and orders ("Dr. Harmon agreed to assume care", "referred to pain management", "MRI ordered") — these establish treatment, not a dated visit.
- Status notations in recap lists ("Dr. Vega (Neurology) — MMI", "Dr. Sinclair, ophthalmology: no further care needed") — these record patient state, not a visit.
- Hospital inpatient days (consecutive daily evals during an admission). One inpatient stay is not many appointments.
- Mentions of a provider only in the diagnosis line, history, or plan that don't pair with a date in the same clause.

{evidence_guidance}

PARTIES — list every named individual and organization that the evidence_quote shows is party to THIS visit:
- Attending clinician(s): named persons only.
- Facility / clinic / hospital: named organizations only.

DO NOT put in `parties`:
- The claimant under any label — pronouns, role labels ("claimant", "patient", "EE", "IE", "IW", "HR"), or pseudonyms ("Patient A", "the IW", "Subject").
- Specialty names alone ("ophthalmology", "neurology", "ortho", "spine", "pain mgmt"). Specialty is not a name.
- Generic phrases or unnamed roles ("the doctor", "the office", "FCM", "TCM", "field nurse", "case manager", "interpreter") standing alone.
- Single-letter abbreviations or placeholders ("F", "C", "Dr. F").
- Referring providers named only in history / treatment plan / referrals.

Strip honorifics ("Dr.", "Mr."), degree suffixes (", MD", ", DO"), and location suffixes ("'s office", "at <clinic>"). A clinician and the clinic they work at are TWO entries, not one. Empty list if no named party appears — never invent one.

No appointment in the note → return appointments: []. Do not infer status from silence.""".format(
    evidence_guidance=EVIDENCE_QUOTE_GUIDANCE
)


def _user_prompt(note: Note) -> str:
    return (
        f"Note date: {note.note_date.isoformat()}\n"
        f"---\n"
        f"{note.body}"
    )


# --- Extractor ---------------------------------------------------


class AppointmentExtractor:
    """LLM-backed appointment extractor."""

    event_type: str = "appointment"

    def __init__(self, llm: StructuredLLM) -> None:
        self._llm = llm

    def can_handle(self, note: Note) -> bool:
        # DD-019: permissive prefilter. Per-note extraction is a
        # candidate generator; the per-claim reconciliation LLM is
        # responsible for filtering noise. Route every note that
        # plausibly mentions an appointment.
        return _passes_prefilter(note.body)

    def extract(self, note: Note) -> list[Event]:
        try:
            response = self._llm.structured(
                system=_SYSTEM_PROMPT,
                user=_user_prompt(note),
                response_model=AppointmentExtractionResponse,
            )
        except LLMError:
            return []

        events: list[Event] = []
        for appt in response.appointments:
            if appt.appointment_date is None:
                # Dateless candidates can't land in Q2 / Q4 (both need
                # a date) and can't cluster in reconciliation (dateless
                # events stay singletons). Drop at the source.
                continue
            if not quote_in_body(appt.evidence_quote, note.body):
                continue  # safety net — drop hallucinated quotes
            events.append(self._to_event(note, appt))
        return events

    def _to_event(
        self, note: Note, appt: _ExtractedAppointment
    ) -> Event:
        event_date = appt.appointment_date or note.note_date
        # Deduplicate parties at construction time — LLMs
        # occasionally repeat the same entity in different surface
        # forms within one entry. Preserve order for stability.
        # No party-in-quote check here under DD-019: per-note is a
        # permissive candidate generator; cross-attribution cleanup
        # lives in the per-claim reconciliation pass.
        seen: set[str] = set()
        parties: list[str] = []
        for p in appt.parties:
            stripped = p.strip()
            if not stripped or stripped.lower() in seen:
                continue
            seen.add(stripped.lower())
            parties.append(stripped)

        evidence = (
            EventEvidence(
                note_date=note.note_date, quote=appt.evidence_quote
            ),
        )
        if appt.status == "scheduled":
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                scheduled_notice_date=note.note_date,
                scheduled_for_date=appt.appointment_date,
                status="scheduled",
                evidence=evidence,
            )
        else:
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                occurred_on=appt.appointment_date,
                status=appt.status,
                evidence=evidence,
            )
        return Event(
            event_id=str(uuid.uuid4()),
            claim_id=note.claim_id,
            event_type="appointment",
            event_date=event_date,
            attributes=attrs,
            extraction_method="llm",
        )
