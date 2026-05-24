"""Appointment resolution. See docs/resolver.md §3 + DD-016.

Two events describe the same encounter iff:
    same claim_id
    AND same encounter_date           (exact match, no ±N window)
    AND parties_overlap(a, b)         (≥1 shared party, OR either
                                       side has no identifiable
                                       parties)

`encounter_date = scheduled_for_date or occurred_on` — the stable
calendar slot for the appointment. The previous design used a
±N day window with `canonicalize_provider`; DD-016 explains why
both went away (window absorbed Marker mis-attributions and
produced cross-date false merges; canonicalization was string
heuristics doing entity resolution badly).

KNOWN FAILURE MODE (DD-016, intentional). One claimant sees two
DIFFERENT clinicians at the SAME facility on the SAME day:

    Note A: parties = ["Caldwell", "Spine & Neurology Group"]
    Note B: parties = ["Farano",   "Spine & Neurology Group"]
    encounter_date = 4/22 for both

The overlap on the shared facility ("Spine & Neurology Group")
satisfies `parties_overlap` and the two events MERGE into one,
even though they are clinically two separate encounters with two
different doctors. The merged event's `parties` list will contain
BOTH clinicians, so the collapse is auditable rather than silent
— but Q2 will undercount attended appointments by 1 and Q4 will
pair the wrong (schedule, seen) dates if both encounters had
scheduling events.

Why we accept this for now:
- Does not occur in either of the two sample claims.
- Pattern is rare outside hospital inpatient days (same-day multi-
  specialist visits at one practice).
- The Q2 / Q4 artifact (one undercount, both doctors still listed
  in `parties`) is inspectable, not silent.

If/when a corpus shows this pattern matters, the local fix is in
`parties.py::parties_overlap` — tighten the rule from "any shared
party" to "shared PERSON party", treating facilities as confirming
evidence only. Search for "DD-016 same-facility-same-day failure
mode" if you arrive at this comment because a real-world
encounter just collapsed.

Status precedence (DD-017, asymmetric):
    - Any `missed` or `cancelled` in the merge group → that wins,
      regardless of any `attended` / `scheduled` / `unknown` present.
      (Within negatives: missed > cancelled.)
    - Otherwise (all positives) → attended > scheduled > unknown.
    - Ties at the same effective tier are broken by the more
      recent `source_note_dates` entry (i.e. `max(source_note_dates)`).

This replaces the previous monotonic-up rank, which silently
upgraded `missed` → `attended` whenever both existed for the same
encounter — see DD-017 for the audited failure case and the
extraction-cost asymmetry that justifies the new shape.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from claims.models import AppointmentAttributes, AppointmentStatus, Event
from claims.resolver.parties import normalize_party, parties_overlap

_log = logging.getLogger(__name__)


def _ev_short(ev: Event) -> str:
    """Compact one-line identity for an event in resolver logs."""
    attrs = ev.attributes
    assert isinstance(attrs, AppointmentAttributes)
    enc = attrs.scheduled_for_date or attrs.occurred_on or ev.event_date
    parties = "|".join(attrs.parties) or "-"
    return f"{ev.event_id[:8]} date={enc} status={attrs.status} parties=[{parties}]"

# DD-017: two-tier status ranking, asymmetric.
_POSITIVE_RANK: dict[AppointmentStatus, int] = {
    "attended": 2,
    "scheduled": 1,
    "unknown": 0,
}
_NEGATIVE_RANK: dict[AppointmentStatus, int] = {
    "missed": 2,
    "cancelled": 1,
}
_NEGATIVE_STATUSES = frozenset(_NEGATIVE_RANK)


def _encounter_date(attrs: AppointmentAttributes) -> date | None:
    return attrs.scheduled_for_date or attrs.occurred_on


def _should_merge(a: Event, b: Event) -> bool:
    """DD-016 merge key."""
    if a.claim_id != b.claim_id:
        return False
    a_attrs = a.attributes
    b_attrs = b.attributes
    assert isinstance(a_attrs, AppointmentAttributes)
    assert isinstance(b_attrs, AppointmentAttributes)
    a_date = _encounter_date(a_attrs)
    b_date = _encounter_date(b_attrs)
    if a_date is None or b_date is None:
        # No date on at least one side — can't anchor the merge.
        # Fall back to party overlap only (rare; mostly the LLM
        # extractor emitting an undated past visit).
        overlap = parties_overlap(a_attrs.parties, b_attrs.parties)
        if not overlap:
            _log.debug(
                "no-merge reason=undated-no-party-overlap a=(%s) b=(%s)",
                _ev_short(a),
                _ev_short(b),
            )
        return overlap
    if a_date != b_date:
        if parties_overlap(a_attrs.parties, b_attrs.parties):
            # High-signal case: same parties, different dates. Often a
            # note-write-date vs DOS off-by-one (e.g. 4/22 Caldwell vs
            # 4/23 OHC, 6/26 Harmon vs 6/27 Harmon). Logged so we can
            # see it without flipping to DEBUG.
            _log.info(
                "no-merge reason=date-mismatch-but-parties-overlap "
                "a=(%s) b=(%s)",
                _ev_short(a),
                _ev_short(b),
            )
        return False
    overlap = parties_overlap(a_attrs.parties, b_attrs.parties)
    if not overlap:
        _log.debug(
            "no-merge reason=same-date-no-party-overlap a=(%s) b=(%s)",
            _ev_short(a),
            _ev_short(b),
        )
    return overlap


_DATE_MIN = date.min  # tiebreaker default for events with no source dates


def _latest_note_date(ev: Event) -> date:
    attrs = ev.attributes
    assert isinstance(attrs, AppointmentAttributes)
    return max(attrs.source_note_dates) if attrs.source_note_dates else _DATE_MIN


def _resolve_status(events: list[Event]) -> AppointmentStatus:
    """DD-017 status resolver. Negatives beat positives; within a
    tier, max rank wins with the latest `source_note_dates` entry
    as the tiebreaker.

    `events` is the full merge group — `_should_merge` already
    confirmed they describe the same encounter."""
    negatives = [
        e
        for e in events
        if e.attributes.status in _NEGATIVE_STATUSES  # type: ignore[union-attr]
    ]
    if negatives:
        # Any negative beats any positive. Among negatives: max
        # rank (missed > cancelled), recency as tiebreaker.
        chosen = max(
            negatives,
            key=lambda e: (
                _NEGATIVE_RANK[e.attributes.status],  # type: ignore[index]
                _latest_note_date(e),
            ),
        )
        return chosen.attributes.status  # type: ignore[return-value]
    # No negatives — fall back to positive rank with recency tiebreak.
    chosen = max(
        events,
        key=lambda e: (
            _POSITIVE_RANK.get(e.attributes.status, 0),  # type: ignore[arg-type]
            _latest_note_date(e),
        ),
    )
    return chosen.attributes.status  # type: ignore[return-value]


def _merge_parties(events: list[Event]) -> tuple[str, ...]:
    """Union of all parties across the merged events, deduped by
    normalized form, preserving first-seen order so output is
    stable. The longest surface form wins as the canonical display
    string for each normalized key (so `"Dr. Harmon's office"`
    beats bare `"Harmon"` for display purposes — same normalized
    identity, more informative label)."""
    by_norm: dict[str, str] = {}
    order: list[str] = []
    for ev in events:
        attrs = ev.attributes
        assert isinstance(attrs, AppointmentAttributes)
        for p in attrs.parties:
            n = normalize_party(p)
            if n is None:
                continue
            existing = by_norm.get(n)
            if existing is None:
                by_norm[n] = p
                order.append(n)
            elif len(p) > len(existing):
                by_norm[n] = p
    return tuple(by_norm[n] for n in order)


def _merge(events: list[Event]) -> Event:
    """Combine events that all describe the same appointment.
    Status follows DD-017 (asymmetric + recency); date fields
    union (first non-null); parties union (longest surface form
    per normalized identity); scheduled_notice_date takes the
    latest (reschedule semantics — resolver.md §5)."""
    first = events[0]
    base_attrs = first.attributes
    assert isinstance(base_attrs, AppointmentAttributes)

    occurred_on = base_attrs.occurred_on
    scheduled_for_date = base_attrs.scheduled_for_date
    scheduled_notice_date = base_attrs.scheduled_notice_date
    specialty = base_attrs.specialty
    appointment_type = base_attrs.appointment_type
    # Union of every contributing note date across the group, deduped
    # and sorted ascending. Single-source events keep their one entry;
    # merged events get the full audit trail. DD-017's tiebreaker uses
    # max(...) when comparing groups.
    all_note_dates: set[date] = set(base_attrs.source_note_dates)

    for ev in events[1:]:
        attrs = ev.attributes
        assert isinstance(attrs, AppointmentAttributes)
        occurred_on = occurred_on or attrs.occurred_on
        scheduled_for_date = (
            scheduled_for_date or attrs.scheduled_for_date
        )
        if attrs.scheduled_notice_date is not None and (
            scheduled_notice_date is None
            or attrs.scheduled_notice_date > scheduled_notice_date
        ):
            scheduled_notice_date = attrs.scheduled_notice_date
        specialty = specialty or attrs.specialty
        appointment_type = appointment_type or attrs.appointment_type
        all_note_dates.update(attrs.source_note_dates)

    status = _resolve_status(events)

    seen_quotes: set[str] = set()
    ordered_quotes: list[str] = []
    for ev in events:
        q = ev.attributes.evidence_quote  # type: ignore[union-attr]
        if q and q not in seen_quotes:
            seen_quotes.add(q)
            ordered_quotes.append(q)
    merged_quote = " ||| ".join(ordered_quotes) if ordered_quotes else None

    merged_attrs = AppointmentAttributes(
        parties=_merge_parties(events),
        specialty=specialty,
        scheduled_notice_date=scheduled_notice_date,
        scheduled_for_date=scheduled_for_date,
        occurred_on=occurred_on,
        status=status,
        appointment_type=appointment_type,
        source_note_dates=tuple(sorted(all_note_dates)),
        evidence_quote=merged_quote,
    )
    event_date = (
        merged_attrs.occurred_on
        or merged_attrs.scheduled_for_date
        or first.event_date
    )

    return Event(
        event_id=str(uuid.uuid4()) if len(events) > 1 else first.event_id,
        claim_id=first.claim_id,
        event_type="appointment",
        event_date=event_date,
        attributes=merged_attrs,
        extraction_method=(
            "merged" if len(events) > 1 else first.extraction_method
        ),
    )


def resolve_appointments(events: list[Event]) -> list[Event]:
    """Group appointment events by the DD-016 merge key and
    merge each group. Quadratic in per-claim group size — fine for
    the corpus scale we care about. Per-claim batches keep the
    inner loop small.
    """
    by_claim: dict[str, list[Event]] = {}
    for ev in events:
        if not isinstance(ev.attributes, AppointmentAttributes):
            continue
        by_claim.setdefault(ev.claim_id, []).append(ev)

    out: list[Event] = []
    for claim_events in by_claim.values():
        # Sort by encounter_date so the earliest seeds each group
        # — gives stable output order across runs.
        claim_events.sort(
            key=lambda e: (
                _encounter_date(e.attributes)  # type: ignore[arg-type]
                or e.event_date,
                e.event_id,
            )
        )
        clusters: list[list[Event]] = []
        for ev in claim_events:
            placed = False
            for cluster in clusters:
                if _should_merge(cluster[0], ev):
                    cluster.append(ev)
                    placed = True
                    break
            if not placed:
                clusters.append([ev])
        for cluster in clusters:
            merged = _merge(cluster)
            if len(cluster) > 1:
                merged_attrs = merged.attributes
                assert isinstance(merged_attrs, AppointmentAttributes)
                statuses = [
                    e.attributes.status  # type: ignore[union-attr]
                    for e in cluster
                ]
                _log.info(
                    "merge cluster_size=%d chosen_status=%s members=[%s] "
                    "input_statuses=%s",
                    len(cluster),
                    merged_attrs.status,
                    "; ".join(_ev_short(e) for e in cluster),
                    statuses,
                )
            else:
                _log.debug(
                    "singleton kept (%s)",
                    _ev_short(cluster[0]),
                )
            out.append(merged)
    return out
