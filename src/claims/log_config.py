"""Logging setup for a single ingestion run.

Each pipeline stage emits via `logging.getLogger(__name__)` and
calls bubble up to the `claims` parent logger. `setup_logging`
attaches a per-run FileHandler at `logs/<timestamp>-<claim_id>.log`
plus a StreamHandler for human-readable terminal output.

Imports are kept stdlib-only so this can be called early in
__main__ without dragging in the rest of the package."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path("logs")


def setup_logging(
    *, claim_id: str | None = None, verbose: bool = False
) -> Path:
    """Configure the `claims` logger tree with a file + stream
    handler. Returns the path of the log file written for this run.

    File gets DEBUG-level detail (every per-note message). Console
    gets INFO by default, DEBUG when --verbose is passed."""
    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{claim_id}" if claim_id else ""
    log_path = LOGS_DIR / f"{ts}{suffix}.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    root = logging.getLogger("claims")
    root.setLevel(logging.DEBUG)
    # Re-runs (tests, repeated invocations) start fresh.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.propagate = False

    return log_path
