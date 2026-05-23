"""Tests for the regex-based claim-metadata inference.

Pinned against the two real sample files so we know which fields
are actually recoverable from each. Synthetic fixtures cover the
fallback edge cases."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from claims.loader import infer_claim_metadata, parse_file

SAMPLES = Path(__file__).resolve().parent.parent / "sample_claim_notes"


def test_sample_1_metadata() -> None:
    """Claim 1: DOL recoverable from `Date of Injury: 12/21/2024`;
    no `Jurisdiction:` line present — leave None."""
    loaded = parse_file(str(SAMPLES / "sample_claim_notes1.md"))
    meta = infer_claim_metadata(loaded)
    assert meta.date_of_loss == date(2024, 12, 21)
    assert meta.jurisdiction is None
    assert meta.claim_type == "injury"  # default


def test_sample_2_metadata() -> None:
    """Claim 2: both DOL and jurisdiction recoverable."""
    loaded = parse_file(str(SAMPLES / "sample_claim_notes2.md"))
    meta = infer_claim_metadata(loaded)
    assert meta.date_of_loss == date(2024, 12, 30)
    assert meta.jurisdiction == "SC"
    assert meta.claim_type == "injury"


def test_no_signals_yields_none_fields(tmp_path: Path) -> None:
    p = tmp_path / "minimal.md"
    p.write_text(
        "Claim #: TEST-1\n"
        "Account #: 1 - X\n"
        "\n"
        "Date: 01/01/2025 | Activity: Contact | Noted By: X\n"
        "Body with no metadata signals at all.\n",
        encoding="utf-8",
    )
    meta = infer_claim_metadata(parse_file(str(p)))
    assert meta.date_of_loss is None
    assert meta.jurisdiction is None
    assert meta.claim_type == "injury"  # default fallback


def test_picks_first_dol_when_repeated(tmp_path: Path) -> None:
    """A claim may state DOL multiple times; the first occurrence
    wins. (Both samples restate Jurisdiction/DOL across notes.)"""
    p = tmp_path / "dup.md"
    p.write_text(
        "Claim #: TEST-1\n"
        "Account #: 1 - X\n"
        "\n"
        "Date: 03/01/2025 | Activity: Contact | Noted By: X\n"
        "Date of Injury: 02/15/2025\n"
        "\n"
        "Date: 04/01/2025 | Activity: Contact | Noted By: X\n"
        "Date of Injury: 02/20/2025\n",  # later restatement — ignored
        encoding="utf-8",
    )
    meta = infer_claim_metadata(parse_file(str(p)))
    assert meta.date_of_loss == date(2025, 2, 15)
