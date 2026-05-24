"""Per-claim appointment reconciliation (DD-019).

One LLM call per claim. Input: every appointment candidate that
came out of the per-note extractor (the LLM appointment extractor
running in permissive mode — no PARTY-IN-QUOTE / DATE-IN-QUOTE /
DD-018 route gate). Output: the canonical appointment list.

The candidate-set has lots of duplicates, cross-attribution noise,
and dateless events. The reconciliation LLM sees the whole set at
once — that's the architectural premise: cross-note attribution
needs cross-note context, which per-note extraction cannot have.

Replaces the DD-016 / DD-017 resolver-side merge for appointments
when `--recon` is on. Other event types (reserve_change,
return_to_work, rtw_terminal) still go through the resolver
unchanged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from claims.llm import StructuredLLM
from claims.llm.base import LLMError
from claims.models import (
    AppointmentAttributes,
    AppointmentStatus,
    AppointmentType,
    Event,
)

_log = logging.getLogger(__name__)


# --- I/O schemas -------------------------------------------------


class _CandidateIn(BaseModel):
    """Compact serialization of one per-note candidate sent to the
    reconciliation LLM. Identifies the candidate by index in the
    request so the LLM can cite contributing candidates by id."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    note_id: str
    note_date: date
    status: AppointmentStatus
    appointment_date: date | None
    scheduled_for_date: date | None
    occurred_on: date | None
    parties: list[str]
    evidence_quote: str | None


class _CanonicalAppointment(BaseModel):
    """One canonical appointment in the reconciled list."""

    model_config = ConfigDict(extra="forbid")

    status: AppointmentStatus
    scheduled_notice_date: date | None = Field(
        default=None,
        description=(
            "Earliest note_date among contributing candidates that "
            "described this appointment as forward-looking "
            "(scheduled). Null if no candidate provided a "
            "scheduling notice."
        ),
    )
    scheduled_for_date: date | None = None
    occurred_on: date | None = None
    parties: list[str] = Field(default_factory=list)
    appointment_type: AppointmentType | None = None
    evidence_quote: str = Field(
        description=(
            "One verbatim evidence quote from a contributing "
            "candidate — pick the strongest / most specific one."
        ),
    )
    contributing_candidate_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Candidate IDs (from the request) that the LLM decided "
            "describe THIS canonical appointment."
        ),
    )


class ReconciledAppointmentList(BaseModel):
    """Discriminated-union empty case: `appointments: []` is the
    canonical 'no appointments' response."""

    model_config = ConfigDict(extra="forbid")

    appointments: list[_CanonicalAppointment] = Field(
        default_factory=list
    )


# --- Prompt ------------------------------------------------------

_SYSTEM_PROMPT = """You are reconciling APPOINTMENT candidates extracted from a single workers'-comp claim's notes.

You will be given a list of candidate appointments. Each candidate was extracted from one note by a permissive per-note extractor; the candidate set contains DUPLICATES (the same real visit referenced by multiple notes), CROSS-ATTRIBUTION ERRORS (the LLM paired a date with the wrong doctor), and NOISE (retrospective recaps generated stale candidates).

Your job is to produce the CANONICAL appointment list: one entry per real clinical encounter.

RULES:

1. CLUSTER candidates that describe the same real visit. Two candidates describe the same visit when they share an encounter date (or near-date with same party) and a party. Same-day same-facility different-clinician visits are TWO appointments, not one.

2. CHOOSE STATUS for each canonical appointment using ASYMMETRIC PRECEDENCE:
   - Any candidate with status `missed` or `cancelled` → that wins, regardless of any `attended` / `scheduled` evidence in the cluster.
   - Within negatives: `missed` beats `cancelled`.
   - All-positive clusters: `attended` beats `scheduled` beats `unknown`.
   - Tiebreaker: more recent note_date wins.

3. POPULATE DATE FIELDS:
   - `scheduled_for_date` / `occurred_on`: take the most specific known date for the visit. If the visit happened, set `occurred_on`. If only future scheduling is known, set `scheduled_for_date`.
   - `scheduled_notice_date`: the EARLIEST note_date among contributing candidates that referenced this visit as forward-looking (status=scheduled or appointment_date in the future relative to note_date). Null if no candidate provided a scheduling notice.

4. POPULATE PARTIES: union the named individuals and organizations from contributing candidates. Drop generic labels ("the doctor", "FCM", "Patient A", "the IE", "the claimant", specialty names like "Ortho" or "Spine"). Strip honorifics and degree suffixes — "Dr. Harmon" and "Harmon" are the same party.

5. EVIDENCE QUOTE: pick ONE verbatim quote from a contributing candidate that best justifies the canonical entry. Prefer quotes that name BOTH the date and at least one party.

6. CITE candidate IDs that contribute to each canonical entry in `contributing_candidate_ids`.

DO NOT EMIT:
- Pure scheduling-with-no-occurrence rows where the appointment_date has already passed (note_date > appointment_date) AND no candidate said attended/missed/cancelled. The visit was likely missed-without-record or rescheduled; emit only what evidence supports.
- "Visits" that are really hospital inpatient days (e.g. consecutive daily evals during an admission). Emit one entry per outpatient encounter; admissions are a separate concept.
- Duplicate entries for the same encounter.

If no real appointment is in the candidate set, return appointments: [].
"""


def _user_prompt(
    claim_id: str,
    date_of_loss: date | None,
    candidates: list[_CandidateIn],
) -> str:
    lines = [
        f"Claim: {claim_id}",
        f"Date of loss: {date_of_loss.isoformat() if date_of_loss else 'unknown'}",
        f"Candidate count: {len(candidates)}",
        "",
        "Candidates (one per line, JSON):",
    ]
    for c in candidates:
        lines.append(c.model_dump_json())
    return "\n".join(lines)


