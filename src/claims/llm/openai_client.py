"""OpenAI implementation of StructuredLLM.

Uses the structured-outputs API: response_format with a pydantic
model, strict=True. The OpenAI SDK's parse() helper does the
JSON-schema generation and validation for us."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import BaseModel

from claims.llm.base import LLMError

if TYPE_CHECKING:
    from openai import OpenAI as _OpenAI

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gpt-4o-2024-08-06"


class OpenAIClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        _client: "_OpenAI | None" = None,
    ) -> None:
        self.model: str = model or os.environ.get(
            "OPENAI_MODEL", DEFAULT_MODEL
        )
        if _client is not None:
            self._client = _client
        else:
            from openai import OpenAI

            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise LLMError(
                    "OPENAI_API_KEY is not set; cannot construct OpenAIClient"
                )
            self._client = OpenAI(api_key=key)

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
    ) -> T:
        try:
            response = self._client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_model,
            )
        except Exception as exc:
            raise LLMError(f"OpenAI call failed: {exc}") from exc

        message = response.choices[0].message
        if message.refusal:
            raise LLMError(f"OpenAI refused: {message.refusal}")
        if message.parsed is None:
            raise LLMError("OpenAI returned no parsed content")
        return cast(T, message.parsed)
