"""Streaming strict UTF-8 preflight (frozen specs 1/14).

KI only detects and reports; it never transcodes and never guesses an
encoding that cannot be proven. detected_encoding values:
- "UTF-8" BOM          -> allowed (returns None from preflight_utf8)
- "UTF-32BE"/"UTF-32LE"/"UTF-16BE"/"UTF-16LE" BOM -> rejected, proven
- "unknown"            -> no BOM, strict UTF-8 decode failed (no guessing)
"""

from __future__ import annotations

import codecs
from pathlib import Path

# frozen spec 14: UTF-32 before UTF-16 — FF FE 00 00 (UTF-32LE) is prefixed
# by FF FE (UTF-16LE), so longest match must win.
BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xFE\xFF", "UTF-32BE"),
    (b"\xFF\xFE\x00\x00", "UTF-32LE"),
    (b"\xFE\xFF", "UTF-16BE"),
    (b"\xFF\xFE", "UTF-16LE"),
    (b"\xEF\xBB\xBF", "UTF-8"),
)

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
CHUNK_SIZE = 64 * 1024


def detect_bom(head: bytes) -> str | None:
    for prefix, name in BOMS:
        if head.startswith(prefix):
            return name
    return None


def preflight_utf8(path: Path, chunk_size: int = CHUNK_SIZE) -> str | None:
    """Full-file streaming strict UTF-8 check (O(1) memory).

    Returns None when the file may enter the pipeline (valid UTF-8, with or
    without a UTF-8 BOM). Returns a detected_encoding descriptor otherwise.
    """
    with Path(path).open("rb") as f:
        bom = detect_bom(f.read(max(len(p) for p, _ in BOMS)))
        if bom is not None:
            return None if bom == "UTF-8" else bom
        decoder = codecs.getincrementaldecoder("utf-8")()
        f.seek(0)
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                return "unknown"
    return None
