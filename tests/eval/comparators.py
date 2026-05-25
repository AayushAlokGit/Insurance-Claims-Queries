"""Per-query comparators for golden-output regression.

Each comparator returns a `Diff` describing what (if anything) drifted
between expected (golden) and actual output. A Diff is "ok" if the
output is within tolerance for that query type, "fail" otherwise. The
test code in `test_golden.py` asserts `diff.ok` and prints
`diff.report` on failure.

Per-query tolerance rules:
- Q1: strict on every structural field; evidence/ids ignored.
- Q3: exact match on bucket counts, deltas, and authors; evidence/ids ignored.
- Q2: set comparison with date±1d + party-overlap match. Pass criteria:
      recall ≥ 0.85, precision ≥ 0.80, all required appointments present.

Required-appointment lists are encoded in `REQUIRED_APPOINTMENTS`:
events the system MUST surface for the eval to pass. They guard
against LLM jitter silently dropping known-real events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Events the pipeline MUST surface for each claim. Hand-picked from
# unambiguous templated medical-record blocks (`Date of Appointment:`
# headers). If one of these disappears, that's a real regression.
REQUIRED_APPOINTMENTS: dict[str, list[tuple[str, str]]] = {
    "1-29RT": [
        # (date, primary_party_substring) — case-insensitive match
        ("2025-02-05", "harmon"),
        ("2025-03-06", "harmon"),
        ("2025-03-14", "sinclair"),
        ("2025-03-28", "vega"),
        ("2025-04-11", "harmon"),
        ("2025-04-17", "valley pt"),
        ("2025-05-10", "valley pt"),
        ("2025-06-27", "harmon"),
        ("2025-07-25", "harmon"),
        ("2025-08-22", "harmon"),
        ("2025-09-19", "harmon"),
        ("2025-10-14", "bridge"),
        ("2025-11-14", "harmon"),
    ],
    "2-248KR": [
        ("2024-12-30", "occupational health"),
        ("2025-01-06", "occupational health"),
        ("2025-04-22", "caldwell"),
        ("2025-05-21", "farano"),
        ("2025-07-03", "caldwell"),
    ],
}


# Tolerance knobs. Tightening is fine; loosening warrants discussion.
Q2_RECALL_FLOOR = 0.85
Q2_PRECISION_FLOOR = 0.80


@dataclass
class Diff:
    """Result of comparing one query's golden vs actual output."""

    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)

    @property
    def report(self) -> str:
        if self.ok:
            return self.summary
        return self.summary + "\n  " + "\n  ".join(self.details)


# --- Q1 -------------------------------------------------------------


def diff_q1(expected: dict, actual: dict) -> Diff:
    """Q1 is small and structural — every field that's set matters.
    Evidence quotes and event_id are skipped (audit metadata)."""
    fails: list[str] = []
    if expected.get("status") != actual.get("status"):
        fails.append(
            f"status: expected {expected.get('status')!r}, "
            f"got {actual.get('status')!r}"
        )
    # Per-status structural fields
    status = expected.get("status")
    if status == "returned":
        for k in ("rtw_date", "days", "duty_type"):
            if expected.get(k) != actual.get(k):
                fails.append(
                    f"{k}: expected {expected.get(k)!r}, got {actual.get(k)!r}"
                )
    elif status == "pending":
        # days_open drifts as today() advances — allow ±1
        e = expected.get("days_open", 0)
        a = actual.get("days_open", 0)
        if abs(a - e) > 1:
            fails.append(
                f"days_open: expected {e}, got {a} (tol ±1)"
            )

    if fails:
        return Diff(
            ok=False,
            summary=f"Q1 FAIL ({len(fails)} field(s) drifted)",
            details=fails,
        )
    return Diff(ok=True, summary=f"Q1 OK (status={status})")


# --- Q3 -------------------------------------------------------------


def diff_q3(expected: dict, actual: dict) -> Diff:
    """Q3 is pure regex — any drift is a real bug."""
    fails: list[str] = []
    exp_buckets = {b["bucket"]: b for b in expected.get("buckets", [])}
    act_buckets = {b["bucket"]: b for b in actual.get("buckets", [])}

    if set(exp_buckets) != set(act_buckets):
        only_e = sorted(set(exp_buckets) - set(act_buckets))
        only_a = sorted(set(act_buckets) - set(exp_buckets))
        if only_e:
            fails.append(f"buckets missing in actual: {only_e}")
        if only_a:
            fails.append(f"unexpected buckets in actual: {only_a}")

    for name, eb in exp_buckets.items():
        ab = act_buckets.get(name)
        if ab is None:
            continue  # already reported
        if eb.get("count") != ab.get("count"):
            fails.append(
                f"{name}: count expected {eb.get('count')}, "
                f"got {ab.get('count')}"
            )
        if str(eb.get("net")) != str(ab.get("net")):
            fails.append(
                f"{name}: net expected {eb.get('net')}, "
                f"got {ab.get('net')}"
            )
        # Per-swing exact compare (skip event_id)
        es = sorted(
            eb.get("swings", []),
            key=lambda s: (s["date"], str(s["new_amount"])),
        )
        as_ = sorted(
            ab.get("swings", []),
            key=lambda s: (s["date"], str(s["new_amount"])),
        )
        if len(es) != len(as_):
            fails.append(
                f"{name}: swing count expected {len(es)}, got {len(as_)}"
            )
            continue
        for i, (e, a) in enumerate(zip(es, as_)):
            for k in ("date", "new_amount", "previous_amount", "delta", "author"):
                if str(e.get(k)) != str(a.get(k)):
                    fails.append(
                        f"{name}[{i}].{k}: expected {e.get(k)!r}, "
                        f"got {a.get(k)!r}"
                    )

    if fails:
        return Diff(
            ok=False,
            summary=f"Q3 FAIL ({len(fails)} drift(s))",
            details=fails,
        )
    n_swings = sum(b["count"] for b in expected.get("buckets", []))
    return Diff(
        ok=True,
        summary=f"Q3 OK ({len(exp_buckets)} buckets, {n_swings} swings)",
    )


