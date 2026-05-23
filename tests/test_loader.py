"""Phase 3 loader tests.

Pinned against the two real sample files. Exit criterion: both
claims parse deterministically; note counts match the values
hand-counted from a grep over the source.
"""

from pathlib import Path

import pytest

from claims.loader import LoaderError, parse_file

SAMPLES = Path(__file__).resolve().parent.parent / "sample_claim_notes"


def test_sample_1_parses() -> None:
    claim = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    assert claim.header.claim_id == "1-29RT"
    assert claim.header.account_raw == "2100000000 - Company WW"
    assert claim.header.source_file == "sample_claim_notes1.md"
    assert len(claim.notes) == 97


def test_sample_2_parses() -> None:
    claim = parse_file(str(SAMPLES / "sample_claim_notes2.md"))
    assert claim.header.claim_id == "2-248KR"
    assert claim.header.account_raw == "0005 - Group W"
    assert claim.header.source_file == "sample_claim_notes2.md"
    assert len(claim.notes) == 113


def test_note_blocks_carry_claim_id() -> None:
    claim = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    assert all(n.claim_id == "1-29RT" for n in claim.notes)


def test_first_note_shape_unbolded() -> None:
    """File 1 has plain-text headers. The raw_header must
    preserve the original line verbatim (no stripping), and the
    body must start immediately after."""
    claim = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    first = claim.notes[0]
    assert first.raw_header.startswith("Date: 01/08/2026")
    assert "Activity: Resolution Strategy" in first.raw_header
    assert "Noted By: M.H." in first.raw_header
    # body should begin with the note content, not the header
    assert first.raw_body.startswith("Note:")


def test_first_note_shape_bolded() -> None:
    """File 2 wraps headers in **...**. The raw_header keeps the
    asterisks; the Normalizer is responsible for stripping them."""
    claim = parse_file(str(SAMPLES / "sample_claim_notes2.md"))
    first = claim.notes[0]
    assert first.raw_header.startswith("**Date: 08/29/2025")
    assert first.raw_header.endswith("**")
    assert "Activity: Resolution Strategy" in first.raw_header


def test_offsets_are_monotonic() -> None:
    """source_offset must increase strictly across notes — a
    sanity check that we're slicing the file in order."""
    claim = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    offsets = [n.source_offset for n in claim.notes]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_body_extends_to_next_header(tmp_path: Path) -> None:
    """Body of note N stops at the start of note N+1's header
    line, not before. We pin this on a tiny synthetic file so it
    doesn't depend on the sample corpus."""
    content = (
        "Claim #: TEST-1\n"
        "Account #: 1 - X\n"
        "\n"
        "Date: 01/01/2025 | Activity: A | Noted By: X\n"
        "first body line\n"
        "second body line\n"
        "\n"
        "Date: 01/02/2025 | Activity: B | Noted By: Y\n"
        "third body line\n"
    )
    p = tmp_path / "synthetic.md"
    p.write_text(content, encoding="utf-8")
    claim = parse_file(str(p))
    assert len(claim.notes) == 2
    assert "first body line" in claim.notes[0].raw_body
    assert "second body line" in claim.notes[0].raw_body
    assert "third body line" not in claim.notes[0].raw_body
    assert claim.notes[1].raw_body.strip() == "third body line"


def test_missing_claim_id_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.md"
    p.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(LoaderError):
        parse_file(str(p))


def test_no_note_headers_raises(tmp_path: Path) -> None:
    p = tmp_path / "headerless.md"
    p.write_text("Claim #: X-1\nAccount #: 1 - X\n", encoding="utf-8")
    with pytest.raises(LoaderError):
        parse_file(str(p))
