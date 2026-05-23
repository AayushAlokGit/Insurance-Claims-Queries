"""Retry policy for LLM client calls. See DD-014.

Both providers exhibit transient failure modes — Gemini 503
UNAVAILABLE on demand spikes, OpenAI 429 rate-limits, generic
network blips. The single-attempt policy in Phase 8 lost data
silently when these fired. Phase 11.5+ uses bounded retry with
exponential backoff: up to 3 attempts total with a short cap,
then we fall back to the existing "log warning + return []"
behavior in the extractor.

Non-transient errors (schema validation, refusal, bad input)
should NOT retry. Each client classifies its own errors and
raises a sentinel `_NonRetryableError` to opt out — the rest
default to retryable.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

_log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
# Base delay per retry attempt — actual sleep is base × (1 ± _JITTER).
# Jitter desynchronizes retries from concurrent callers hitting the
# same provider outage so we don't all hammer the API at the same
# moment after backoff expires.
_BACKOFF_SECONDS = (1.0, 3.0)  # base for first sleep, second sleep
_JITTER = 0.5
T = TypeVar("T")


def _jittered_delay(base: float) -> float:
    return base * random.uniform(1.0 - _JITTER, 1.0 + _JITTER)


class NonRetryableError(Exception):
    """Wrap any exception that callers want to skip retrying."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def with_retry(fn: Callable[[], T], *, label: str) -> T:
    """Invoke `fn` with up to _MAX_ATTEMPTS attempts. Each
    attempt that raises (non-NonRetryableError) sleeps and
    retries; final failure re-raises the last exception.

    `label` is a short identifier for log lines (e.g. the model
    name) so retries are traceable in the per-run log."""
    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except NonRetryableError as exc:
            raise exc.original from None
        except Exception as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                _log.warning(
                    "%s: final attempt %d/%d failed: %s",
                    label,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                )
                raise
            sleep_s = _jittered_delay(_BACKOFF_SECONDS[attempt - 1])
            _log.info(
                "%s: attempt %d/%d failed (%s); retrying in %.1fs",
                label,
                attempt,
                _MAX_ATTEMPTS,
                exc,
                sleep_s,
            )
            time.sleep(sleep_s)
    # Unreachable but keeps the type checker honest.
    assert last_exc is not None
    raise last_exc
