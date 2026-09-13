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


# ---- v0.3.0 A1: three-bucket routing (discovered/excluded/effective) ----

def test_route_excludes_document(tmp_path: Path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"p")
    vid = tmp_path / "b.mp4"
    vid.write_bytes(b"v")
    r = route_source(tmp_path, excludes={doc.resolve()})
    assert r.documents == []                          # effective
    assert r.excluded_documents == [doc.resolve()]
    assert r.media == [vid.resolve()]
    assert r.excluded_media == []
    assert r.discovered_documents == [doc.resolve()]
    assert r.discovered_media == [vid.resolve()]


def test_route_exclude_preserves_discovery_order(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"v")
    (tmp_path / "b.pdf").write_bytes(b"p")
    (tmp_path / "c.mp4").write_bytes(b"v")
    r = route_source(tmp_path, excludes={(tmp_path / "a.mp4").resolve()})
    assert r.media == [(tmp_path / "c.mp4").resolve()]
    assert r.discovered_media == [(tmp_path / "a.mp4").resolve(),
                                  (tmp_path / "c.mp4").resolve()]


def test_route_exclude_unsupported_clears_effective(tmp_path: Path):
    bat = tmp_path / "目录.bat"
    bat.write_bytes(b"x")
    md = tmp_path / "a.md"
    md.write_bytes(b"m")
    r = route_source(tmp_path, excludes={bat.resolve()})
    assert r.unsupported == []
    assert r.excluded_unsupported == [bat.resolve()]
    assert r.documents == [md.resolve()]


def test_route_single_file_source_excluded(tmp_path: Path):
    exe = tmp_path / "tool.bin"
    exe.write_bytes(b"x")
    r = route_source(exe, excludes={exe.resolve()})
    assert r.unsupported == []
    assert r.excluded_unsupported == [exe.resolve()]
    assert r.discovered_unsupported == [exe.resolve()]


def test_route_bucket_invariant_multiset_and_order(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"v")
    (tmp_path / "b.xyz").write_bytes(b"x")
    (tmp_path / "c.mp4").write_bytes(b"v")
    (tmp_path / "d.pdf").write_bytes(b"p")
    r = route_source(tmp_path, excludes={(tmp_path / "a.mp4").resolve(),
                                         (tmp_path / "b.xyz").resolve()})
    for eff, excl, disc in ((r.media, r.excluded_media, r.discovered_media),
                            (r.documents, r.excluded_documents,
                             r.discovered_documents),
                            (r.unsupported, r.excluded_unsupported,
                             r.discovered_unsupported)):
        assert sorted(eff + excl) == sorted(disc)          # multiset invariant
        # effective must be an order-preserving subsequence of discovered
        it = iter(disc)
        assert all(p in it for p in eff)


def test_route_excluded_bucket_preserves_discovery_order(tmp_path: Path):
    (tmp_path / "a.mp4").write_bytes(b"v")
    (tmp_path / "b.pdf").write_bytes(b"p")
    (tmp_path / "c.mp4").write_bytes(b"v")
    r = route_source(tmp_path, excludes={(tmp_path / "a.mp4").resolve(),
                                         (tmp_path / "c.mp4").resolve()})
    assert r.excluded_media == [(tmp_path / "a.mp4").resolve(),
                                (tmp_path / "c.mp4").resolve()]


def test_route_exclude_non_discovered_path_is_inert(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"m")
    ghost = Path("/foo/not-discovered.pdf")
    r = route_source(tmp_path, excludes={ghost})
    assert r.excluded_documents == []
    assert r.documents == [(tmp_path / "a.md").resolve()]
    assert ghost not in r.discovered_documents
    assert ghost not in r.excluded_documents


def test_route_excluded_document_never_reaches_document_set(tmp_path: Path):
    """v0.2 production bug regression: route exclusion must propagate to
    the Document Set — an excluded document may not re-enter via collection."""
    import yaml

    from knowledge_ingest.collection import build_document_set

    (tmp_path / "a.pdf").write_bytes(b"p")
    (tmp_path / "b.pdf").write_bytes(b"q")
    r = route_source(tmp_path, excludes={(tmp_path / "a.pdf").resolve()})
    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    build_document_set(handoff_dir=handoff, source_root=tmp_path,
                       document_paths=r.documents, media_paths=r.media,
                       transcripts={})
    names = sorted(p.name for p in handoff.iterdir())
    assert names == ["b.pdf"]
    mapping = yaml.safe_load((tmp_path / "handoff" / "document-set-map.yaml")
                             .read_text(encoding="utf-8"))
    assert {e["source_relative_path"] for e in mapping} == {"b.pdf"}
