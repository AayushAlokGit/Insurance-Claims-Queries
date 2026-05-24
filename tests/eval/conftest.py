"""Eval-only pytest fixtures.

Wires a session-scoped file logger so every `pytest --eval` run leaves
a single forensic log at `logs/eval/<ts>.log`:

- All Python `logging` output from the test process at DEBUG (test
  harness, comparators, anything they call).
- Per-test pass/fail lines plus the captured stdout/stderr/log for
  that test (via `pytest_runtest_logreport` below) so a failing eval
  is fully debuggable from one file.

The ingest subprocess additionally writes its own
`logs/claims_ingest/` file — the eval log is the orchestrator's view,
not a replacement.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_LOG_DIR = REPO_ROOT / "logs" / "eval"


def _eval_selected(config: pytest.Config) -> bool:
    """Only set up file logging when eval tests are actually running.
    Otherwise the fixture would create empty log files on every
    `pytest` invocation."""
    return bool(config.getoption("--eval", default=False))


def _eval_handler_attached() -> bool:
    """True when the session fixture has installed its file handler.
    The logreport hook uses this to gate work without needing access
    to the pytest config."""
    root = logging.getLogger()
    return any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).parent == EVAL_LOG_DIR
        for h in root.handlers
    )


@pytest.fixture(scope="session", autouse=True)
def eval_file_logger(
    request: pytest.FixtureRequest,
) -> Iterator[Path | None]:
    """Attach a DEBUG file handler to the root logger for the run."""
    if not _eval_selected(request.config):
        yield None
        return

    EVAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = EVAL_LOG_DIR / f"{ts}.log"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s :: %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    prior_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    log = logging.getLogger("eval")
    log.info("=== eval run start :: log=%s ===", log_path)
    try:
        yield log_path
    finally:
        log.info("=== eval run end :: log=%s ===", log_path)
        handler.flush()
        root.removeHandler(handler)
        root.setLevel(prior_level)
        handler.close()
        # Surface the path on green runs too — pytest only echoes
        # captured stdout when something fails.
        print(f"\n[eval] log written to {log_path}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Mirror each test outcome (and its captured stdout/stderr/log)
    into the eval log file. Skipped when no eval log is attached."""
    if not _eval_handler_attached():
        return
    if report.when != "call" and not report.failed:
        return

    log = logging.getLogger("eval")
    status = (
        "PASS" if report.passed
        else "FAIL" if report.failed
        else "SKIP"
    )
    log.info(
        "test %s [%s] %s (%.3fs)",
        report.nodeid,
        report.when,
        status,
        report.duration,
    )
    for name, blob in (
        ("stdout", report.capstdout),
        ("stderr", report.capstderr),
        ("log", report.caplog),
    ):
        if blob:
            log.debug(
                "  captured %s for %s:\n%s",
                name,
                report.nodeid,
                blob.rstrip(),
            )
