"""File-type router: documents vs media vs unsupported. No format guessing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".aac"}
IGNORED_NAMES = {".DS_Store"}


@dataclass
class RoutingResult:
    documents: list[Path] = field(default_factory=list)
    media: list[Path] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)
    is_collection: bool = False


def _classify(path: Path, result: RoutingResult) -> None:
    ext = path.suffix.lower()
    if ext in DOCUMENT_EXTS:
        result.documents.append(path)
    elif ext in MEDIA_EXTS:
        result.media.append(path)
    else:
        result.unsupported.append(path)


def route_source(path: Path) -> RoutingResult:
    path = Path(path)
    if path.is_file():
        result = RoutingResult(is_collection=False)
        _classify(path.resolve(), result)
        return result
    result = RoutingResult(is_collection=True)
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        if item.name in IGNORED_NAMES or item.name.startswith("._"):
            continue
        if any(part.startswith(".") for part in item.relative_to(path).parts):
            continue
        _classify(item.resolve(), result)
    return result
