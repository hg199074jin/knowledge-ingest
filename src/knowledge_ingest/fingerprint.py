"""Content-addressed fingerprints for files and directories."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

CHUNK = 8 * 1024 * 1024
IGNORED_PREFIXES = ("._",)
IGNORED_NAMES = {".DS_Store"}


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


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return f"sha256:{h.hexdigest()}"


def fingerprint_file(path: Path) -> str:
    return _hash_file(Path(path))


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # 文件 symlink 按其解析目标参与指纹（handoff 目录由 symlink 组成，
        # 指纹必须反映最终内容才能支撑跨来源去重）；坏链自动跳过
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
            sha256=_hash_file(path),
        )
        for path in _iter_files(root)
    )
    canonical = json.dumps(
        [{"relative_path": f.relative_path, "size_bytes": f.size_bytes,
          "sha256": f.sha256} for f in files],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CollectionFingerprint(root=root.resolve(), files=files,
                                 sha256=f"sha256:{digest}")
