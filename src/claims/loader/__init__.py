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
from claims.loader.metadata import ClaimMetadata, infer_claim_metadata

__all__ = [
    "ClaimMetadata",
    "LoadedClaim",
    "LoaderError",
    "RawClaimHeader",
    "RawNoteBlock",
    "infer_claim_metadata",
    "parse_file",
]
