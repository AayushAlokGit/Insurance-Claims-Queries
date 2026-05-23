"""Google Gemini implementation of StructuredLLM.

Uses the google-genai SDK's native pydantic schema support
(response_schema). The free tier on gemini-2.0-flash is generous
enough for dev iteration on this corpus.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypeVar, cast

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

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=response_model,
                ),
            )
        except Exception as exc:  # network, auth, rate limit
            raise LLMError(f"Gemini call failed: {exc}") from exc

        parsed = response.parsed
        if parsed is None:
            raise LLMError(
                f"Gemini returned no parsed content "
                f"(text={response.text!r})"
            )
        # google-genai returns the pydantic instance directly when
        # response_schema is a pydantic type. The cast pleases the
        # type checker without changing runtime behaviour.
        return cast(T, parsed)
