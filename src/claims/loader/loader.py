"""Per-claim file loader.

Splits a claim notes file into a claim header (claim_id, account)
and a list of raw note blocks. No interpretation — header strings
and body bytes are passed through as-is. The Normalizer (Phase 4)
handles the messy bits.

The two sample files use slightly different surface forms:

    Claim #: 1-29RT
    Account #: 2100000000 - Company WW
    Date: 01/08/2026 10:02am CT | Activity: ... | Noted By: M.H.

vs.

    # Claim #: 2-248KR
    **Account #:** 0005 - Group W
    **Date: 08/29/2025 12:22pm CT | Activity: ... | Noted By: R.N.**

One header regex covers both by tolerating optional markdown
bolding. The claim-level metadata is extracted from the first
matching line each — subsequent occurrences (e.g. `Claim #:`
inside a note body) are ignored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# A note header line. Matches both unbolded (file 1) and
# markdown-bolded (file 2) forms.
_NOTE_HEADER_RE = re.compile(
    r"^\*{0,2}Date:\s.*\|\s*Activity:\s.*\|\s*Noted By:\s.*?\*{0,2}\s*$",
    re.MULTILINE,
)

# Claim-level metadata anchors. Tolerates leading `# ` markdown
# heading and `**...**` wrapping around the label.
_CLAIM_ID_RE = re.compile(
    r"^\s*#?\s*\*{0,2}Claim\s*#:\*{0,2}\s*(.+?)\s*$",
    re.MULTILINE,
)
_ACCOUNT_RE = re.compile(
    r"^\s*\*{0,2}Account\s*#:\*{0,2}\s*(.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class RawNoteBlock:
    """A single note before normalization.

    `raw_header` is the original header line (markdown wrapping
    intact). `raw_body` is every line between this header and the
    next, including blank lines and any `---` separators — the
    Normalizer will clean those. `source_offset` is the byte
    offset of the header line in the source file."""

    claim_id: str
    raw_header: str
    raw_body: str
    source_offset: int


@dataclass(frozen=True)
class RawClaimHeader:
    claim_id: str
    account_raw: str | None
    source_file: str


@dataclass(frozen=True)
class LoadedClaim:
    header: RawClaimHeader
    notes: list[RawNoteBlock]


class LoaderError(ValueError):
    """Raised when a file cannot be parsed into a claim shape at
    all — missing claim_id, no notes detected, etc."""


def parse_file(path: str) -> LoadedClaim:
    """Read a claim notes file and split it into header + notes.

    Notes are returned in source order (i.e. as they appear in the
    file). No date sorting happens here — the file's own ordering
    is preserved verbatim."""

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    claim_id_match = _CLAIM_ID_RE.search(text)
    if claim_id_match is None:
        raise LoaderError(f"no 'Claim #:' line found in {path!r}")
    claim_id = claim_id_match.group(1).strip()

    account_match = _ACCOUNT_RE.search(text)
    account_raw = account_match.group(1).strip() if account_match else None

    header_matches = list(_NOTE_HEADER_RE.finditer(text))
    if not header_matches:
        raise LoaderError(f"no note headers found in {path!r}")

    notes: list[RawNoteBlock] = []
    for i, m in enumerate(header_matches):
        header_line = m.group(0).rstrip("\r\n")
        body_start = m.end()
        body_end = (
            header_matches[i + 1].start()
            if i + 1 < len(header_matches)
            else len(text)
        )
        raw_body = text[body_start:body_end].strip("\n")
        notes.append(
            RawNoteBlock(
                claim_id=claim_id,
                raw_header=header_line,
                raw_body=raw_body,
                source_offset=m.start(),
            )
        )

    return LoadedClaim(
        header=RawClaimHeader(
            claim_id=claim_id,
            account_raw=account_raw,
            source_file=os.path.basename(path),
        ),
        notes=notes,
    )
