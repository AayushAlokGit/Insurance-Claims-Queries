"""Normalizer stage.

Two concerns: destructive body cleanup (mojibake repair, line
endings, whitespace) and non-destructive date interpretation via
the shared parser. See normalizer.md.
"""

from claims.normalizer.cleanup import clean_text
from claims.normalizer.normalizer import normalize
from claims.normalizer.date_parser import DateParseResult, parse_date

__all__ = [
    "DateParseResult",
    "clean_text",
    "normalize",
    "parse_date",
]
