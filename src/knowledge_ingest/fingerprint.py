"""Content-addressed fingerprints for files and directories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

CHUNK = 8 * 1024 * 1024
IGNORED_PREFIXES = ("._",)
IGNORED_NAMES = {".DS_Store"}


def fingerprint_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return f"sha256:{h.hexdigest()}"


@dataclass(frozen=True)
class FileFingerprint:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CollectionFingerprint:
    root: Path
    files: tuple[FileFingerprint, ...]
    sha256: str


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in IGNORED_NAMES or path.name.startswith(IGNORED_PREFIXES):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        yield path


def fingerprint_collection(root: Path) -> CollectionFingerprint:
    root = Path(root)
    files = tuple(
        FileFingerprint(
            relative_path=path.relative_to(root).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=fingerprint_file(path),
        )
        for path in _iter_files(root)
    )
    canonical = json.dumps(
        [f.__dict__ if not hasattr(f, "__dataclass_fields__") else
         {"relative_path": f.relative_path, "size_bytes": f.size_bytes,
          "sha256": f.sha256}
         for f in files],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CollectionFingerprint(root=root.resolve(), files=files,
                                 sha256=f"sha256:{digest}")
