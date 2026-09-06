from pathlib import Path

from knowledge_ingest.router import route_source


def test_router_mixed_collection(tmp_path: Path):
    (tmp_path / "01.mp4").write_bytes(b"video")
    (tmp_path / "02.pdf").write_bytes(b"pdf")
    (tmp_path / "03.xyz").write_bytes(b"x")
    result = route_source(tmp_path)
    assert len(result.media) == 1
    assert len(result.documents) == 1
    assert len(result.unsupported) == 1
    assert result.is_collection is True


def test_router_single_document(tmp_path: Path):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"pdf")
    result = route_source(pdf)
    assert result.is_collection is False
    assert result.documents == [pdf.resolve()]
    assert result.media == []
    assert result.unsupported == []


def test_router_single_media(tmp_path: Path):
    mp4 = tmp_path / "lesson.mp4"
    mp4.write_bytes(b"v")
    result = route_source(mp4)
    assert result.is_collection is False
    assert result.media == [mp4.resolve()]


def test_router_ignores_noise_and_hidden(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"md")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "._a.md").write_bytes(b"x")
    hidden_dir = tmp_path / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "b.mp4").write_bytes(b"v")
    result = route_source(tmp_path)
    assert len(result.documents) == 1
    assert result.media == []
    assert result.unsupported == []


def test_router_markdown_variant_supported(tmp_path: Path):
    md = tmp_path / "notes.markdown"
    md.write_bytes(b"x")
    result = route_source(md)
    assert len(result.documents) == 1
    assert result.unsupported == []


def test_router_unknown_single_file_unsupported(tmp_path: Path):
    exe = tmp_path / "tool.bin"
    exe.write_bytes(b"x")
    result = route_source(exe)
    assert result.unsupported == [exe.resolve()]
    assert result.is_collection is False
