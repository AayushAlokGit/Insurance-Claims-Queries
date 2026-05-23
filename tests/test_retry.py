"""Retry helper tests (DD-014)."""

from __future__ import annotations

import pytest

from claims.llm.retry import NonRetryableError, with_retry


def test_succeeds_first_attempt() -> None:
    calls = 0

    def fn() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert with_retry(fn, label="t") == 42
    assert calls == 1


def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two transient failures, then success on attempt 3."""
    monkeypatch.setattr("time.sleep", lambda s: None)  # speed up
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "ok"

    assert with_retry(fn, label="t") == "ok"
    assert attempts == [1, 2, 3]


def test_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda s: None)
    attempts: list[int] = []

    def fn() -> None:
        attempts.append(len(attempts) + 1)
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
        with_retry(fn, label="t")
    assert attempts == [1, 2, 3]


def test_non_retryable_skips_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors wrapped in NonRetryableError unwrap and propagate
    immediately without consuming attempts."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    attempts: list[int] = []

    class _DeterministicError(ValueError):
        pass

    def fn() -> None:
        attempts.append(len(attempts) + 1)
        raise NonRetryableError(_DeterministicError("schema bad"))

    with pytest.raises(_DeterministicError, match="schema bad"):
        with_retry(fn, label="t")
    assert attempts == [1]
