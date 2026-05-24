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
from claims.models import AppointmentAttributes, Event, Note


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


# --- Prompts -----------------------------------------------------
#
# KNOWN FAILURE MODE — cross-appointment date/party mis-pairing.
# When a single note discusses multiple appointments without
# clean per-line attribution, the LLM tends to pair a doctor
# with the nearest date in the note even when they refer to
# different appointments. The DATE↔PARTY PAIRING and NEGATIVE
# EVENTS rules below mitigate this by demanding same-sentence
# proximity; the architectural fix (a second-pass reconciliation
# extractor that sees the whole claim) is sketched in DD-017
# "Known limitations" but not built — the cost is too high for a
# ~1-row precision dip on Q2 across the sample claims.
# Trade-off: under-emission is recoverable downstream; over-
# emission propagates as wrong rows.

_SYSTEM_PROMPT = """You extract APPOINTMENT events from a workers'-comp claim note. An appointment is a clinical encounter (or planned encounter) between the claimant and a healthcare provider. Judge by the body content, not by administrative wrapper lines.

STATUS:
- attended  — the visit happened. A clinical visit summary (date + provider + diagnosis/plan content) counts as attended even without an explicit "attended" verb.
- missed    — claimant did not attend / no-show / DNA / "unable to attend".
- cancelled — cancelled or rescheduled before it could happen.
- scheduled — a future appointment with a date.

If one note recaps multiple historical visits, emit one entry per appointment.

DO NOT EXTRACT:
- Paperwork / communications with no visit summary attached (emails, texts, calls, faxes, signed forms, records receipts).
- Orders or referrals not yet performed ("MRI ordered", "referral placed").
- Instructions or questions ("patient to follow up around DATE", "checking whether she attended").

{evidence_guidance}

DATE: an explicit date in the body → `appointment_date`. A relative phrase that names a specific day relative to the note date ("yesterday", "this morning", "last Friday") → compute from the note date. Vague phrases ("soon", "next month") → null.

DATE-IN-QUOTE RULE (hard requirement): The `appointment_date` you set MUST be derivable from the `evidence_quote` you choose. Either the date appears literally in the quote (in any common form: "8/11", "08/11/25", "August 11", "11-Aug-2025") OR a relative marker that resolves to it appears ("yesterday", "today", "last Friday"). If the date in the body and the status/party context are in different sentences, you must EITHER (a) expand your quote to include both, OR (b) emit the event with `appointment_date: null`. Do not pair a date that lives outside your quote.

DATE↔PARTY PAIRING: when a note mentions multiple appointments, do NOT reach across sentences to pair a date with a doctor. The date and the doctor must appear in the SAME context or clause for you to pair them. Otherwise leave `appointment_date` null or skip the event.

NEGATIVE EVENTS (missed / cancelled) are STRICT: only emit when the note explicitly names BOTH the doctor/clinic AND the date in unambiguous proximity (same sentence preferred). If either is uncertain, omit. Over-emitting a missed/cancelled event corrupts status resolution downstream; under-emitting is recoverable.

PARTIES — list every named individual and organization party to THIS specific appointment:
- The attending clinician(s) — must be a named person, not a role.
- The facility / clinic / hospital / practice where it occurs — must be a named org, not a generic word.
- A case manager, field nurse, or interpreter PHYSICALLY PRESENT at the encounter, ONLY if personally named in the note.

PARTY-IN-QUOTE RULE (hard requirement): Every party you list MUST appear by name inside the `evidence_quote` you choose for that same appointment. If your evidence_quote does not contain the party's name, do not list that party. Choose a longer or different verbatim substring of the note that includes BOTH the date (or a clear date referent like "yesterday") AND every party — or omit the party / event.

EXAMPLE of joint-reference failure (do NOT do this):
> Body: "Dr. Farano wanted to see her. Unfortunately, she was unable to attend her appointments scheduled for 08/11 and 08/13."
> Wrong: emit two events tagging 08/11 with Farano and 08/13 with Farano, status=missed.
> Wrong because: the 08/11 and 08/13 sentence does not name a doctor; Dr. Farano is in a different sentence and may belong to one, both, or neither date.
> Correct: either (a) emit two events with parties: [] (date known, party not attributable in the same clause), or (b) emit nothing for the dates whose provider is ambiguous.

DO NOT put in `parties`:
- The claimant under any label — pronouns ("she", "he"), role labels ("claimant", "patient", "EE", "IE", "IW", "HR"), or pseudonyms ("Patient A", "the IW", "Subject").
- Referring providers named only in history / treatment plan / referrals.
- Other clinicians mentioned only in the diagnosis line.
- Specialty names ("ophthalmology", "neurology"). Specialty is not a party.
- Generic phrases ("office", "the doctor", "the field nurse").
- Unnamed roles ("FCM", "TCM", "field nurse", "case manager", "interpreter") on their own.
- Single-letter abbreviations or placeholders ("F", "C", "Dr. F", "Dr. _").

Each entry names ONE entity. Strip honorifics ("Dr.", "Mr."), degree suffixes (", MD", ", DO"), and location suffixes ("'s office", "at <clinic>"). A doctor and the clinic they work at are TWO entries, not one. Emit an empty list if no identifiable party is named — never invent one.

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

        if appt.status == "scheduled":
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                scheduled_notice_date=note.note_date,
                scheduled_for_date=appt.appointment_date,
                status="scheduled",
                source_note_dates=(note.note_date,),
                evidence_quote=appt.evidence_quote,
            )
        else:
            attrs = AppointmentAttributes(
                parties=tuple(parties),
                occurred_on=appt.appointment_date,
                status=appt.status,
                source_note_dates=(note.note_date,),
                evidence_quote=appt.evidence_quote,
            )
        return Event(
            event_id=str(uuid.uuid4()),
            claim_id=note.claim_id,
            event_type="appointment",
            event_date=event_date,
            attributes=attrs,
            extraction_method="llm",
        )
