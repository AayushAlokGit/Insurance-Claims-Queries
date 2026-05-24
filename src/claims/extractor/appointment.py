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

from claims.llm import StructuredLLM, quote_in_body
from claims.llm.base import LLMError
from claims.models import AppointmentAttributes, Event, Note

# --- Prefilter ---------------------------------------------------

# Status-verb branch. Catches the attended/missed/cancelled
# cases regardless of header style.
_RE_STATUS_VERBS = re.compile(
    r"\b(attended|missed|no[- ]show|did not show|DNA"
    r"|cancell?ed|seen by|EE attended)\b",
    re.IGNORECASE,
)
# `saw <provider> on <date>` is its own narrative-style trigger.
_RE_SAW_ON = re.compile(r"\bsaw\b.{0,40}\bon\b", re.IGNORECASE)

# Date-and-context branch. Catches scheduling-only events that
# use a non-templated header (no marker, no status verb).
_RE_APPT_CONTEXT = re.compile(
    r"\b(appointment|visit|follow[- ]?up|scheduled"
    r"|seen|consult|exam|evaluation|referral)\b",
    re.IGNORECASE,
)
# A cheap date-pattern check: numeric MDY or month name.
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


# --- Prompts -----------------------------------------------------

_SYSTEM_PROMPT = """You extract APPOINTMENT events from a workers'-comp claim note. An appointment is a clinical encounter (or planned encounter) between the claimant and a healthcare provider. Judge by what the body says, not by any administrative header or wrapper line at the top of the note.

STATUS — pick the strongest evidence:
- attended  — the visit happened. Includes explicit statements ("attended …", "saw/seen by Dr. X on …"), passive forms ("was attended on …"), and consult/procedure language that names a result. A clinical visit summary (date + provider + diagnosis/plan content) implies the visit happened — emit it as attended even if the surrounding paragraph opens with an administrative line.
- missed    — claimant did not attend / no-show / DNA / "unable to attend".
- cancelled — cancelled or rescheduled before it could happen.
- scheduled — a future appointment with a date.

EXTRACT EACH appointment separately. If one note recaps multiple historical visits, emit one entry per appointment.

DO NOT EXTRACT:
- Pure communications/paperwork: emails, texts, phone calls, faxes, signed forms, records receipts with no visit summary attached.
- Orders/referrals not yet performed ("referral placed", "MRI ordered").
- Instructions or questions ("patient to follow up around DATE", "checking whether she attended").

EVIDENCE_QUOTE is REQUIRED on every entry — a verbatim substring of the body. If you can't find one, don't emit the entry.

DATE: explicit date in the body → `appointment_date`. Definite relative phrase that names a specific day relative to the note date ("yesterday", "today", "this morning", "last Friday") → compute the date from the note date and return it. Vague relative phrase ("recently", "soon", "next month", "in a few weeks") → leave null.

PARTIES — list every named individual and organization that is party to THIS specific appointment:
- The attending clinician(s) who actually see (or would see) the claimant at this encounter — must be a named person (e.g. "Dr. Harmon"), not a role.
- The facility / clinic / hospital / practice where the appointment occurs — must be a named org (e.g. "Spine & Neurology Group"), not a generic word.
- A case manager, field nurse, or interpreter PHYSICALLY PRESENT at the encounter, ONLY if they have a personal name in the note (e.g. "FCM T.W.", "interpreter Maria Lopez"). Never emit generic role labels like "FCM", "field nurse", "TCM", "interpreter", "case manager" on their own.

NOTE: The claimant is never a party to their own appointment for our purposes.

DO NOT put in `parties`:
- The claimant / injured worker themselves, under any label — including pronouns ("she", "he"), role labels ("claimant", "patient", "EE", "IE", "IW", "HR"), AND pseudonymous proper names used to refer to the claimant ("Patient A", "Patient B", "the IE", "the IW", "Subject", "the subject"). 
- Referring physicians or providers named only in history / treatment plan / referrals.
- Other clinicians mentioned only in the diagnosis line.
- Specialty names ("ophthalmology", "spine surgery", "neurology"). Specialty is not a party.
- Generic phrases ("office", "clinic", "my office", "the doctor", "the provider", "the field nurse", "the FCM").
- Unnamed roles: "FCM", "TCM", "field nurse", "case manager", "interpreter" when the note does not give a personal name.

Each entry in `parties` names ONE entity. Strip honorifics (Dr., Mr., Mrs.), degree suffixes (", MD", ", DO", ", PhD"), and location suffixes ("'s office", "at <clinic>"). For example: "Dr. Harmon's office" → emit "Harmon". "Dr. Caldwell at Spine & Neurology Group" → emit two entries: "Caldwell" and "Spine & Neurology Group". "Valley PT Group" → emit "Valley PT Group". A note that reads "I traveled to Dr. Vega's office today with Patient A and our field nurse" → emit just "Vega" (claimant excluded; unnamed field nurse excluded). If no identifiable party is named, emit an empty list — never invent one.

No appointment in the note → return appointments: []. Do not infer status from silence."""


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
        seen: set[str] = set()
        parties: list[str] = []
        for p in appt.parties:
            stripped = p.strip()
            if not stripped or stripped.lower() in seen:
                continue
            seen.add(stripped.lower())
            parties.append(stripped)

        if appt.status == "scheduled":
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                scheduled_notice_date=note.note_date,
                scheduled_for_date=appt.appointment_date,
                status="scheduled",
                source_note_date=note.note_date,
            )
        else:
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                occurred_on=appt.appointment_date,
                status=appt.status,
                source_note_date=note.note_date,
            )
        return Event(
            event_id=str(uuid.uuid4()),
            claim_id=note.claim_id,
            event_type="appointment",
            event_date=event_date,
            attributes=attrs,
            extraction_method="llm",
        )
