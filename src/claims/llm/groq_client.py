"""Groq implementation of StructuredLLM.

Uses the official `groq` Python SDK. Unlike the OpenAI SDK, Groq
does not expose a pydantic `.parse()` helper, so we send a
`response_format={"type": "json_schema", ...}` derived from the
pydantic model and validate the returned JSON ourselves — same
shape as the Google client. Strict-mode json_schema is supported
on Groq's gpt-oss, kimi-k2, and llama-4 model families; picking a
non-compatible model will surface as an LLMError on the first call.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from claims.llm.base import LLMError
from claims.llm.retry import with_retry

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from groq import Groq as _Groq

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "openai/gpt-oss-120b"

# Hard per-request ceiling. Same rationale as the other clients —
# bound stuck sockets so the retry layer can take over.
_REQUEST_TIMEOUT_S = 60.0


class GroqClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        _client: "_Groq | None" = None,
    ) -> None:
        self.model: str = model or os.environ.get(
            "GROQ_MODEL", DEFAULT_MODEL
        )
        if _client is not None:
            self._client = _client
        else:
            from groq import Groq

            key = api_key or os.environ.get("GROQ_API_KEY")
            if not key:
                raise LLMError(
                    "GROQ_API_KEY is not set; cannot construct GroqClient"
                )
            self._client = Groq(api_key=key)

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
    ) -> T:
        schema = response_model.model_json_schema()
        # Groq's strict json_schema mode rejects `$schema` and `title`
        # at the top level — strip them. `additionalProperties` IS
        # accepted (and required for strict mode), so unlike Google
        # we leave it alone.
        schema.pop("$schema", None)
        schema.pop("title", None)
        # Groq strict mode additionally demands that every key in
        # `properties` appears in `required` — pydantic only lists
        # non-default fields, so we promote all of them. This matches
        # how OpenAI's strict mode internally rewrites the schema;
        # Groq just doesn't do it for you.
        _force_all_required(schema)

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": schema,
                "strict": True,
            },
        }

        _log.debug(
            "groq call model=%s schema=%s user_chars=%d",
            self.model,
            response_model.__name__,
            len(user),
        )

        def _call():
            return self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                temperature=0.0,
                timeout=_REQUEST_TIMEOUT_S,
            )

        try:
            response = with_retry(_call, label=f"groq/{self.model}")
        except Exception as exc:
            raise LLMError(f"Groq call failed: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            _log.warning("groq refused: %s", message.refusal)
            raise LLMError(f"Groq refused: {message.refusal}")
        text = message.content
        if not text:
            _log.warning("groq returned empty content")
            raise LLMError("Groq returned empty content")
        usage = getattr(response, "usage", None)
        if usage is not None:
            _log.debug(
                "groq ok prompt_tokens=%s completion_tokens=%s",
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
            )
        try:
            return response_model.model_validate_json(text)
        except Exception as exc:
            _log.warning(
                "groq response failed validation for %s: %s",
                response_model.__name__,
                exc,
            )
            raise LLMError(
                f"Groq response did not match {response_model.__name__}: "
                f"{exc}; text={text!r}"
            ) from exc


def _force_all_required(schema: object) -> None:
    """Walk the JSON schema and, for every object node that has a
    `properties` dict, set `required` to the full list of property
    names. Groq's strict mode rejects schemas where any property is
    not in `required`; pydantic's default emission only lists
    non-default fields, so we promote the rest here. Recurses into
    nested objects via `$defs`, `properties.*`, and `items`."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            schema["required"] = list(props.keys())
        for value in schema.values():
            _force_all_required(value)
    elif isinstance(schema, list):
        for value in schema:
            _force_all_required(value)
