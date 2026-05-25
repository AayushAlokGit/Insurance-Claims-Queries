"""Per-extractor eval for `AppointmentExtractor`.

Each fixture in `appointment_fixtures.ALL_FIXTURES` is one
(note → expected candidates) test case. The runner builds a `Note`,
calls `AppointmentExtractor(llm).extract(note)`, and matches emitted
candidates against the fixture's `expected` list per the rules in
the fixture-module docstring.

Skipped by default. Run with `pytest --eval`. Costs ~$0.001-0.005 per
fixture × 25 fixtures ≈ $0.05-0.15 per full eval run.

Complements the end-to-end Layer 1 eval in `test_golden.py`:
- Layer 1 measures consumer-facing query correctness end-to-end.
- This module isolates the per-note extractor's behavior so prompt
  changes can be iterated against without paying for full ingest.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from claims.extractor.appointment import AppointmentExtractor
from claims.llm import get_client
from claims.models import AppointmentAttributes, Event, Note

from tests.eval.appointment_fixtures import (
    ALL_FIXTURES,
    ExpectedCandidate,
    Fixture,
)

_log = logging.getLogger("eval.appointment")

pytestmark = pytest.mark.eval


@pytest.fixture(scope="session")
def extractor() -> AppointmentExtractor:
    """One shared extractor across all fixture tests in this session."""
    from dotenv import load_dotenv

    load_dotenv()
    llm = get_client()
    return AppointmentExtractor(llm)


def _build_note(fix: Fixture) -> Note:
    return Note(
        note_id=f"eval-{fix.name}",
        claim_id="eval-claim",
        note_date=fix.note_date,
        activity=fix.activity,
        author=fix.author,
        body=fix.body,
    )


def _emitted_kind(attrs: AppointmentAttributes) -> str:
    """Discriminator that matches `ExpectedCandidate.kind`. `occurred`
    if `occurred_on` is set; `scheduled` if only `scheduled_for_date`
    is set; otherwise `?` (shouldn't happen for emitted candidates)."""
    if attrs.occurred_on is not None:
        return "occurred"
    if attrs.scheduled_for_date is not None:
        return "scheduled"
    return "?"


def _candidate_anchor_date(attrs: AppointmentAttributes):
    return attrs.occurred_on or attrs.scheduled_for_date


def _party_substr_matches(
    expected_subs: tuple[str, ...], parties: tuple[str, ...]
) -> tuple[bool, list[str]]:
    """Returns (all_match, missing). Each expected substring must
    appear (case-insensitive) in at least one of `parties`."""
    blob = " | ".join(parties).lower()
    missing = [s for s in expected_subs if s.lower() not in blob]
    return (not missing), missing


def _match_candidate(
    expected: ExpectedCandidate, attrs: AppointmentAttributes
) -> tuple[bool, str]:
    """Returns (match, reason_if_not). A candidate matches an
    expected if date + status + parties + kind all align."""
    if _candidate_anchor_date(attrs) != expected.date:
        return False, f"date mismatch (got {_candidate_anchor_date(attrs)})"
    if attrs.status != expected.status:
        return False, f"status mismatch (got {attrs.status!r})"
    if expected.kind is not None and _emitted_kind(attrs) != expected.kind:
        return False, f"kind mismatch (got {_emitted_kind(attrs)!r})"
    if expected.parties_must_be_empty:
        if attrs.parties:
            return False, f"expected empty parties, got {list(attrs.parties)}"
    elif expected.parties_substrings:
        ok, missing = _party_substr_matches(
            expected.parties_substrings, attrs.parties
        )
        if not ok:
            return (
                False,
                f"party substring(s) {missing} not in {list(attrs.parties)}",
            )
    return True, ""


def _format_event(ev: Event) -> str:
    """Compact one-line summary of an emitted event for failure reports."""
    attrs = ev.attributes
    assert isinstance(attrs, AppointmentAttributes)
    parties = "|".join(attrs.parties) or "-"
    return (
        f"date={_candidate_anchor_date(attrs)} "
        f"status={attrs.status} kind={_emitted_kind(attrs)} "
        f"parties=[{parties}]"
    )


def _format_expected(e: ExpectedCandidate) -> str:
    if e.parties_must_be_empty:
        parties = "[]"
    elif e.parties_substrings:
        parties = "any-of(" + ", ".join(e.parties_substrings) + ")"
    else:
        parties = "*"
    kind = e.kind or "*"
    return f"date={e.date} status={e.status} kind={kind} parties={parties}"


@pytest.mark.parametrize(
    "fix",
    ALL_FIXTURES,
    ids=[f.name for f in ALL_FIXTURES],
)
def test_appointment_fixture(
    fix: Fixture, extractor: AppointmentExtractor
) -> None:
    note = _build_note(fix)
    events = extractor.extract(note)

    # Collect attribute objects (frozen) for matching.
    emitted: list[AppointmentAttributes] = []
    for ev in events:
        attrs = ev.attributes
        assert isinstance(attrs, AppointmentAttributes), (
            f"non-appointment attrs from AppointmentExtractor: {attrs!r}"
        )
        emitted.append(attrs)

    # Greedy match: each expected consumes one emitted (by first
    # match). Surface every unmatched expected and every unmatched
    # emitted in the failure report.
    used_emitted: set[int] = set()
    unmatched_expected: list[ExpectedCandidate] = []

    for exp in fix.expected:
        found_idx = -1
        last_reason = ""
        for i, attrs in enumerate(emitted):
            if i in used_emitted:
                continue
            ok, reason = _match_candidate(exp, attrs)
            if ok:
                found_idx = i
                break
            last_reason = reason
        if found_idx == -1:
            unmatched_expected.append(exp)
            _log.debug(
                "no match for %s; last_reason=%s",
                _format_expected(exp),
                last_reason or "(no candidates emitted)",
            )
        else:
            used_emitted.add(found_idx)

    extras = [
        emitted[i] for i in range(len(emitted)) if i not in used_emitted
    ]

    # Build a structured report regardless of pass/fail.
    lines = [
        f"[{fix.name}] emitted={len(emitted)} expected={len(fix.expected)} "
        f"matched={len(used_emitted)}",
    ]
    if unmatched_expected:
        lines.append("  missing expected:")
        for e in unmatched_expected:
            lines.append(f"    - {_format_expected(e)}")
    if extras:
        lines.append("  extra emitted:")
        for a in extras:
            ev_summary = (
                f"date={_candidate_anchor_date(a)} status={a.status} "
                f"kind={_emitted_kind(a)} parties=[{'|'.join(a.parties) or '-'}]"
            )
            lines.append(f"    - {ev_summary}")

    print("\n" + "\n".join(lines))

    # Pass criteria: every expected must match. Extras fail unless
    # the fixture opts into `allow_extras`.
    fails: list[str] = []
    if unmatched_expected:
        fails.append(f"{len(unmatched_expected)} expected unmatched")
    if extras and not fix.allow_extras:
        fails.append(f"{len(extras)} unexpected extra(s) emitted")

    if fails:
        pytest.fail("; ".join(fails) + "\n" + "\n".join(lines))
