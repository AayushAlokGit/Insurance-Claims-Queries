"""Summarize the four canned queries for a claim against a DB.

Calls each query directly (no JSON round-trip through files) so
we sidestep PowerShell's UTF-16 redirect default. Run after an
ingest:

    py -3.12 -m uv run python scripts/summarize_queries.py \
        --claim-id 1-29RT --db demo.db
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--claim-id", required=True)
    p.add_argument("--db", default="demo.db")
    args = p.parse_args()

    conn = connect(args.db)

    print(f"=== {args.claim_id} ===")

    q1 = q1_return_to_work(conn, args.claim_id)
    print(f"\nQ1: {q1.status}")
    if q1.status == "returned":
        print(f"  days={q1.days}  rtw_date={q1.rtw_date}  duty={q1.duty_type}")
    elif q1.status == "never_returned":
        print(f"  reason={q1.reason}  terminal_date={q1.terminal_date}")
    else:
        print(f"  days_open={q1.days_open}")

    q2 = q2_appointments_attended(conn, args.claim_id)
    print(f"\nQ2: attended count = {q2.count}")
    providers = {}
    for a in q2.appointments:
        providers[a.provider] = providers.get(a.provider, 0) + 1
    for provider, n in sorted(providers.items(), key=lambda x: -x[1]):
        print(f"  {provider!r}: {n}")

    q3 = q3_reserve_changes(conn, args.claim_id)
    print("\nQ3: reserve changes")
    total = 0
    for b in q3.buckets:
        print(f"  {b.bucket:50s} count={b.count} net=${b.net}")
        total += b.count
    print(f"  total events: {total}")

    q4 = q4_schedule_to_seen(conn, args.claim_id)
    print(f"\nQ4: merged scheduled-then-seen visits = {len(q4.per_visit)}")
    print(
        f"  median={q4.lag.median}d  mean={q4.lag.mean}d  p90={q4.lag.p90}d"
    )
    print("  per-visit lags (sorted):")
    for v in sorted(q4.per_visit, key=lambda v: v.lag_days):
        print(
            f"    {v.provider!r:35s} notice={v.scheduled_notice_date} "
            f"booked={v.scheduled_for_date} occurred={v.occurred_on} "
            f"lag={v.lag_days}d"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
