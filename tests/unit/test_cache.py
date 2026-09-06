from pathlib import Path

from knowledge_ingest.cache import TranscriptCache, TranscriptCacheEntry
from knowledge_ingest.fingerprint import fingerprint_file


def make_entry(tmp_path: Path, key: str = "k1") -> TranscriptCacheEntry:
    md = tmp_path / "lesson.md"
    md.write_text("# transcript", encoding="utf-8")
    meta = tmp_path / "metadata.yaml"
    meta.write_text("source: lesson\n", encoding="utf-8")
    return TranscriptCacheEntry(
        cache_key=key,
        source_path=tmp_path / "lesson.mp4",
        markdown_path=md,
        metadata_path=meta,
        transcript_sha256=fingerprint_file(md),
    )


def test_cache_roundtrip(tmp_path: Path):
    index = tmp_path / "transcript-index.json"
    cache = TranscriptCache(index)
    entry = make_entry(tmp_path)
    cache.put(entry)
    loaded = TranscriptCache(index)
    assert loaded.lookup("k1") == entry


def test_cache_rejects_modified_transcript(tmp_path: Path):
    cache = TranscriptCache(tmp_path / "i.json")
    entry = make_entry(tmp_path)
    cache.put(entry)
    entry.markdown_path.write_text("changed", encoding="utf-8")
    assert cache.lookup("k1") is None


def test_cache_rejects_missing_metadata(tmp_path: Path):
    cache = TranscriptCache(tmp_path / "i.json")
    entry = make_entry(tmp_path)
    cache.put(entry)
    entry.metadata_path.unlink()
    assert cache.lookup("k1") is None


def test_cache_rejects_missing_markdown(tmp_path: Path):
    cache = TranscriptCache(tmp_path / "i.json")
    entry = make_entry(tmp_path)
    cache.put(entry)
    entry.markdown_path.unlink()
    assert cache.lookup("k1") is None


def test_cache_miss_unknown_key(tmp_path: Path):
    cache = TranscriptCache(tmp_path / "i.json")
    assert cache.lookup("nope") is None


def test_cache_overwrites_same_key(tmp_path: Path):
    cache = TranscriptCache(tmp_path / "i.json")
    entry = make_entry(tmp_path, "k1")
    cache.put(entry)
    entry.markdown_path.write_text("v2", encoding="utf-8")
    entry.transcript_sha256 = fingerprint_file(entry.markdown_path)
    cache.put(entry)
    reloaded = cache.lookup("k1")
    assert reloaded is not None
    assert reloaded.transcript_sha256 == entry.transcript_sha256
