"""Q1 negative-case extractor — RTW terminal events (DD-011).

A first-class "never returned" event, not the absence of a
positive one. Emitted only when the note has positive evidence
of a terminal state: PTD, deceased, separated, or claim closed
without RTW. See extractor.md §5.5 + DD-011.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from claims.llm import StructuredLLM, quote_in_body
from claims.llm.base import LLMError
from claims.models import Event, Note, RTWTerminalAttributes

_RE_TERMINAL_SIGNAL = re.compile(
    r"\b(never\s+returned|permanent\s+total|PTD|deceased|passed\s+away"
    r"|terminated|separated|claim\s+closed|lump[- ]sum\s+settlement)\b",
    re.IGNORECASE,
)


class _TerminalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["ptd", "deceased", "separated", "closed_no_rtw"] = (
        Field(
            description=(
                "ptd: permanent total disability. "
                "deceased: claimant died. "
                "separated: employment ended without an RTW. "
                "closed_no_rtw: claim settled/closed with no RTW on record."
            )
        )
    )
    context: str | None = Field(
        default=None,
        description="Short human-readable context (PPD %, settlement type, etc.)",
    )
    evidence_quote: str = Field(
        description="A verbatim substring of the note body. Required."
    )


class RTWTerminalExtractionResponse(BaseModel):
    """Discriminated-union empty case: `rtw_terminal=null` means no event."""

    model_config = ConfigDict(extra="forbid")

    rtw_terminal: _TerminalPayload | None = None


_SYSTEM_PROMPT = """You extract RTW-TERMINAL events from a workers'-comp claim note.

A terminal event records that the claimant will NOT be returning to work — a definitive, first-class negative outcome. Extract only when the note has POSITIVE EVIDENCE of one of these states:

REASON:
- ptd            — declared permanent total disability
- deceased       — the claimant died
- separated      — employment ended (terminated, resigned, retired) without an RTW
- closed_no_rtw  — claim closed/settled (lump-sum, full and final, etc.) with no RTW on record

NEGATIVE — do NOT extract:
- "Has not yet returned to work" → still pending, not terminal
- General discussion of permanency without a declaration ("permanency stipulation in negotiation" — not yet declared)
- "May not be able to return to her prior role" → speculation, not a terminal declaration

EVIDENCE_QUOTE is REQUIRED — a verbatim substring of the body.

At most one terminal event per note. If no terminal state is positively declared, return rtw_terminal: null. Do not infer from silence."""


class RtwTerminalExtractor:
    event_type: str = "rtw_terminal"

    def __init__(self, llm: StructuredLLM) -> None:
        self._llm = llm

    def can_handle(self, note: Note) -> bool:
        return _RE_TERMINAL_SIGNAL.search(note.body) is not None

    def extract(self, note: Note) -> list[Event]:
        try:
            response = self._llm.structured(
                system=_SYSTEM_PROMPT,
                user=(
                    f"Note date: {note.note_date.isoformat()}\n"
                    f"---\n{note.body}"
                ),
                response_model=RTWTerminalExtractionResponse,
            )
        except LLMError:
            return []

        payload = response.rtw_terminal
        if payload is None:
            return []
        if not quote_in_body(payload.evidence_quote, note.body):
            return []

        return [
            Event(
                event_id=str(uuid.uuid4()),
                claim_id=note.claim_id,
                event_type="rtw_terminal",
                event_date=note.note_date,
                attributes=RTWTerminalAttributes(
                    reason=payload.reason,
                    context=payload.context,
                ),
                extraction_method="llm",
            )
        ]
