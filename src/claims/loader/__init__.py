"""Claim notes file loader.

Splits a raw claim notes file into a header + a list of
RawNoteBlocks. No interpretation — that's the Normalizer's job.
"""

from claims.loader.loader import (
    LoadedClaim,
    LoaderError,
    RawClaimHeader,
    RawNoteBlock,
    parse_file,
)

__all__ = [
    "LoadedClaim",
    "LoaderError",
    "RawClaimHeader",
    "RawNoteBlock",
    "parse_file",
]
