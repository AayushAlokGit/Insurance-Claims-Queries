"""Q3 reserve-change extractor. Pure regex, no LLM (DD-005 + §8
of extractor.md). Triggers only on Activity: Reserving notes —
Resolution Strategy summaries restate balances and would
double-count if matched.

The previous_amount and delta are intentionally left None here:
they're cross-note state and belong to the Resolver
(q3-reserve-changes.md §6).
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from claims.models import Event, Note, ReserveChangeAttributes

_PATTERN = re.compile(
    r"(?P<coverage>Indemnity|Expense-Litigation)\s+for\s+"
    r"\((?P<num>\d+)\)\s+(?P<sub_bucket>[A-Za-z\s\-]+?)\s+"
    r"Changed to\s+\$(?P<amount>[\d,]+\.\d{2})"
)


class ReserveChangeExtractor:
    event_type: str = "reserve_change"

    def can_handle(self, note: Note) -> bool:
        return note.activity == "Reserving"

    def extract(self, note: Note) -> list[Event]:
        events: list[Event] = []
        for m in _PATTERN.finditer(note.body):
            bucket = (
                f"{m.group('coverage')} ({m.group('num')}) "
                f"{m.group('sub_bucket').strip()}"
            )
            new_amount = Decimal(m.group("amount").replace(",", ""))
            events.append(
                Event(
                    event_id=str(uuid.uuid4()),
                    claim_id=note.claim_id,
                    event_type="reserve_change",
                    event_date=note.note_date,
                    attributes=ReserveChangeAttributes(
                        bucket=bucket,
                        new_amount=new_amount,
                        author=note.author,
                    ),
                    extraction_method="rule",
                )
            )
        return events
