"""Shared `evidence_quote` guidance, embedded into every LLM
extractor prompt. One source of truth for what makes a quote
acceptable — drift between extractors would mean Q1, Q2, and Q4
each have their own private definition of "evidence", which is
exactly what audit can't tolerate.

The rules generalize the n0005 failure mode (LLM picked the
templated `Dr. Harmon Clinic: Orthopedic & Spine Associates`
parties fragment as evidence for an attended appointment — a
verbatim substring, yes, but it does not prove the event).
"""

from __future__ import annotations

EVIDENCE_QUOTE_GUIDANCE = """EVIDENCE_QUOTE — required on every emitted event. Quote QUALITY matters as much as presence:

1. VERBATIM — a single contiguous substring of the note body, character-for-character. No paraphrasing, no joining of non-adjacent fragments, no ellipses, no normalization of whitespace or punctuation.

2. JUSTIFIES THE EVENT — the quote must contain the words that prove BOTH (a) the event occurred (or is scheduled, or is terminal — whatever this extractor emits) AND (b) the key classification fields you populated (status, duty_type, reason, date, etc.). A fragment that only names the parties, the clinic, or a header field — without an action verb or its templated equivalent — is INSUFFICIENT. Pick a clause that contains the operative verb (`attended`, `missed`, `cancelled`, `returned`, `released`, `scheduled`, `declared`, `closed`, etc.) or its TEMPLATED equivalent — a `Date of Appointment: X` or `DOS: X` line IS the templated equivalent of "attended" when it heads a clinical block; that single line alone is a sufficient quote for an attended templated visit.

   Insufficient: `Dr. Harmon Clinic: Orthopedic & Spine Associates`     (names parties only)
   Sufficient:   `EE attended scheduled follow-up with Dr. Harmon on 8/20`
   Sufficient:   `Date of Appointment: 11-14-25`                       (templated header — equivalent to "attended" when followed by clinical content)
   Sufficient:   `DOS: 4-2-25`                                          (templated header — same)

3. MINIMAL — prefer the smallest sentence or clause that carries the proof. Do not quote a whole paragraph when one sentence suffices.

4. ONE EVENT PER QUOTE — each emitted event carries the quote for THAT event only. Do not bundle evidence for multiple events into a single quote.

If no single substring of the body satisfies (1) AND (2), DO NOT EMIT the event. Under-emission is recoverable; an unsupported event is not."""
