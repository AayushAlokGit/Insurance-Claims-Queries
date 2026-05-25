"""End-to-end golden-output regression tests.

Skipped by default. Two run modes:

- `pytest --eval`               — ingest live, hits LLM API, costs money,
                                  takes ~80s. The canonical signal.
- `pytest --eval --saved-outputs` — compare goldens against the saved
                                  `sample_claim_notes/query_outputs/<id>.json`
                                  files. No LLM calls, sub-second. Useful
                                  when iterating on the goldens or the
                                  comparators themselves; assumes the saved
                                  outputs are current (regenerate via
                                  `scripts/write_query_outputs.py --claim-id <id>`).

Per design notes in `comparators.py`:
- Q1 / Q3: strict structural match
- Q2: set comparison with recall/precision floors + required-appt list
- Q4 is intentionally out of scope — see `_CASES` below.

A failing test prints a per-query Diff report explaining the drift.
Updating goldens after an intentional improvement: copy current
`sample_claim_notes/query_outputs/<id>.json` into
`tests/golden/<id>.json`, verify the new output is correct (or
better), commit.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

_log = logging.getLogger("eval")

from tests.eval.comparators import (
    diff_q1,
    diff_q2,
    diff_q3,
    parse_query_outputs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
SAVED_OUTPUTS_ROOT = REPO_ROOT / "sample_claim_notes" / "query_outputs"
CLAIM_FILES: dict[str, Path] = {
    "1-29RT": REPO_ROOT / "sample_claim_notes" / "sample_claim_notes1.md",
    "2-248KR": REPO_ROOT / "sample_claim_notes" / "sample_claim_notes2.md",
}

pytestmark = pytest.mark.eval


def _load_saved_outputs() -> dict[str, dict]:
    """Read the on-disk query JSONs as the actual-output side of the
    comparison. Resolves the provider/model slug from env so the
    outputs read match what `write_query_outputs.py` writes by default.
    Caller is responsible for keeping these current via
    `scripts/write_query_outputs.py`."""
    from dotenv import load_dotenv

    from claims.llm import provider_model_slug

    load_dotenv()
    slug = provider_model_slug()
    saved_dir = SAVED_OUTPUTS_ROOT / slug

    out: dict[str, dict] = {}
    for claim_id in CLAIM_FILES:
        path = saved_dir / f"{claim_id}.json"
        if not path.exists():
            pytest.fail(
                f"--saved-outputs requested but {path} does not exist; "
                f"run `python scripts/write_query_outputs.py --claim-id {claim_id}` first"
            )
        _log.info("load saved :: claim=%s slug=%s path=%s", claim_id, slug, path)
        out[claim_id] = parse_query_outputs(
            path.read_text(encoding="utf-8")
        )
    return out


def _ingest_and_render(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict]:
    """Live path: ingest both claims into a temp DB, render queries
    to stdout, parse the result."""
    work = tmp_path_factory.mktemp("eval")
    db = work / "eval.db"

    for claim_id, src in CLAIM_FILES.items():
        _log.info("ingest start :: claim=%s src=%s", claim_id, src)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "claims",
                "ingest",
                "--file",
                str(src),
                "--db",
                str(db),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        _log.debug(
            "ingest stderr (claim=%s):\n%s",
            claim_id,
            (result.stderr or "").rstrip(),
        )
        if result.returncode != 0:
            _log.error(
                "ingest FAILED claim=%s rc=%s", claim_id, result.returncode
            )
            pytest.fail(
                f"ingest failed for {claim_id}: "
                f"rc={result.returncode}\n{result.stderr[-2000:]}"
            )
        _log.info("ingest done :: claim=%s", claim_id)

    outputs: dict[str, dict] = {}
    for claim_id in CLAIM_FILES:
        _log.info("render start :: claim=%s", claim_id)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "write_query_outputs.py"),
                "--claim-id",
                claim_id,
                "--db",
                str(db),
                "--stdout",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        _log.debug(
            "render stderr (claim=%s):\n%s",
            claim_id,
            (result.stderr or "").rstrip(),
        )
        if result.returncode != 0:
            _log.error(
                "render FAILED claim=%s rc=%s", claim_id, result.returncode
            )
            pytest.fail(
                f"query render failed for {claim_id}: "
                f"rc={result.returncode}\n{result.stderr[-2000:]}"
            )
        outputs[claim_id] = parse_query_outputs(result.stdout)
        _log.info("render done :: claim=%s", claim_id)

    return outputs


@pytest.fixture(scope="session")
def fresh_outputs(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict]:
    """Outputs to compare goldens against.

    Default path: ingest both sample claims into a temp DB, render the
    four queries per claim, parse the result. Session-scoped: one
    ingest pass shared across all eval tests in this run. Costs
    ~$0.10-0.20 in LLM API calls per session.

    With `--saved-outputs`: read the on-disk
    `sample_claim_notes/query_outputs/<id>.json` files instead. No
    LLM calls. The fixture name stays `fresh_outputs` for backward
    compatibility with the parametrize wiring below — the test body
    doesn't care whether the bytes came from a fresh ingest or a
    saved file."""
    if request.config.getoption("--saved-outputs"):
        return _load_saved_outputs()
    return _ingest_and_render(tmp_path_factory)


@pytest.fixture(scope="session")
def goldens() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for claim_id in CLAIM_FILES:
        text = (GOLDEN_DIR / f"{claim_id}.json").read_text(encoding="utf-8")
        out[claim_id] = parse_query_outputs(text)
    return out


# Parametrize: (claim_id, query). Q4 is intentionally excluded — its
# numbers are a distribution derived from Q2, and setting a sensible
# golden bar on a distribution from a 2-claim corpus is unreliable.
# Revisit if the corpus grows or a downstream consumer needs it.
_CASES = [(cid, q) for cid in CLAIM_FILES for q in ("q1", "q2", "q3")]


@pytest.mark.parametrize("claim_id,query", _CASES)
def test_golden_regression(
    claim_id: str,
    query: str,
    fresh_outputs: dict[str, dict],
    goldens: dict[str, dict],
) -> None:
    exp = goldens[claim_id][query]
    act = fresh_outputs[claim_id][query]

    if query == "q1":
        diff = diff_q1(exp, act)
    elif query == "q2":
        diff = diff_q2(claim_id, exp, act)
    elif query == "q3":
        diff = diff_q3(exp, act)
    else:
        pytest.fail(f"unknown query {query!r}")

    line = f"[{claim_id} {query}] {diff.report}"
    print("\n" + line)
    (_log.info if diff.ok else _log.error)(line)
    assert diff.ok, diff.report
