"""Per-claim appointment reconciliation (DD-019).

Three stages:

1. **Pre-cluster (rules).** Group per-note candidates by anchor
   date (±1 day) AND party overlap (with empty-party absorption
   on one side only). Dateless candidates stay singletons.
2. **Per-cluster LLM call.** One small focused call per cluster
   that picks status (DD-017 precedence) and cleans parties.
3. **Date fields + evidence (rules).** Encounter date = mode of
   the contributing candidates' dates. `scheduled_notice_date` =
   earliest contributing `note_date` not later than the encounter
   (retrospective recaps are excluded by construction). Evidence
   pairs (note_date, quote) are unioned across contributors — no
   single quote is privileged.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from claims.llm import StructuredLLM
from claims.llm.base import LLMError
from claims.models import (
    AppointmentAttributes,
    AppointmentEvidence,
    AppointmentStatus,
    Event,
)

_log = logging.getLogger(__name__)


# --- Party normalization ----------------------------------------

_PARTY_STRIP_RE = re.compile(
    r"^(?:dr|mr|mrs|ms|prof|rev|sr|jr)\.?\s+"
    r"|,?\s*(?:MD|DO|NP|PA|PT|DPT|OT|DC|PhD|DDS|RN|LCSW)\.?$"
    r"|[.,]",
    re.IGNORECASE,
)

# Generic / role / claimant labels that are never a party.
_GENERIC = frozenset(
    {
        "claimant", "patient", "patient a", "the patient",
        "the claimant", "ee", "ie", "iw", "hr", "fcm", "tcm",
        "field nurse", "case manager", "interpreter", "the doctor",
        "doctor", "the office", "office", "subject",
        "ortho", "spine", "pain mgmt", "pain management",
        "neurology", "ophthalmology", "physical therapy",
        "occupational health",
    }
)


def _normalize_party(raw: str | None) -> str | None:
    """Lowercase, strip honorifics / suffixes / punctuation, collapse
    whitespace. Returns None for empties and generic labels."""
    if not raw:
        return None
    s = _PARTY_STRIP_RE.sub("", raw).strip().lower()
    s = re.sub(r"\s+", " ", s).replace("&", "and")
    return None if not s or s in _GENERIC else s


def _norm_set(parties: tuple[str, ...]) -> set[str]:
    return {n for p in parties if (n := _normalize_party(p))}


# --- Stage 1: pre-cluster ---------------------------------------


def _anchor(ev: Event) -> date | None:
    a = ev.attributes
    assert isinstance(a, AppointmentAttributes)
    return a.scheduled_for_date or a.occurred_on


_DATE_TOLERANCE_DAYS = 3


def _can_join(seed: Event, cand: Event) -> bool:
    """Join the cluster if anchor dates are within
    `_DATE_TOLERANCE_DAYS` AND parties overlap (or one side has no
    named parties — empty candidates absorb into the matching dated
    cluster)."""
    s_date, c_date = _anchor(seed), _anchor(cand)
    if s_date is None or c_date is None:
        return False
    if abs((s_date - c_date).days) > _DATE_TOLERANCE_DAYS:
        return False
    s_parties = _norm_set(seed.attributes.parties)  # type: ignore[union-attr]
    c_parties = _norm_set(cand.attributes.parties)  # type: ignore[union-attr]
    return not s_parties or not c_parties or bool(s_parties & c_parties)


def _precluster(candidates: list[Event]) -> list[list[Event]]:
    """Greedy cluster: each new candidate joins the first existing
    cluster whose seed it matches; otherwise starts a new cluster.
    Sorted by anchor date so the earliest mention seeds — stable."""
    sorted_cands = sorted(
        candidates,
        key=lambda e: (_anchor(e) or date.max, e.event_id),
    )
    clusters: list[list[Event]] = []
    for cand in sorted_cands:
        for cluster in clusters:
            if _can_join(cluster[0], cand):
                cluster.append(cand)
                break
        else:
            clusters.append([cand])
    return clusters


# --- Stage 2: per-cluster LLM call ------------------------------


class _ClusterResolution(BaseModel):
    """LLM output: status + cleaned parties. Evidence is assembled
    deterministically downstream by unioning contributing pairs."""

    model_config = ConfigDict(extra="forbid")

    status: AppointmentStatus
    parties: list[str] = Field(default_factory=list)


_SYSTEM_PROMPT = """You are resolving one cluster of appointment candidates from a workers'-comp claim. Every candidate describes the SAME real encounter — clustering is already done.

1. STATUS (asymmetric precedence). If any candidate is `missed` or `cancelled`, that wins (missed > cancelled). Otherwise: `attended` > `scheduled` > `unknown`. Tiebreak by latest note_date.

2. PARTIES. Union the named parties across candidates. Strip honorifics ("Dr.", "Mr.") and degree suffixes. Treat "Dr. Harmon" and "Harmon" as one party. DROP claimant labels (claimant, patient, Patient A, EE, IE, IW), unnamed roles (FCM, TCM, the doctor, interpreter), and specialty words used as names (Ortho, Spine, Pain Mgmt, Neurology). Empty list if no named party survives.

