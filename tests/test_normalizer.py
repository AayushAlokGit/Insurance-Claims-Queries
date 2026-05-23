"""Integration tests for Normalizer orchestration.

Covers the per-note shape (typed header, cleaned body), the
optional Activity / Noted By fields, and the failure-mode flags
that propagate to the Note's data_quality_flags."""

from __future__ import annotations

from pathlib import Path

from claims.loader import RawNoteBlock, parse_file
from claims.normalizer import normalize

SAMPLES = Path(__file__).resolve().parent.parent / "sample_claim_notes"


def _block(
    header: str,
    body: str = "Note: example body\n",
    claim_id: str = "TEST-1",
) -> RawNoteBlock:
    return RawNoteBlock(
        claim_id=claim_id, raw_header=header, raw_body=body, source_offset=0
    )


def test_unbolded_header_parsed() -> None:
    block = _block(
        "Date: 01/08/2026 10:02am CT | Activity: Resolution Strategy | Noted By: M.H."
    )
    note = normalize(block, index=0)
    assert note is not None
    assert note.note_date.isoformat() == "2026-01-08"
    assert note.activity == "Resolution Strategy"
    assert note.author == "M.H."
    assert note.note_id == "TEST-1-n0000"
    assert note.data_quality_flags == []


def test_bolded_header_parsed() -> None:
    block = _block(
        "**Date: 08/29/2025 12:22pm CT | Activity: Investigation | Noted By: R.N.**"
    )
    note = normalize(block, index=42)
    assert note is not None
    assert note.note_date.isoformat() == "2025-08-29"
    assert note.activity == "Investigation"
    assert note.note_id == "TEST-1-n0042"


def test_missing_activity_yields_none() -> None:
    """normalizer.md §6: a header without an Activity field
    produces activity=None rather than rejecting the note."""
    block = _block("Date: 01/08/2026 | Noted By: M.H.")
    note = normalize(block, index=0)
    assert note is not None
    assert note.activity is None


def test_body_cleanup_applied() -> None:
    block = _block(
        "Date: 11/14/2025 | Activity: Investigation | Noted By: M.H.",
        body="Work Status: Modified duty â€” sedentary\r\nLine 2\t with tab",
    )
    note = normalize(block, index=0)
    assert note is not None
    assert "—" in note.body
    assert "\r" not in note.body
    assert "\t" not in note.body


def test_unparseable_header_returns_none() -> None:
    block = _block("not even a header")
    assert normalize(block, index=0) is None


def test_implausible_date_flagged() -> None:
    """Year < 2000 → keep the note but flag it."""
    block = _block(
        "Date: 01/08/1985 | Activity: Investigation | Noted By: M.H."
    )
    note = normalize(block, index=0)
    assert note is not None
    assert "header_date_implausible" in note.data_quality_flags


def test_sample_1_first_note_round_trip() -> None:
    """End-to-end: load and normalize the first note of sample 1.
    Pins the values we expect after Phase 4's cleanup."""
    claim = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    note = normalize(claim.notes[0], index=0)
    assert note is not None
    assert note.claim_id == "1-29RT"
    assert note.note_date.isoformat() == "2026-01-08"
    assert note.activity == "Resolution Strategy"
    assert note.author == "M.H."
    # Body must have had mojibake cleaned up — the file contains
    # `Modified duty â€" scheduling coordinator` which should now
    # have a real em-dash.
    assert "Modified duty — scheduling coordinator" in note.body


def test_sample_2_first_note_round_trip() -> None:
    """File 2 uses **...** wrapping on headers and `---`
    separators. Both must be handled cleanly."""
    claim = parse_file(str(SAMPLES / "sample_claim_notes2.md"))
    note = normalize(claim.notes[0], index=0)
    assert note is not None
    assert note.claim_id == "2-248KR"
    assert note.note_date.isoformat() == "2025-08-29"
    assert note.activity == "Resolution Strategy"
    assert note.author == "R.N."
