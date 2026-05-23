"""Date parser tests. Covers every surface form in normalizer.md
§4, the two-digit-year pivot, the yearless-inference rule, and
the failure-mode flags from §6."""

from __future__ import annotations

from datetime import date

import pytest

from claims.normalizer import parse_date


@pytest.mark.parametrize(
    ("input_str", "expected_iso"),
    [
        ("4-17-25", "2025-04-17"),
        ("4/17/2025", "2025-04-17"),
        ("4.17.25", "2025-04-17"),
        ("04-17-25", "2025-04-17"),
        ("Apr 17 2025", "2025-04-17"),
        ("April 17 2025", "2025-04-17"),
        ("Apr 17, 2025", "2025-04-17"),
        ("Apr 17 2025 9:00AM", "2025-04-17"),
        ("01/08/2026 10:02am CT", "2026-01-08"),
        ("12-22-24", "2024-12-22"),
    ],
)
def test_surface_forms(input_str: str, expected_iso: str) -> None:
    result = parse_date(input_str)
    assert result.iso == expected_iso


@pytest.mark.parametrize(
    ("yy_input", "expected_year"),
    [
        ("5.21.00", 2000),
        ("5.21.25", 2025),
        ("5.21.69", 2069),
        ("5.21.70", 1970),
        ("5.21.99", 1999),
    ],
)
def test_two_digit_year_pivot(yy_input: str, expected_year: int) -> None:
    result = parse_date(yy_input)
    assert result.iso is not None
    assert int(result.iso.split("-")[0]) == expected_year


def test_yearless_with_reference_same_year() -> None:
    """`4/17` seen in a note dated 2025-04-14 means 2025-04-17."""
    result = parse_date("4/17", reference_date=date(2025, 4, 14))
    assert result.iso == "2025-04-17"
    assert "year_inferred" in result.flags


def test_yearless_falls_back_to_previous_year() -> None:
    """`12/15` seen in a note dated 2025-03-01 means 2024-12-15
    — a date more than six months in the future is more likely
    last year's."""
    result = parse_date("12/15", reference_date=date(2025, 3, 1))
    assert result.iso == "2024-12-15"
    assert "year_inferred" in result.flags


def test_yearless_without_reference_fails_with_flag() -> None:
    result = parse_date("4/17")
    assert result.iso is None
    assert "yearless_no_reference" in result.flags


def test_unparseable_returns_flag() -> None:
    result = parse_date("not a date at all")
    assert result.iso is None
    assert "unparseable" in result.flags


def test_empty_input_returns_flag() -> None:
    assert parse_date("").iso is None
    assert parse_date("   ").iso is None


def test_invalid_components_caught() -> None:
    """Feb 30 is not a date."""
    result = parse_date("2-30-25")
    assert result.iso is None
    assert "invalid_components" in result.flags
