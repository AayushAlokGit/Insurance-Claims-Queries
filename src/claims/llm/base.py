"""The provider-agnostic LLM interface.

Every extractor depends on this Protocol, not on a concrete
OpenAI/Gemini client. The factory in `claims.llm.__init__`
picks the implementation from the LLM_PROVIDER env var.

The contract is intentionally narrow: schema-in, instance-out.
extractor.md §6 enumerates the four principles every LLM call
must obey — schema-constrained output, discriminated-union
empty case, required evidence_quote with substring check, and
explicit negative prompt rules. The first is enforced by this
interface (the response_model is a pydantic type and the
provider's structured-outputs mode validates against it); the
other three live in the prompt and the extractor's post-
processing.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Anything went wrong on a structured() call — refusal,
    schema mismatch the SDK couldn't recover from, transport
    failure. Extractors should let this bubble; the orchestrator
    catches and skips the note."""


class StructuredLLM(Protocol):
    """A schema-constrained, single-turn chat client."""

    model: str

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
    ) -> T: ...
