"""File-type router: documents vs media vs unsupported. No format guessing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".aac"}
IGNORED_NAMES = {".DS_Store"}


@dataclass
class RoutingResult:
    """Three-bucket routing (v0.3 frozen spec 5).

    documents/media/unsupported are the *effective* paths: discovery order
    preserved, excluded items removed. discovered_* = effective + excluded
    (per type, scan order). excluded_* keep their own scan order.
    """

    documents: list[Path] = field(default_factory=list)
    media: list[Path] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)
    excluded_documents: list[Path] = field(default_factory=list)
    excluded_media: list[Path] = field(default_factory=list)
    excluded_unsupported: list[Path] = field(default_factory=list)
    discovered_documents: list[Path] = field(default_factory=list)
    discovered_media: list[Path] = field(default_factory=list)
    discovered_unsupported: list[Path] = field(default_factory=list)
    is_collection: bool = False


def _classify(path: Path, result: RoutingResult,
              excluded: set[str]) -> None:
    ext = path.suffix.lower()
    if ext in DOCUMENT_EXTS:
        eff, exc, disc = (result.documents, result.excluded_documents,
                          result.discovered_documents)
    elif ext in MEDIA_EXTS:
        eff, exc, disc = (result.media, result.excluded_media,
                          result.discovered_media)
    else:
        eff, exc, disc = (result.unsupported, result.excluded_unsupported,
                          result.discovered_unsupported)
    disc.append(path)
    if path.as_posix() in excluded:
        exc.append(path)
    else:
        eff.append(path)


def _excluded_posix(excludes) -> set[str]:
    return {Path(p).expanduser().resolve().as_posix() for p in (excludes or [])}


def route_source(path: Path, excludes=None) -> RoutingResult:
    """Scan *path* and classify files, honouring the ordered exclusion set."""
    excluded = _excluded_posix(excludes)
    path = Path(path)
    if path.is_file():
        result = RoutingResult(is_collection=False)
        _classify(path.resolve(), result, excluded)
        return result
    result = RoutingResult(is_collection=True)
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        if item.name in IGNORED_NAMES or item.name.startswith("._"):
            continue
        if any(part.startswith(".") for part in item.relative_to(path).parts):
            continue
        _classify(item.resolve(), result, excluded)
    return result


def effective_paths(routing: dict, type_: str) -> list[Path]:
    """Read effective paths from a routing dict, with v0.2 legacy fallback.

    v0.3 manifests carry routing["effective"][type]; v0.2 manifests only had
    routing["<type>_paths"] (already effective: exclusions were never applied
    to documents before v0.3, and media exclusions are reflected there).
    """
    eff = routing.get("effective") or {}
    if eff:
        return [Path(p) for p in (eff.get(type_) or [])]
    return [Path(p) for p in routing.get(f"{type_}_paths") or []]
