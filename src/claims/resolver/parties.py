"""Party-set normalization and overlap. See DD-016.

Replaces the previous provider canonicalization heuristic
(`provider.py`, which classified person-vs-org from a single
string and grabbed `.split()[-1]` for persons). The new design
moves disambiguation upstream: the LLM emits a `parties` list
in which each entry is already a clean single-entity name with
honorifics, degree suffixes, and location suffixes stripped.
The resolver's job here is only to apply LOSSLESS deterministic
normalization (case, whitespace, residual punctuation) and check
set overlap.

No fuzzy / similarity matching — see DD-016 "Rejected".
Surnames are too short and org names too boilerplate-heavy for
edit distance to be safe; silent false merges in entity
resolution are strictly worse than visible duplicates.

Residual same-entity variants that survive normalization
(e.g. nicknames, common typos) are out of scope for the MVP and
would be handled by an explicit alias table when the corpus
shows it matters.
"""

from __future__ import annotations

import re

# Lossless transformations applied to both sides before exact
# comparison. Each must preserve meaning — none of these change
# WHICH entity the string identifies.
_HONORIFIC_RE = re.compile(
    r"^(?:dr|mr|mrs|ms|prof|rev|sr|jr)\.?\s+", re.IGNORECASE
)
_DEGREE_SUFFIX_RE = re.compile(
    r",?\s*(?:MD|DO|NP|PA|PT|DPT|OT|DC|PhD|DDS|RN|LCSW)\.?$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[.,]")
_WS_RE = re.compile(r"\s+")


def normalize_party(raw: str | None) -> str | None:
    """Return a normalized form for exact equality comparison.

    None / empty / whitespace-only → None (no identifiable party).
    Anything else gets lowercased, ampersands → 'and', honorifics
    and degree suffixes stripped, punctuation removed, whitespace
    collapsed. All transformations preserve identity.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()
    # Strip honorific even if the LLM forgot. Repeated for chained
    # forms like "Dr. Mr. X" (defensive; cheap).
    while True:
        new = _HONORIFIC_RE.sub("", s)
        if new == s:
            break
        s = new
    s = _DEGREE_SUFFIX_RE.sub("", s).strip()
    s = s.replace("&", "and")
    s = _PUNCT_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def parties_overlap(
    a: tuple[str, ...] | list[str],
    b: tuple[str, ...] | list[str],
) -> bool:
    """DD-016 set-overlap rule for the appointment merge key.

    Returns True if the two parties lists share ≥1 entity (under
    normalization), OR if at least one side is empty / fully
    normalizes to nothing. The empty-side branch is deliberate:
    when one note names parties and the other doesn't (Marker
    extractor saw only a date, or the LLM found no identifiable
    party), we don't BLOCK the merge — date alone carries it.
    Returns False only when both sides have parties and none
    overlap.

    DD-016 SAME-FACILITY-SAME-DAY FAILURE MODE.
    `≥1 shared entity` is a deliberately loose rule: a shared
    facility is treated as identifying, not just confirming.
    Consequence: one claimant seeing two different clinicians on
    the same day at the same practice (e.g. Dr. Caldwell *and*
    Dr. Farano, both at Spine & Neurology Group) overlap on the
    shared facility name and MERGE into one appointment event
    incorrectly. Both clinicians remain visible in the merged
    `parties` list (the collapse is auditable, not silent), but
    Q2's count is off by one for that day. Bounded and inspectable
    — see DD-016 "Known residual failure mode" and the comment at
    the top of `appointments.py` for the conditions under which
    this can fire and the local fix (tighten the rule to "shared
    PERSON party") if it ever shows up in a real corpus.
    """
    a_norm = {n for p in a if (n := normalize_party(p)) is not None}
    b_norm = {n for p in b if (n := normalize_party(p)) is not None}
    if not a_norm or not b_norm:
        return True
    return bool(a_norm & b_norm)
