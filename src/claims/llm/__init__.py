"""Provider-agnostic LLM layer.

Extractors depend on the StructuredLLM Protocol; the concrete
implementation is picked from the LLM_PROVIDER env var
(`google` by default, `openai` available). Each implementation
adapts its SDK to the same `structured(*, system, user,
response_model) -> T` contract — pydantic in, pydantic out.

Swapping providers is one env var. No extractor code changes.
"""

from __future__ import annotations

import os

from claims.llm.base import LLMError, StructuredLLM
from claims.llm.evidence import quote_in_body


def get_client(provider: str | None = None) -> StructuredLLM:
    """Construct an LLM client based on the LLM_PROVIDER env var.

    `provider` overrides the env var when supplied — useful in
    tests. Recognised values: `google` (default), `openai`."""
    name = (
        provider
        or os.environ.get("LLM_PROVIDER", "google")
    ).lower()
    if name == "google":
        from claims.llm.google_client import GoogleClient

        return GoogleClient()
    if name == "openai":
        from claims.llm.openai_client import OpenAIClient

        return OpenAIClient()
    raise LLMError(f"unknown LLM provider: {name!r}")


__all__ = [
    "LLMError",
    "StructuredLLM",
    "get_client",
    "quote_in_body",
]
