from pathlib import Path

import pytest
import yaml

from knowledge_ingest.collection import CollectionIncomplete, build_document_set


def make_source(tmp_path: Path) -> Path:
    src = tmp_path / "source" / "课程"
    src.mkdir(parents=True)
    (src / "01.mp4").write_bytes(b"v1")
    (src / "02.mp4").write_bytes(b"v2")
    (src / "03.pdf").write_bytes(b"pdf")
    (src / "04.docx").write_bytes(b"docx")
    return src


def make_transcripts(tmp_path: Path, src: Path) -> dict[str, str]:
    tdir = tmp_path / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    t1 = tdir / "01.md"
    t2 = tdir / "02.md"
    t1.write_text("t1", encoding="utf-8")
    t2.write_text("t2", encoding="utf-8")
    return {
        str(src / "01.mp4"): str(t1),
        str(src / "02.mp4"): str(t2),
    }


def test_build_mixed_document_set(tmp_path: Path):
    src = make_source(tmp_path)
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    result = build_document_set(
        handoff_dir=handoff,
        source_root=src,
        document_paths=[src / "03.pdf", src / "04.docx"],
        media_paths=[src / "01.mp4", src / "02.mp4"],
        transcripts=make_transcripts(tmp_path, src),
        excluded=set(),
    )
    names = sorted(p.name for p in handoff.iterdir())
    assert names == ["01.md", "02.md", "03.pdf", "04.docx"]
    assert (handoff / "03.pdf").is_symlink()
    assert (handoff / "01.md").resolve().name == "01.md"
    mapping = yaml.safe_load(result.map_path.read_text(encoding="utf-8"))
    kinds = {e["handoff_name"]: e["kind"] for e in mapping}
    assert kinds == {"01.md": "transcript", "02.md": "transcript",
                     "03.pdf": "document", "04.docx": "document"}
    by_name = {e["handoff_name"]: e for e in mapping}
    assert by_name["03.pdf"]["source_relative_path"] == "03.pdf"
    assert by_name["01.md"]["source_relative_path"] == "01.mp4"
    assert by_name["01.md"]["transcript_path"].endswith("transcripts/01.md")


def test_missing_transcript_blocks(tmp_path: Path):
    src = make_source(tmp_path)
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    transcripts = make_transcripts(tmp_path, src)
    del transcripts[str(src / "02.mp4")]
    with pytest.raises(CollectionIncomplete) as excinfo:
        build_document_set(
            handoff_dir=handoff, source_root=src,
            document_paths=[src / "03.pdf"],
            media_paths=[src / "01.mp4", src / "02.mp4"],
            transcripts=transcripts, excluded=set(),
        )
    assert str(src / "02.mp4") in str(excinfo.value)


def test_excluded_media_skipped(tmp_path: Path):
    src = make_source(tmp_path)
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    transcripts = make_transcripts(tmp_path, src)
    del transcripts[str(src / "02.mp4")]
    result = build_document_set(
        handoff_dir=handoff, source_root=src,
        document_paths=[src / "03.pdf"],
        media_paths=[src / "01.mp4", src / "02.mp4"],
        transcripts=transcripts,
        excluded={str(src / "02.mp4")},
    )
    names = sorted(p.name for p in handoff.iterdir())
    assert names == ["01.md", "03.pdf"]
    assert "02.mp4" not in result.map_path.read_text(encoding="utf-8")


def test_name_conflict_uses_sanitized_relative(tmp_path: Path):
    src = tmp_path / "source"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    (src / "a" / "notes.pdf").write_bytes(b"1")
    (src / "b" / "notes.pdf").write_bytes(b"2")
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    result = build_document_set(
        handoff_dir=handoff, source_root=src,
        document_paths=[src / "a" / "notes.pdf", src / "b" / "notes.pdf"],
        media_paths=[], transcripts={}, excluded=set(),
    )
    names = sorted(p.name for p in handoff.iterdir())
    assert len(names) == 2
    assert len({names[0], names[1]}) == 2
    assert names[0] != names[1]
    mapping = yaml.safe_load(result.map_path.read_text(encoding="utf-8"))
    rels = {e["source_relative_path"] for e in mapping}
    assert rels == {"a/notes.pdf", "b/notes.pdf"}


def test_single_document_handoff(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    doc = src / "book.pdf"
    doc.write_bytes(b"pdf")
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    result = build_document_set(
        handoff_dir=handoff, source_root=doc,
        document_paths=[doc], media_paths=[], transcripts={}, excluded=set(),
    )
    entries = list(handoff.iterdir())
    assert len(entries) == 1
    assert entries[0].is_symlink()
    mapping = yaml.safe_load(result.map_path.read_text(encoding="utf-8"))
    assert mapping[0]["kind"] == "document"


def test_refuses_to_overwrite_existing_handoff(tmp_path: Path):
    src = tmp_path / "source"
    src.mkdir()
    doc = src / "book.pdf"
    doc.write_bytes(b"pdf")
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    (handoff / "stale.md").write_text("old", encoding="utf-8")
    with pytest.raises(RuntimeError):
        build_document_set(
            handoff_dir=handoff, source_root=doc,
            document_paths=[doc], media_paths=[], transcripts={},
            excluded=set(),
        )
