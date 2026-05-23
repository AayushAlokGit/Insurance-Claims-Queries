"""LLM layer tests.

Two concerns:
- quote_in_body — whitespace-tolerant substring check (the
  post-LLM safety net from extractor.md §6.3).
- get_client — factory dispatches on LLM_PROVIDER env var, fails
  cleanly on unknown providers. Does not actually construct a
  client that talks to the network.

Concrete provider implementations (GoogleClient, OpenAIClient)
are NOT exercised here — they need real SDKs to be useful and
mocking each SDK's structured-outputs internals adds maintenance
without buying much. The FakeStructuredLLM pattern below is what
extractor tests in later phases will use to stay offline.
"""

from __future__ import annotations

from typing import TypeVar

import pytest
from pydantic import BaseModel

from claims.llm import LLMError, StructuredLLM, get_client, quote_in_body

T = TypeVar("T", bound=BaseModel)


# --- quote_in_body ------------------------------------------------


def test_quote_exact_match() -> None:
    body = "EE returned to modified duty on 11/10/25."
    assert quote_in_body("returned to modified duty on 11/10/25", body)


def test_quote_whitespace_tolerant() -> None:
    """A quote with collapsed whitespace still matches a body
    that has the same content with different runs of whitespace."""
    body = "EE returned\n   to modified\tduty on 11/10/25."
    assert quote_in_body("returned to modified duty on 11/10/25", body)


def test_quote_not_present_rejected() -> None:
    body = "EE was discussing modified duty options."
    assert not quote_in_body(
        "returned to modified duty on 11/10/25", body
    )


def test_empty_quote_rejected() -> None:
    assert not quote_in_body("", "anything")


# --- get_client factory ------------------------------------------


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(LLMError):
        get_client(provider="claude")


def test_factory_dispatches_to_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing GoogleClient without a key raises LLMError —
    we use that as the signal that the factory dispatched to the
    right class, without needing to actually authenticate."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GOOGLE_API_KEY"):
        get_client(provider="google")


def test_factory_dispatches_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        get_client(provider="openai")


# --- FakeStructuredLLM (the test-harness pattern) ----------------


class _Reply(BaseModel):
    answer: str


class _FakeLLM:
    """Reusable across extractor tests. Returns a queued list of
    pydantic instances in order. Asserts the queue isn't exhausted."""

    model: str = "fake"

    def __init__(self, replies: list[BaseModel]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str, type[BaseModel]]] = []

    def structured(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
    ) -> T:
        if not self._replies:
            raise AssertionError("FakeLLM exhausted")
        reply = self._replies.pop(0)
        self.calls.append((system, user, response_model))
        # In real tests the queued replies are already the right
        # type; the cast is unsafe-but-fine in a stub.
        return reply  # type: ignore[return-value]


def test_fake_structured_llm_round_trip() -> None:
    """Sanity check that the FakeLLM satisfies StructuredLLM and
    that an extractor-style call site works against it."""
    fake = _FakeLLM([_Reply(answer="ok")])
    client: StructuredLLM = fake
    result = client.structured(
        system="sys", user="usr", response_model=_Reply
    )
    assert result.answer == "ok"
    assert fake.calls == [("sys", "usr", _Reply)]