You are NOT responsible for clustering, date fields, notice date, or evidence quotes — the caller handles those.
"""


def _cluster_prompt(cluster: list[Event]) -> str:
    lines = [f"Cluster size: {len(cluster)}", "Candidates:"]
    for i, ev in enumerate(cluster):
        a = ev.attributes
        assert isinstance(a, AppointmentAttributes)
        nd = a.evidence[0].note_date if a.evidence else None
        quote = a.evidence[0].quote if a.evidence else ""
        anchor = a.scheduled_for_date or a.occurred_on
        lines.append(
            f"  [{i}] note_date={nd} status={a.status} "
            f"appointment_date={anchor} parties={list(a.parties)} "
            f"evidence={quote[:200]!r}"
        )
    return "\n".join(lines)


def _resolve_cluster(
    cluster: list[Event], llm: StructuredLLM
) -> _ClusterResolution | None:
    try:
        return llm.structured(
            system=_SYSTEM_PROMPT,
            user=_cluster_prompt(cluster),
            response_model=_ClusterResolution,
        )
    except LLMError as exc:
        _log.warning("cluster-recon LLM failed: %s — skipping cluster", exc)
        return None


# --- Stage 3: deterministic date fields -------------------------


def _encounter_date(cluster: list[Event]) -> date | None:
    """Mode of contributing dates, preferring `occurred_on` over
    `scheduled_for_date`. Ties broken by earliest date."""
    occurred, scheduled = [], []
    for ev in cluster:
        a = ev.attributes
        assert isinstance(a, AppointmentAttributes)
        (occurred if a.occurred_on else scheduled).append(
            a.occurred_on or a.scheduled_for_date
        )
    pool = [d for d in (occurred or scheduled) if d is not None]
    if not pool:
        return None
    counts = Counter(pool)
    top = max(counts.values())
    return min(d for d, c in counts.items() if c == top)


def _notice_date(
    cluster: list[Event], encounter: date | None
) -> date | None:
    """Earliest contributing note_date <= encounter. Retrospective
    recaps (note_date > encounter) excluded."""
    if encounter is None:
        return None
    eligible = [
        e.note_date
        for ev in cluster
        for e in ev.attributes.evidence  # type: ignore[union-attr]
        if e.note_date <= encounter
    ]
    return min(eligible) if eligible else None


# --- Top-level entry --------------------------------------------


def reconcile_appointments(
    claim_id: str,
    date_of_loss: date | None,
    candidates: list[Event],
    llm: StructuredLLM,
) -> list[Event]:
    if not candidates:
        _log.info("reconcile claim=%s candidates=0 -> 0", claim_id)
        return []

    clusters = _precluster(candidates)
    _log.info(
        "reconcile claim=%s candidates=%d -> clusters=%d",
        claim_id,
        len(candidates),
        len(clusters),
    )

    out: list[Event] = []
    for idx, cluster in enumerate(clusters):
        encounter = _encounter_date(cluster)
        resolution = _resolve_cluster(cluster, llm)
        if resolution is None:
            continue  # LLM gave up after retries — skip this cluster

        parties = [
            p for p in resolution.parties if _normalize_party(p)
        ]
        notice = _notice_date(cluster, encounter)

        if resolution.status in {"attended", "missed", "cancelled", "unknown"}:
            occurred_on, scheduled_for_date = encounter, None
        else:  # scheduled
            occurred_on, scheduled_for_date = None, encounter

        # Union evidence pairs across contributors, dedup by
        # (note_date, quote). Sort by note_date for stable output.
        seen_evidence: set[tuple[date, str]] = set()
        evidence_items: list[AppointmentEvidence] = []
        for ev in cluster:
            for item in ev.attributes.evidence:  # type: ignore[union-attr]
                key = (item.note_date, item.quote)
                if key in seen_evidence:
                    continue
                seen_evidence.add(key)
                evidence_items.append(item)
        evidence_items.sort(key=lambda e: (e.note_date, e.quote))

        attrs = AppointmentAttributes(
            parties=tuple(parties),
            scheduled_notice_date=notice,
            scheduled_for_date=scheduled_for_date,
            occurred_on=occurred_on,
            status=resolution.status,
            evidence=tuple(evidence_items),
        )
        event_date = (
            occurred_on or scheduled_for_date or cluster[0].event_date
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

        kind = "occurred" if occurred_on else "scheduled" if scheduled_for_date else "?"
        _log.info(
            "recon claim=%s cluster=%d size=%d :: date=%s kind=%s "
            "status=%s parties=[%s] notice=%s",
            claim_id,
            idx,
            len(cluster),
            event_date,
            kind,
            resolution.status,
            "|".join(parties) or "-",
            notice or "-",
        )

    _log.info(
        "reconcile claim=%s clusters=%d -> canonical=%d",
        claim_id,
        len(clusters),
        len(out),
    )
    return out
