"""Top-level resolver: dispatches per-event-type, glues passes.

Under DD-019, appointments bypass the resolver — they are
reconciled in `claims.extractor.reconciliation` before reaching
this stage. The resolver handles:
- reserve_change: cross-note delta derivation, drop delta=0
- return_to_work / rtw_terminal: identity-merge / pass-through
"""

from __future__ import annotations

import logging

from claims.models import Event
from claims.resolver.reserve_change import resolve_reserve_changes
from claims.resolver.rtw import resolve_rtw

_log = logging.getLogger(__name__)


def resolve(events: list[Event]) -> list[Event]:
    by_type: dict[str, list[Event]] = {}
    for ev in events:
        by_type.setdefault(ev.event_type, []).append(ev)

    out: list[Event] = []
    for event_type, fn in (
        ("reserve_change", resolve_reserve_changes),
        ("return_to_work", resolve_rtw),
    ):
        incoming = by_type.get(event_type, [])
        outgoing = fn(incoming)
        if incoming or outgoing:
            _log.debug(
                "resolve %s: %d in -> %d out",
                event_type,
                len(incoming),
                len(outgoing),
            )
        out.extend(outgoing)
    # rtw_terminal currently passes through unchanged.
    terminals = by_type.get("rtw_terminal", [])
    if terminals:
        _log.debug("resolve rtw_terminal: %d passthrough", len(terminals))
    out.extend(terminals)
    # Defensive: an appointment slipping into resolve() is a bug
    # under DD-019. Log loudly rather than swallowing.
    stragglers = by_type.get("appointment", [])
    if stragglers:
        _log.warning(
            "resolve: %d appointment event(s) reached the resolver — "
            "should have gone through reconcile_appointments (DD-019)",
            len(stragglers),
        )
        out.extend(stragglers)
    return out
