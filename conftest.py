"""Pytest root config — registers the `eval` marker for end-to-end
golden-output regression tests.

Eval tests hit the live LLM API and cost money, so they are skipped
by default. Run them explicitly with `pytest --eval`. See
`tests/eval/README.md` (if present) or `tests/eval/test_golden.py`
for the test set.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--eval",
        action="store_true",
        default=False,
        help="Run end-to-end eval tests (hits LLM API; costs money).",
    )
    parser.addoption(
        "--saved-outputs",
        action="store_true",
        default=False,
        help=(
            "For test_golden: compare goldens against the saved "
            "`sample_claim_notes/query_outputs/<id>.json` files instead of "
            "running a fresh ingest. No LLM calls; useful for fast "
            "iteration on the golden files or the comparators themselves."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "eval: marks a test as a live-LLM eval (deselect with -m 'not eval')",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--eval"):
        return  # run everything including evals
    skip_eval = pytest.mark.skip(
        reason="eval tests skipped by default; run with `pytest --eval`"
    )
    for item in items:
        if "eval" in item.keywords:
            item.add_marker(skip_eval)
