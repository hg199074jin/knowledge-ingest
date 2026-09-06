"""Local source adapter: registers an existing path without copying it."""

from __future__ import annotations

import json
from pathlib import Path

HANDOFF_SCHEMA_VERSION = 1


def build_local_handoff(source_path: Path) -> dict:
    resolved = Path(source_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"local source not found: {resolved}")
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "provider": "local",
        "remote": None,
        "local_path": str(resolved),
        "download_completed": True,
        "source_notes": [],
    }


def write_handoff(source_path: Path, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(build_local_handoff(source_path), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest
