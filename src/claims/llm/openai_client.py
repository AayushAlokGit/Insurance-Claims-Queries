"""OpenAI implementation of StructuredLLM.

Uses the structured-outputs API: response_format with a pydantic
model, strict=True. The OpenAI SDK's parse() helper does the
JSON-schema generation and validation for us."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import BaseModel

from claims.llm.base import LLMError
from claims.llm.retry import with_retry

_log = logging.getLogger(__name__)

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
        _log.debug(
            "openai call model=%s schema=%s user_chars=%d",
            self.model,
            response_model.__name__,
            len(user),
        )

        def _call():
            return self._client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_model,
                temperature=0.0,
            )

        try:
            response = with_retry(_call, label=f"openai/{self.model}")
        except Exception as exc:
            raise LLMError(f"OpenAI call failed: {exc}") from exc

        message = response.choices[0].message
        if message.refusal:
            _log.warning("openai refused: %s", message.refusal)
            raise LLMError(f"OpenAI refused: {message.refusal}")
        if message.parsed is None:
            _log.warning("openai returned no parsed content")
            raise LLMError("OpenAI returned no parsed content")
        usage = getattr(response, "usage", None)
        if usage is not None:
            _log.debug(
                "openai ok prompt_tokens=%s completion_tokens=%s",
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
            )
        return cast(T, message.parsed)
