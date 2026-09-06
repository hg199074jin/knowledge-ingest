import json
from pathlib import Path

from knowledge_ingest.adapters.local import build_local_handoff, write_handoff


def test_build_local_handoff_shape(tmp_path: Path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    handoff = build_local_handoff(source)
    assert handoff["provider"] == "local"
    assert handoff["remote"] is None
    assert handoff["local_path"] == str(source.resolve())
    assert handoff["download_completed"] is True
    assert handoff["schema_version"] == 1


def test_write_handoff_roundtrip(tmp_path: Path):
    source = tmp_path / "course"
    source.mkdir()
    (source / "01.md").write_text("x", encoding="utf-8")
    dest = tmp_path / "source.json"
    written = write_handoff(source, dest)
    assert written == dest
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["local_path"] == str(source.resolve())
