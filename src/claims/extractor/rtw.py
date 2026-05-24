"""Q1 positive-case extractor — return-to-work events.

Strict evidence-only: the LLM emits an event only when the note
explicitly states the return OCCURRED. Discussions, offers,
releases without a confirmed return, and future-dated start
dates are rejected. See extractor.md §5.4 + q1-return-to-work.md.

Discriminated-union empty case: `rtw=null` is the canonical
'no event in this note' response.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from claims.extractor._evidence import EVIDENCE_QUOTE_GUIDANCE
from claims.llm import StructuredLLM, quote_in_body
from claims.llm.base import LLMError
from claims.models import Event, Note, ReturnToWorkAttributes

_RE_RTW_SIGNAL = re.compile(
    r"\b(return(ed|ing)?\s+to\s+work|RTW|light\s+duty"
    r"|modified\s+duty|full\s+duty|released\s+to|start\s+date)\b",
    re.IGNORECASE,
)


class _ReturnToWorkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_date: date = Field(
        description="The date the claimant actually returned to work."
    )
    duty_type: Literal["modified", "full"] = Field(
        description="`modified` if returning on restrictions, "
        "`full` if returning to full duty."
    )
    role: str | None = Field(
        default=None,
        description="The role/job the claimant returned to, if named.",
    )
    evidence_quote: str = Field(
        description="A verbatim substring of the note body that "
        "justifies the extraction. Required."
    )


class RTWExtractionResponse(BaseModel):
    """Discriminated-union empty case: `rtw=null` means no event."""

    model_config = ConfigDict(extra="forbid")

    rtw: _ReturnToWorkPayload | None = None


_SYSTEM_PROMPT = """You extract RETURN-TO-WORK events from a workers'-comp claim note.

Extract a return-to-work event ONLY when the note explicitly states the claimant has RETURNED TO WORK on a specific date. The return must have OCCURRED, not be planned or offered.

POSITIVE evidence (extract):
- "EE returned to modified duty on 11/10/25 as a scheduling coordinator"
- "She started back at work on DATE in a light-duty role"
- "RTW confirmed effective DATE"

NEGATIVE — do NOT extract:
- Offers without acceptance ("light-duty offer extended effective 8/25")
- Discussions / plans ("EE will discuss RTW options at next visit")
- Provider releases without confirmation of actual return ("Dr. X released EE to modified duty" — only the release happened, not the return itself)
- Future-dated start dates after the note date (defer until a later note confirms)
- "Released to work" by a provider — that's a clearance, not a return

DUTY TYPE:
- modified — the claimant returned with restrictions, on light/sedentary/modified duty
- full    — the claimant returned to their pre-injury role with no restrictions

{evidence_guidance}

If no qualifying return-to-work event is described, return rtw: null. Do not infer from silence.""".format(
    evidence_guidance=EVIDENCE_QUOTE_GUIDANCE
)


class ReturnToWorkExtractor:
    event_type: str = "return_to_work"

    def __init__(self, llm: StructuredLLM) -> None:
        self._llm = llm

    def can_handle(self, note: Note) -> bool:
        return _RE_RTW_SIGNAL.search(note.body) is not None

    def extract(self, note: Note) -> list[Event]:
        try:
            response = self._llm.structured(
                system=_SYSTEM_PROMPT,
                user=(
                    f"Note date: {note.note_date.isoformat()}\n"
                    f"---\n{note.body}"
                ),
                response_model=RTWExtractionResponse,
            )
        except LLMError:
            return []

        payload = response.rtw
        if payload is None:
            return []
        if not quote_in_body(payload.evidence_quote, note.body):
            return []

        return [
            Event(
                event_id=str(uuid.uuid4()),
                claim_id=note.claim_id,
                event_type="return_to_work",
                event_date=payload.return_date,
                attributes=ReturnToWorkAttributes(
                    duty_type=payload.duty_type,
                    role=payload.role,
                    source_note_dates=(note.note_date,),
                    evidence_quote=payload.evidence_quote,
                ),
                extraction_method="llm",
            )
        ]