# --- Reconciliation entry point ----------------------------------


def _to_candidate_in(idx: int, ev: Event) -> _CandidateIn:
    """Project a per-note appointment Event into the compact
    candidate shape the recon LLM expects."""
    attrs = ev.attributes
    assert isinstance(attrs, AppointmentAttributes)
    # `note_date` is the earliest source_note_dates entry — per-note
    # candidates have exactly one entry, so this is unambiguous.
    note_date = (
        attrs.source_note_dates[0]
        if attrs.source_note_dates
        else ev.event_date
    )
    return _CandidateIn(
        candidate_id=idx,
        note_id=_extract_note_id_hint(ev),
        note_date=note_date,
        status=attrs.status,
        appointment_date=(
            attrs.scheduled_for_date or attrs.occurred_on
        ),
        scheduled_for_date=attrs.scheduled_for_date,
        occurred_on=attrs.occurred_on,
        parties=list(attrs.parties),
        evidence_quote=attrs.evidence_quote,
    )


def _extract_note_id_hint(ev: Event) -> str:
    """Per-note candidates don't carry a note_id in the Event
    model — use the event_id (per-note candidates have unique
    event_ids) so the LLM has SOMETHING to cite if it needs to."""
    return ev.event_id[:8]


def reconcile_appointments(
    claim_id: str,
    date_of_loss: date | None,
    candidates: list[Event],
    llm: StructuredLLM,
) -> list[Event]:
    """Reconcile a flat list of per-note appointment candidate Events
    into the canonical appointment list. Returns Events suitable for
    direct insertion (no further resolver pass)."""
    if not candidates:
        _log.info(
            "reconcile claim=%s candidates=0 -> 0 (skipped)", claim_id
        )
        return []
    candidate_ins = [
        _to_candidate_in(i, ev) for i, ev in enumerate(candidates)
    ]
    _log.info(
        "reconcile claim=%s candidates=%d",
        claim_id,
        len(candidate_ins),
    )
    try:
        response = llm.structured(
            system=_SYSTEM_PROMPT,
            user=_user_prompt(claim_id, date_of_loss, candidate_ins),
            response_model=ReconciledAppointmentList,
        )
    except LLMError as exc:
        _log.warning(
            "reconcile claim=%s failed: %s -- falling back to "
            "raw candidates",
            claim_id,
            exc,
        )
        # If reconciliation fails, return the raw candidates so the
        # downstream pipeline doesn't lose the data. The user sees
        # duplicates; they don't see a silent zero.
        return candidates

    contributed: set[int] = set()
    out: list[Event] = []
    for appt in response.appointments:
        # Collect contributing candidates' source_note_dates so
        # the audit trail remains visible in the final event.
        contrib_dates: set[date] = set()
        valid_cids: list[int] = []
        for cid in appt.contributing_candidate_ids:
            if 0 <= cid < len(candidates):
                valid_cids.append(cid)
                contributed.add(cid)
                src = candidates[cid].attributes
                if isinstance(src, AppointmentAttributes):
                    contrib_dates.update(src.source_note_dates)
        attrs = AppointmentAttributes(
            parties=tuple(appt.parties),
            specialty=None,
            scheduled_notice_date=appt.scheduled_notice_date,
            scheduled_for_date=appt.scheduled_for_date,
            occurred_on=appt.occurred_on,
            status=appt.status,
            appointment_type=appt.appointment_type,
            source_note_dates=tuple(sorted(contrib_dates)),
            evidence_quote=appt.evidence_quote,
        )
        event_date = (
            attrs.occurred_on
            or attrs.scheduled_for_date
            or (
                candidates[valid_cids[0]].event_date
                if valid_cids
                else date.today()
            )
        )
        out.append(
            Event(
                event_id=str(uuid.uuid4()),
                claim_id=claim_id,
                event_type="appointment",
                event_date=event_date,
                attributes=attrs,
                extraction_method="merged",
            )
        )
        # Per-canonical-appointment INFO line. Mirrors the
        # extractor's `extract note=...` shape so the same grep
        # ("appointment date=") picks up both the candidate set
        # and the reconciled survivors.
        enc = attrs.occurred_on or attrs.scheduled_for_date or event_date
        kind = (
            "occurred"
            if attrs.occurred_on
            else "scheduled"
            if attrs.scheduled_for_date
            else "?"
        )
        quote = (attrs.evidence_quote or "").replace("\n", " ")[:80]
        _log.info(
            "recon claim=%s :: appointment date=%s kind=%s status=%s "
            "parties=[%s] notice=%s contrib=%s evidence=\"%s\"",
            claim_id,
            enc,
            kind,
            attrs.status,
            "|".join(attrs.parties) or "-",
            attrs.scheduled_notice_date or "-",
            valid_cids,
            quote,
        )

    # Audit any candidates the LLM dropped — useful when the
    # reconciliation output is missing something we expected.
    dropped = sorted(
        set(range(len(candidates))) - contributed
    )
    if dropped:
        _log.info(
            "recon claim=%s dropped %d candidate(s) (no contribution): %s",
            claim_id,
            len(dropped),
            dropped[:20] + (["..."] if len(dropped) > 20 else []),
        )

    _log.info(
        "reconcile claim=%s candidates=%d -> canonical=%d",
        claim_id,
        len(candidate_ins),
        len(out),
    )
    return out
