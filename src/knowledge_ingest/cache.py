"""Verified transcript & corpus caches. Reuse is validated, never assumed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from knowledge_ingest.fingerprint import fingerprint_file


def _sha256_of_payload(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def build_transcript_cache_key(
    source_sha256: str,
    mt_head: str,
    config_sha: str | None,
    device: str,
    timestamp: str,
    glossary: str | None,
    hotwords: str | None,
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "media_transcriber_head": mt_head,
        "config_sha256": config_sha,
        "device": device,
        "timestamp": timestamp,
        "glossary": glossary,
        "hotwords": hotwords,
    }
    return _sha256_of_payload(payload)


def build_corpus_cache_key(
    handoff_fingerprint: str,
    docchunk_revision: str,
    config_fingerprint: str,
) -> str:
    payload = {
        "handoff_fingerprint": handoff_fingerprint,
        "docchunk_revision": docchunk_revision,
        "config_fingerprint": config_fingerprint,
    }
    return _sha256_of_payload(payload)


class TranscriptCacheEntry(BaseModel):
    cache_key: str
    source_path: Path
    markdown_path: Path
    metadata_path: Path
    transcript_sha256: str


class TranscriptCache:
    def __init__(self, index_path: Path) -> None:
        self.index_path = Path(index_path)

    def _read(self) -> dict:
        if not self.index_path.is_file():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def put(self, entry: TranscriptCacheEntry) -> None:
        data = self._read()
        data[entry.cache_key] = entry.model_dump(mode="json")
        self._write(data)

    def lookup(self, key: str) -> TranscriptCacheEntry | None:
        raw = self._read().get(key)
        if raw is None:
            return None
        entry = TranscriptCacheEntry.model_validate(raw)
        # 复用前校验：md 与 metadata 存在，且 md 内容指纹未变
        if not entry.markdown_path.is_file():
            return None
        if not entry.metadata_path.is_file():
            return None
        if fingerprint_file(entry.markdown_path) != entry.transcript_sha256:
            return None
        return entry


class CorpusCache:
    def __init__(self, index_path: Path) -> None:
        self.index_path = Path(index_path)

    def _read(self) -> dict:
        if not self.index_path.is_file():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def lookup(self, key: str, verify: Callable[[Path], bool]) -> Path | None:
        raw = self._read().get(key)
        if raw is None:
            return None
        corpus = Path(raw["corpus_path"])
        if corpus.is_dir() and verify(corpus):
            return corpus
        # verify 失败即移除条目并重建
        data = self._read()
        data.pop(key, None)
        self._write(data)
        return None

    def put(self, key: str, corpus_path: Path) -> None:
        data = self._read()
        data[key] = {"corpus_path": str(corpus_path)}
        self._write(data)
