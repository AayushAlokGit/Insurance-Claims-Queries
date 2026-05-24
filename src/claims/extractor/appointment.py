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

import logging
import re
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from claims.extractor._evidence import EVIDENCE_QUOTE_GUIDANCE
from claims.llm import StructuredLLM, quote_in_body
from claims.llm.base import LLMError
from claims.models import AppointmentAttributes, Event, Note

_log = logging.getLogger(__name__)


# Honorifics / degree suffixes the LLM occasionally leaves on a name.
# Stripped before the party-in-quote substring check so that "Dr.
# Farano" in the LLM's parties list still matches "Farano" in the
# quote (and vice versa).
_PARTY_STRIP_RE = re.compile(
    r"^(dr|mr|mrs|ms|atty)\.?\s+|,\s*(md|do|np|pa|rn|lpn|esq)\.?$",
    re.IGNORECASE,
)


def _normalize_party_for_check(p: str) -> str:
    """Strip honorifics / suffixes and lowercase for the in-quote
    substring check. Display strings keep their original casing."""
    s = p.strip()
    # apply twice to catch "Dr. Mr." or both-ends matches
    s = _PARTY_STRIP_RE.sub("", s)
    s = _PARTY_STRIP_RE.sub("", s)
    return s.strip().lower()


def _party_supported_by_quote(party: str, quote: str) -> bool:
    """Party is supported iff its normalized name (>= 2 chars after
    stripping honorifics/suffixes) appears as a case-insensitive
    substring of the evidence quote. Single-letter abbreviations
    ('F', 'C') and placeholders are rejected on length."""
    norm = _normalize_party_for_check(party)
    if len(norm) < 2:
        return False
    return norm in quote.lower()


# Relative-date markers: "yesterday", "today", "this morning",
# "tomorrow", "last <weekday>". Accepted as evidence of a specific
# date because the prompt instructs the LLM to resolve them from
# the note's date. We trust that resolution.
_REL_DATE_RE = re.compile(
    r"\b(yesterday|today|tonight|this (?:morning|afternoon|evening)"
    r"|tomorrow|last\s+(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat|sun)"
    r"(?:day)?)\b",
    re.IGNORECASE,
)

_MONTH_NAMES_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_NAMES_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _date_surface_forms(d: date) -> list[str]:
    """Every reasonable surface form of `d` we might see in the
    note body: M/D, MM/DD, M-D-YY, M.D.YYYY, "August 11", "11
    August", etc. Quote check is case-insensitive substring on
    these forms."""
    m, day = d.month, d.day
    y2, y4 = d.year % 100, d.year
    forms: list[str] = []
    for sep in ("/", "-", "."):
        forms.append(f"{m}{sep}{day}")
        forms.append(f"{m:02d}{sep}{day:02d}")
        for y in (y2, y4):
            forms.append(f"{m}{sep}{day}{sep}{y}")
            forms.append(f"{m:02d}{sep}{day:02d}{sep}{y}")
    full = _MONTH_NAMES_FULL[m - 1]
    abbr = _MONTH_NAMES_ABBR[m - 1]
    for name in (full, abbr):
        forms.append(f"{name} {day}")
        forms.append(f"{day} {name}")
        forms.append(f"{day}{name}")  # "11August" — rare but cheap
    return forms


def _date_supported_by_quote(d: date | None, quote: str) -> bool:
    """The appointment_date `d` is supported iff (a) a relative
    marker resolvable from note_date appears in the quote, OR (b)
    some surface form of `d` appears in the quote. When `d` is
    None, there is no claim to verify — return True (the resolver
    will leave the event a singleton)."""
    if d is None:
        return True
    if _REL_DATE_RE.search(quote):
        return True
    lower = quote.lower()
    return any(f.lower() in lower for f in _date_surface_forms(d))

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
            if not _date_supported_by_quote(
                appt.appointment_date, appt.evidence_quote
            ):
                # The LLM claimed a date that doesn't live in its
                # own evidence quote — the date came from a
                # different sentence. Null the date out and let
                # the resolver keep it as a dateless singleton
                # rather than asserting a (date, status, party)
                # tuple we can't verify.
                _log.info(
                    "date-in-quote drop note=%s claimed_date=%s "
                    "status=%s quote=\"%s\"",
                    note.note_id,
                    appt.appointment_date,
                    appt.status,
                    appt.evidence_quote.replace("\n", " ")[:80],
                )
                appt = appt.model_copy(update={"appointment_date": None})
            events.append(self._to_event(note, appt))
        return events

    def _to_event(
        self, note: Note, appt: _ExtractedAppointment
    ) -> Event:
        event_date = appt.appointment_date or note.note_date
        # Deduplicate parties at construction time — LLMs
        # occasionally repeat the same entity in different surface
        # forms within one entry. Preserve order for stability.
        # Also enforce the PARTY-IN-QUOTE rule: any party whose
        # normalized name does not appear in evidence_quote is the
        # LLM cross-attributing from elsewhere in the note. Drop it
        # and log so the cross-attribution failure is auditable.
        seen: set[str] = set()
        parties: list[str] = []
        dropped: list[str] = []
        for p in appt.parties:
            stripped = p.strip()
            if not stripped or stripped.lower() in seen:
                continue
            seen.add(stripped.lower())
            if not _party_supported_by_quote(stripped, appt.evidence_quote):
                dropped.append(stripped)
                continue
            parties.append(stripped)
        if dropped:
            _log.info(
                "party-in-quote drop note=%s date=%s dropped=%s quote=\"%s\"",
                note.note_id,
                appt.appointment_date,
                dropped,
                appt.evidence_quote.replace("\n", " ")[:80],
            )

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
