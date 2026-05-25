"""Provider-agnostic LLM layer.

Extractors depend on the StructuredLLM Protocol; the concrete
implementation is picked from the LLM_PROVIDER env var. Each
implementation adapts its SDK to the same `structured(*, system,
user, response_model) -> T` contract — pydantic in, pydantic out.

Swapping providers is one env var. No extractor code changes.

Supported providers: `google` (default), `openai`, `groq`.
"""

from __future__ import annotations

import os
import re

from claims.llm.base import LLMError, StructuredLLM
from claims.llm.evidence import quote_in_body


def get_client(provider: str | None = None) -> StructuredLLM:
    """Construct an LLM client based on the LLM_PROVIDER env var.

    `provider` overrides the env var when supplied — useful in
    tests. Recognised values: `google` (default), `openai`, `groq`."""
    name = resolve_provider(provider)
    if name == "google":
        from claims.llm.google_client import GoogleClient

        return GoogleClient()
    if name == "openai":
        from claims.llm.openai_client import OpenAIClient

        return OpenAIClient()
    if name == "groq":
        from claims.llm.groq_client import GroqClient

        return GroqClient()
    raise LLMError(f"unknown LLM provider: {name!r}")


def resolve_provider(provider: str | None = None) -> str:
    """Lowercased provider name. Defaults to LLM_PROVIDER env, then `google`."""
    return (provider or os.environ.get("LLM_PROVIDER", "google")).lower()


def resolve_model(provider: str | None = None, model: str | None = None) -> str:
    """The model name a freshly constructed client would use, without
    actually constructing one (avoids needing an API key for the slug).
    Mirrors the defaults in `openai_client.py` and `google_client.py`."""
    if model:
        return model
    name = resolve_provider(provider)
    if name == "google":
        from claims.llm.google_client import DEFAULT_MODEL as G

        return os.environ.get("GOOGLE_MODEL", G)
    if name == "openai":
        from claims.llm.openai_client import DEFAULT_MODEL as O

        return os.environ.get("OPENAI_MODEL", O)
    if name == "groq":
        from claims.llm.groq_client import DEFAULT_MODEL as Gr

        return os.environ.get("GROQ_MODEL", Gr)
    raise LLMError(f"unknown LLM provider: {name!r}")


_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def provider_model_slug(
    provider: str | None = None, model: str | None = None
) -> str:
    """A filesystem-safe `<provider>-<model>` slug used to cluster
    per-run artifacts (query-output JSON files, SQLite DBs, log
    filenames) by which LLM produced them. E.g.
    `openai-gpt-4o-2024-08-06` or `google-gemini-2.5-flash-lite`."""
    p = resolve_provider(provider)
    m = resolve_model(provider, model)
    return _SLUG_SAFE.sub("-", f"{p}-{m}").strip("-")


def rule_only_slug() -> str:
    """Slug used for the `--no-llm` ingest path, which has no model."""
    return "rule-only"


__all__ = [
    "LLMError",
    "StructuredLLM",
    "get_client",
    "provider_model_slug",
    "quote_in_body",
    "resolve_model",
    "resolve_provider",
    "rule_only_slug",
]
