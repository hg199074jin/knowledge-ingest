import os
from pathlib import Path

from knowledge_ingest.cache import (
    CorpusCache,
    build_corpus_cache_key,
)
from knowledge_ingest.fingerprint import fingerprint_collection, fingerprint_file


def test_same_content_different_paths_same_key(tmp_path: Path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert fingerprint_file(a) == fingerprint_file(b)


def test_symlinked_handoff_fingerprint_matches_direct(tmp_path: Path):
    direct = tmp_path / "direct"
    direct.mkdir()
    (direct / "01.md").write_text("t", encoding="utf-8")
    (direct / "02.pdf").write_bytes(b"pdf")

    handoff = tmp_path / "handoff" / "document-set"
    handoff.mkdir(parents=True)
    os.symlink(direct / "01.md", handoff / "01.md")
    os.symlink(direct / "02.pdf", handoff / "02.pdf")

    assert (fingerprint_collection(handoff).sha256
            == fingerprint_collection(direct).sha256)


def test_corpus_cache_key_changes_with_components(tmp_path: Path):
    base = dict(handoff_fingerprint="sha256:h1",
                docchunk_revision="e84e889", config_fingerprint="none")
    k1 = build_corpus_cache_key(**base)
    assert k1 == build_corpus_cache_key(**base)
    assert (build_corpus_cache_key(**{**base, "handoff_fingerprint": "sha256:h2"})
            != k1)
    assert (build_corpus_cache_key(**{**base, "docchunk_revision": "other"})
            != k1)
    assert (build_corpus_cache_key(**{**base, "config_fingerprint": "cfg"})
            != k1)


def make_fake_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus-demo"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{}", encoding="utf-8")
    (corpus / "index.jsonl").write_text("", encoding="utf-8")
    return corpus


def test_corpus_cache_roundtrip(tmp_path: Path):
    cache = CorpusCache(tmp_path / "corpus-index.json")
    corpus = make_fake_corpus(tmp_path)
    cache.put("k1", corpus)
    loaded = CorpusCache(tmp_path / "corpus-index.json")
    assert loaded.lookup("k1", verify=lambda p: True) == corpus


def test_corpus_cache_drops_entry_on_verify_fail(tmp_path: Path):
    cache = CorpusCache(tmp_path / "corpus-index.json")
    corpus = make_fake_corpus(tmp_path)
    cache.put("k1", corpus)
    assert cache.lookup("k1", verify=lambda p: False) is None
    # 已被移除：下次即使 verify 通过也不该命中
    assert cache.lookup("k1", verify=lambda p: True) is None


def test_corpus_cache_miss_when_dir_gone(tmp_path: Path):
    cache = CorpusCache(tmp_path / "corpus-index.json")
    corpus = make_fake_corpus(tmp_path)
    cache.put("k1", corpus)
    import shutil
    shutil.rmtree(corpus)
    assert cache.lookup("k1", verify=lambda p: True) is None


def test_corpus_cache_heals_corrupted_index(tmp_path: Path):
    import json
    index = tmp_path / "corpus-index.json"
    index.write_text("not json at all", encoding="utf-8")
    cache = CorpusCache(index)
    assert cache.lookup("k1", verify=lambda p: True) is None
    corpus = make_fake_corpus(tmp_path)
    cache.put("k1", corpus)
    data = json.loads(index.read_text(encoding="utf-8"))
    assert "k1" in data
