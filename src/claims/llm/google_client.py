"""Google Gemini implementation of StructuredLLM.

Uses the google-genai SDK's native pydantic schema support
(response_schema). The free tier on gemini-2.0-flash is generous
enough for dev iteration on this corpus.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from claims.llm.base import LLMError

if TYPE_CHECKING:  # avoid import cost when the provider isn't used
    from google.genai import Client as _Client

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-2.0-flash"


class GoogleClient:
    """Lazily-constructed google-genai client wrapper.

    Reads GOOGLE_API_KEY from env. The model name comes from the
    constructor or GOOGLE_MODEL env var, defaulting to
    gemini-2.0-flash.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        _client: "_Client | None" = None,
    ) -> None:
        self.model: str = model or os.environ.get(
            "GOOGLE_MODEL", DEFAULT_MODEL
        )
        if _client is not None:
            self._client = _client
        else:
            from google import genai

            key = api_key or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise LLMError(
                    "GOOGLE_API_KEY is not set; cannot construct GoogleClient"
                )
            self._client = genai.Client(api_key=key)

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
    ) -> T:
        from google.genai import types

        # Google's response_schema is a subset of JSON Schema — it
        # does NOT accept `additionalProperties`, but pydantic emits
        # it whenever extra="forbid" is set. Strip it (recursively)
        # before sending, then parse the JSON text ourselves.
        schema = _strip_unsupported(response_model.model_json_schema())

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except Exception as exc:  # network, auth, rate limit
            raise LLMError(f"Gemini call failed: {exc}") from exc

        text = response.text
        if not text:
            raise LLMError(
                f"Gemini returned empty response (response={response!r})"
            )
        try:
            return response_model.model_validate_json(text)
        except Exception as exc:
            raise LLMError(
                f"Gemini response did not match {response_model.__name__}: "
                f"{exc}; text={text!r}"
            ) from exc


_UNSUPPORTED_KEYS: tuple[str, ...] = (
    "additionalProperties",
    "$schema",
    "title",
)


def _strip_unsupported(schema: object) -> object:
    """Recursively remove keys that Google's response_schema subset
    does not accept. `additionalProperties` is the load-bearing one
    (pydantic emits it for extra="forbid"); `$schema` and `title`
    are harmless extras that also aren't part of Google's proto."""
    if isinstance(schema, dict):
        for key in _UNSUPPORTED_KEYS:
            schema.pop(key, None)
        for value in schema.values():
            _strip_unsupported(value)
    elif isinstance(schema, list):
        for value in schema:
            _strip_unsupported(value)
    return schema
