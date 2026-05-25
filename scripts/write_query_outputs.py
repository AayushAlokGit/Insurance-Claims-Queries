"""Write the four canned queries for a claim to a single JSON file.

Output shape matches the existing
`sample_claim_notes/query_outputs/<slug>/<claim_id>.json` convention,
where `<slug>` is `<provider>-<model>` (e.g. `openai-gpt-4o-2024-08-06`
or `google-gemini-2.5-flash-lite`). Clustering by slug keeps outputs
from different LLMs from clobbering each other.

    === q1 ===
    {...}

    === q2 ===
    {...}

    ...

Run after an ingest to refresh the on-disk artifact:

    py -3.12 -m uv run python scripts/write_query_outputs.py \
        --claim-id 1-29RT --db sample_claim_notes/query_outputs/sample.db

Pass `--stdout` to write to stdout (used by the eval harness).
Pass `--provider` / `--model` to override what env says.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv

from claims.llm import provider_model_slug, resolve_model, resolve_provider
from claims.query import (
    q1_return_to_work,
    q2_appointments_attended,
    q3_reserve_changes,
    q4_schedule_to_seen,
)
from claims.store import connect

_QUERY_OUTPUTS_ROOT = REPO_ROOT / "sample_claim_notes" / "query_outputs"


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
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--claim-id", required=True)
    p.add_argument(
        "--db",
        default=None,
        help=(
            "SQLite path. Default: "
            "sample_claim_notes/query_outputs/<provider>-<model>/sample.db, "
            "matching the slug convention used by `claims ingest`."
        ),
    )
    p.add_argument(
        "--provider",
        choices=["google", "openai"],
        default=None,
        help="Override LLM_PROVIDER for the output-subdir slug.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the provider's default model for the slug.",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Explicit output JSON path. If omitted, writes to "
            "sample_claim_notes/query_outputs/<provider>-<model>/<claim_id>.json."
        ),
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Write to stdout instead of a file (used by the eval harness).",
    )
    args = p.parse_args()

    slug = provider_model_slug(args.provider, args.model)
    db_path = args.db or str(_QUERY_OUTPUTS_ROOT / slug / "sample.db")
    rendered = _render(args.claim_id, db_path)
    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    print(
        f"slug={slug} (provider={resolve_provider(args.provider)} "
        f"model={resolve_model(args.provider, args.model)}) "
        f"db={db_path}",
        file=sys.stderr,
    )
    if args.out is not None:
        out_path = Path(args.out)
    else:
        out_path = _QUERY_OUTPUTS_ROOT / slug / f"{args.claim_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
