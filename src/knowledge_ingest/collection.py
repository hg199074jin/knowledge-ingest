"""Provenance-preserving Document Set builder for mixed collections."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class CollectionIncomplete(Exception):
    """Raised when any required transcript/document is missing — hard gate."""


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


@dataclass
class DocumentSetResult:
    document_set: Path
    map_path: Path
    entries: list[dict] = field(default_factory=list)


def _sanitize_relative(rel: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", rel)
    return cleaned[:120] or "file"


def build_document_set(
    handoff_dir: Path,
    source_root: Path,
    document_paths: list[Path],
    media_paths: list[Path],
    transcripts: dict[str, str],
    excluded: set[str],
) -> DocumentSetResult:
    handoff_dir = Path(handoff_dir)
    source_root = Path(source_root).resolve()

    existing = [p for p in handoff_dir.iterdir()] if handoff_dir.is_dir() else []
    if existing:
        raise RuntimeError(
            f"refusing to overwrite non-empty handoff dir: {handoff_dir}")

    entries: list[dict] = []

    # 完整性硬门：任何未排除的媒体缺转写即失败，不静默继续
    for media in sorted(media_paths, key=lambda p: _natural_key(str(p))):
        media_str = str(Path(media).resolve())
        if media_str in excluded or str(media) in excluded:
            continue
        transcript = transcripts.get(media_str)
        if not transcript or not Path(transcript).is_file():
            raise CollectionIncomplete(
                f"media has no transcript (job cannot proceed): {media}")

    sources = []
    for media in sorted(media_paths, key=lambda p: _natural_key(str(p))):
        media_resolved = Path(media).resolve()
        if media_resolved.as_posix() in {Path(e).resolve().as_posix()
                                         for e in excluded}:
            continue
        sources.append(("transcript", media_resolved,
                        Path(transcripts[media_resolved.as_posix()])))
    for doc in sorted(document_paths, key=lambda p: _natural_key(str(p))):
        sources.append(("document", Path(doc).resolve(), None))

    sources.sort(key=lambda item: _natural_key(
        item[1].relative_to(source_root).as_posix()
        if source_root.is_dir() and source_root in item[1].parents
        else item[1].name))

    used_names: set[str] = set()
    for index, (kind, source_path, transcript_path) in enumerate(sources, start=1):
        rel = (source_path.relative_to(source_root).as_posix()
               if source_root.is_dir() and source_root in source_path.parents
               else source_path.name)
        if kind == "transcript":
            handoff_name = f"{index:02d}.md"
            target = transcript_path
        else:
            base = source_path.name
            handoff_name = (base if base not in used_names
                            else f"{index:02d}__{_sanitize_relative(rel)}")
            target = source_path
        if handoff_name in used_names:
            handoff_name = f"{index:02d}__{_sanitize_relative(rel)}"
        used_names.add(handoff_name)

        link = handoff_dir / handoff_name
        os.symlink(target, link)
        entry = {
            "handoff_name": handoff_name,
            "source_relative_path": rel,
            "kind": kind,
        }
        if kind == "transcript":
            entry["transcript_path"] = str(target)
        else:
            entry["source_path"] = str(target)
        entries.append(entry)

    handoff_dir.mkdir(parents=True, exist_ok=True)
    # map 与 document-set 同级（handoff/document-set-map.yaml），
    # 不混入送入 docchunk 的目录本身
    map_path = handoff_dir.parent / "document-set-map.yaml"
    map_path.write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return DocumentSetResult(document_set=handoff_dir, map_path=map_path,
                             entries=entries)


def clear_handoff(handoff_dir: Path) -> None:
    """Remove previously built handoff content before a rebuild (idempotent)."""
    handoff_dir = Path(handoff_dir)
    if not handoff_dir.is_dir():
        return
    for item in handoff_dir.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
