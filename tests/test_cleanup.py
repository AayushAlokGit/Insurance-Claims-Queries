"""Body cleanup tests. Covers every row in normalizer.md §3.1."""

from claims.normalizer import clean_text


def test_em_dash_repaired() -> None:
    # `â€"` (the three-codepoint mojibake) → em dash —
    assert clean_text("Modified duty â€” next") == "Modified duty — next"


def test_right_single_quote_repaired() -> None:
    assert clean_text("itâ€™s fine") == "it’s fine"


def test_curly_double_quotes_repaired() -> None:
    """Left and right double quotes both round-trip. The left
    form (â€œ) must be replaced before the bare `â€` (right)
    pattern, or the longer match never fires."""
    assert clean_text("â€œhelloâ€") == "“hello”"


def test_nbsp_artifact_repaired() -> None:
    """Real mojibake from a UTF-8 NBSP (U+00A0) misread as
    Windows-1252 is `Â` + NBSP, not `Â` + regular space."""
    nbsp = " "
    assert clean_text(f"partÂ{nbsp}two") == "part two"


def test_crlf_to_lf() -> None:
    assert clean_text("line1\r\nline2") == "line1\nline2"


def test_tab_to_space() -> None:
    assert clean_text("a\tb") == "a b"


def test_trailing_whitespace_stripped_per_line() -> None:
    assert clean_text("a   \nb \n") == "a\nb\n"


def test_idempotent_on_clean_text() -> None:
    clean = "Modified duty — next\nLine 2"
    assert clean_text(clean) == clean
