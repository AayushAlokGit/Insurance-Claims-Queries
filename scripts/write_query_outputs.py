"""Write the four canned queries for a claim to a single JSON file.

Output shape matches the existing
`sample_claim_notes/query_outputs/<claim_id>.json` convention:

    === q1 ===
    {...}

    === q2 ===
    {...}

    ...

Run after an ingest to refresh the on-disk artifact:

    py -3.12 -m uv run python scripts/write_query_outputs.py \
        --claim-id 1-29RT --db sample_claim_notes/query_outputs/sample.db

By default writes to `sample_claim_notes/query_outputs/<claim_id>.json`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from claims.query import (
    q1_return_to_work,
    q2_appointments_attended,
    q3_reserve_changes,
    q4_schedule_to_seen,
)
from claims.store import connect

_DEFAULT_OUT_DIR = REPO_ROOT / "sample_claim_notes" / "query_outputs"


def _render(claim_id: str, db: str) -> str:
    conn = connect(db)
    parts: list[str] = []
    for tag, fn in (
        ("q1", q1_return_to_work),
        ("q2", q2_appointments_attended),
        ("q3", q3_reserve_changes),
        ("q4", q4_schedule_to_seen),
    ):
        result = fn(conn, claim_id)
        parts.append(f"=== {tag} ===")
        parts.append(result.model_dump_json(indent=2))
        parts.append("")
    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--claim-id", required=True)
    p.add_argument(
        "--db",
        default=str(_DEFAULT_OUT_DIR / "sample.db"),
        help="SQLite path (default: %(default)s).",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Output JSON path. Defaults to "
            "sample_claim_notes/query_outputs/<claim_id>.json."
        ),
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Write to stdout instead of a file (used by the eval harness).",
    )
    args = p.parse_args()

    rendered = _render(args.claim_id, args.db)
    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    out_path = (
        Path(args.out) if args.out else _DEFAULT_OUT_DIR / f"{args.claim_id}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
