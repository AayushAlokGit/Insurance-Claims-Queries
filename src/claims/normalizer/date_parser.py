"""Shared date parser. See normalizer.md §4.

Single source of truth for every date string seen in this
pipeline. Header dates flow through `normalize()`; body date
substrings will flow through here too once the extractors are
built (DD-013). Failure is a first-class return value (`iso=None`)
— callers decide whether to skip, flag, or reject.

US-locale MDY convention is assumed (workers'-comp claim notes
are US-origin). `5/4` parses as May 4, not April 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Cutoff for two-digit-year pivot. 00-69 → 2000-2069; 70-99 → 1970-1999.
# Configurable per deployment if the corpus stretches to older claims.
_PIVOT_CUTOFF = 70

# "More-than-six-months in the future" threshold for the yearless
# inference rule. A `12/15` seen in a January-2026 note should
# resolve to Dec 2025, not Dec 2026.
_YEARLESS_FUTURE_DAYS = 180


@dataclass(frozen=True)
class DateParseResult:
    iso: str | None
    flags: tuple[str, ...] = ()


# Numeric MDY with explicit 4-digit year: 4/17/2025, 04-17-2025, 4.17.2025
_RE_MDY4 = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")

# Numeric MDY with 2-digit year: 4-17-25, 4/17/25, 5.21.25
_RE_MDY2 = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})(?!\d)")

# Yearless: 4/17, 11-14, 12.05 — needs reference_date context
_RE_MD = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})(?![-/.\d])")

# Month name forms: Apr 17 2025, April 17 2025, Apr 17, 2025
_RE_MONTH_NAME = re.compile(
    r"^\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})"
)

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _pivot(yy: int) -> int:
    return 2000 + yy if yy < _PIVOT_CUTOFF else 1900 + yy


def _build(y: int, m: int, d: int, *, flags: tuple[str, ...] = ()) -> DateParseResult:
    try:
        return DateParseResult(date(y, m, d).isoformat(), flags)
    except ValueError:
        return DateParseResult(None, ("invalid_components",))


def parse_date(
    input: str, *, reference_date: date | None = None
) -> DateParseResult:
    """Parse the leading date in `input`. Trailing content (time,
    timezone, "9:00AM CT") is ignored — callers responsible for
    semantic interpretation of those if they care.

    Returns iso=None on failure. The `flags` tuple carries
    quality signals — `year_inferred` for yearless dates,
    `invalid_components` for things like Feb 30, etc.
    """
    if not input or not input.strip():
        return DateParseResult(None, ("empty_input",))
    s = input

    if (m := _RE_MDY4.match(s)) is not None:
        return _build(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    if (m := _RE_MDY2.match(s)) is not None:
        return _build(
            _pivot(int(m.group(3))),
            int(m.group(1)),
            int(m.group(2)),
        )

    if (m := _RE_MONTH_NAME.match(s)) is not None:
        name = m.group(1).lower().rstrip(".")
        month = _MONTHS.get(name)
        if month is not None:
            return _build(int(m.group(3)), month, int(m.group(2)))

    if (m := _RE_MD.match(s)) is not None:
        if reference_date is None:
            return DateParseResult(None, ("yearless_no_reference",))
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(reference_date.year, month, day)
        except ValueError:
            return DateParseResult(None, ("invalid_components",))
        # If the candidate is far in the future relative to the
        # reference, treat it as last year (normalizer.md §4.2).
        if (candidate - reference_date).days > _YEARLESS_FUTURE_DAYS:
            candidate = candidate.replace(year=reference_date.year - 1)
        return DateParseResult(candidate.isoformat(), ("year_inferred",))

    return DateParseResult(None, ("unparseable",))
