"""Logging setup for a single ingestion run.

Each pipeline stage emits via `logging.getLogger(__name__)` and
calls bubble up to the `claims` parent logger. `setup_logging`
attaches a per-run FileHandler under
`logs/claims_<subcommand>/<timestamp>-<claim_id>-<tags>.log`
plus a StreamHandler for human-readable terminal output.

Imports are kept stdlib-only so this can be called early in
__main__ without dragging in the rest of the package."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path("logs")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    """Collapse anything filesystem-hostile into a single dash."""
    return _SAFE.sub("-", value).strip("-")


def setup_logging(
    *,
    subcommand: str | None = None,
    claim_id: str | None = None,
    tags: dict[str, str] | None = None,
    verbose: bool = False,
) -> Path:
    """Configure the `claims` logger tree with a file + stream
    handler. Returns the path of the log file written for this run.

    File gets DEBUG-level detail (every per-note message). Console
    gets INFO by default, DEBUG when --verbose is passed.

    Layout: `logs/claims_<subcommand>/<ts>-<claim_id>-<tag1>-<tag2>.log`.
    `tags` values are appended in iteration order, slugified for
    filesystem safety. Successive runs are distinguished by the
    timestamp + tag tail.
    """
    sub_dir = f"claims_{_slug(subcommand)}" if subcommand else "claims"
    log_dir = LOGS_DIR / sub_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    parts = [ts]
    if claim_id:
        parts.append(_slug(claim_id))
    if tags:
        for v in tags.values():
            if v:
                parts.append(_slug(str(v)))
    log_path = log_dir / ("-".join(parts) + ".log")

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