# --- Q2 -------------------------------------------------------------


def _normalize_parties(parties: list[str]) -> set[str]:
    return {p.strip().lower() for p in parties if p}


def _match_appointments(
    expected: list[dict], actual: list[dict]
) -> tuple[int, list[dict], list[dict]]:
    """Greedy bipartite-ish match. Two appointments match if dates are
    within ±1 day AND parties overlap (or both empty).

    Returns: (n_matched, missing_in_actual, extra_in_actual).
    """
    from datetime import date as _date

    used_actual_idx: set[int] = set()
    missing: list[dict] = []
    matched = 0

    for e in expected:
        e_date = _date.fromisoformat(e["date"])
        e_parties = _normalize_parties(e.get("parties") or [])
        found = False
        for i, a in enumerate(actual):
            if i in used_actual_idx:
                continue
            a_date = _date.fromisoformat(a["date"])
            if abs((e_date - a_date).days) > 1:
                continue
            a_parties = _normalize_parties(a.get("parties") or [])
            party_match = (
                bool(e_parties & a_parties)
                or (not e_parties and not a_parties)
                or (not e_parties)
                or (not a_parties)
            )
            if party_match:
                used_actual_idx.add(i)
                matched += 1
                found = True
                break
        if not found:
            missing.append(e)

    extra = [a for i, a in enumerate(actual) if i not in used_actual_idx]
    return matched, missing, extra


def _check_required(
    claim_id: str, actual_appts: list[dict]
) -> list[str]:
    """Each (date, party_substring) in REQUIRED_APPOINTMENTS must be
    matched by some actual appointment. Returns list of misses."""
    required = REQUIRED_APPOINTMENTS.get(claim_id, [])
    misses: list[str] = []
    for req_date, req_party_sub in required:
        sub = req_party_sub.lower()
        hit = False
        for a in actual_appts:
            if a["date"] != req_date:
                continue
            parties_blob = " ".join(a.get("parties") or []).lower()
            if sub in parties_blob:
                hit = True
                break
        if not hit:
            misses.append(f"{req_date} ({req_party_sub})")
    return misses


def diff_q2(claim_id: str, expected: dict, actual: dict) -> Diff:
    exp_appts = expected.get("appointments", [])
    act_appts = actual.get("appointments", [])

    n_matched, missing, extra = _match_appointments(exp_appts, act_appts)
    recall = n_matched / len(exp_appts) if exp_appts else 1.0
    precision = n_matched / len(act_appts) if act_appts else 1.0

    required_misses = _check_required(claim_id, act_appts)

    fails: list[str] = []
    if recall < Q2_RECALL_FLOOR:
        fails.append(
            f"recall={recall:.2f} below floor {Q2_RECALL_FLOOR}"
        )
    if precision < Q2_PRECISION_FLOOR:
        fails.append(
            f"precision={precision:.2f} below floor {Q2_PRECISION_FLOOR}"
        )
    if required_misses:
        fails.append(
            f"required appointments missing: {required_misses}"
        )

    details: list[str] = [
        f"expected={len(exp_appts)} actual={len(act_appts)} matched={n_matched}",
        f"recall={recall:.2f} (floor {Q2_RECALL_FLOOR})",
        f"precision={precision:.2f} (floor {Q2_PRECISION_FLOOR})",
    ]
    if missing:
        details.append(
            "missing in actual: "
            + ", ".join(
                f"{m['date']}({'/'.join(m.get('parties') or []) or '-'})"
                for m in missing[:10]
            )
        )
    if extra:
        details.append(
            "extra in actual: "
            + ", ".join(
                f"{e['date']}({'/'.join(e.get('parties') or []) or '-'})"
                for e in extra[:10]
            )
        )

    if fails:
        return Diff(
            ok=False,
            summary=f"Q2 FAIL ({'; '.join(fails)})",
            details=details,
        )
    return Diff(
        ok=True,
        summary=(
            f"Q2 OK (recall={recall:.2f}, precision={precision:.2f}, "
            f"matched={n_matched}/{len(exp_appts)})"
        ),
        details=details,
    )


# --- helpers --------------------------------------------------------


def parse_query_outputs(text: str) -> dict[str, Any]:
    """Parse the `=== qN ===`-delimited JSON file produced by
    `scripts/write_query_outputs.py`. Returns whichever query
    sections are present, keyed by name (e.g. `q1`, `q2`, `q3`).
    Q4 is parsed when present but Layer 1 evals ignore it — see
    `test_golden.py::_CASES`."""
    import json

    out: dict[str, Any] = {}
    for q in ("q1", "q2", "q3", "q4"):
        marker = f"=== {q} ==="
        if marker not in text:
            continue
        seg = text.split(marker, 1)[1].split("=== ", 1)[0]
        out[q] = json.loads(seg)
    return out