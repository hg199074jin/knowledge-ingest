"""Verified transcript & corpus caches. Reuse is validated, never assumed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from knowledge_ingest.fingerprint import fingerprint_file
from knowledge_ingest.manifest_store import (
    MANIFEST_LOCK_TIMEOUT as CACHE_LOCK_TIMEOUT,
)
from knowledge_ingest.manifest_store import atomic_write_text, flock_ctx


def _sha256_of_payload(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _load_index(path: Path) -> dict:
    """损坏的索引自愈为空（断电/中断留下的半文件不应造成 crash-loop）。"""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2))


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


def build_media_cache_key(
    source_sha256: str,
    mt_head: str,
    config_sha: str | None,
    device: str,
    timestamp: str,
    glossary: str | None,
    hotwords: str | None,
) -> str:
    """Transcript 缓存键的唯一入口（v0.3 A3）。

    cli.py 查询侧与 adapters/media.py put 侧都必须经由本函数取键；
    payload 与 build_transcript_cache_key 完全一致——统一不得改变现有键
    （characterization 测试锁定），否则现有缓存会整体失效。
    """
    return build_transcript_cache_key(
        source_sha256=source_sha256,
        mt_head=mt_head,
        config_sha=config_sha,
        device=device,
        timestamp=timestamp,
        glossary=glossary,
        hotwords=hotwords,
    )


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

    @property
    def lock_path(self) -> Path:
        """cache-index 一致性锁（独立于 index 文件——index 走 atomic replace 会换 inode）。

        EXCLUSIVE + blocking（有界等待，默认 30 秒）；禁止与 manifest 锁嵌套获取。
        """
        return self.index_path.with_name(self.index_path.name + ".lock")

    def _read(self) -> dict:
        return _load_index(self.index_path)

    def _write(self, data: dict) -> None:
        _save_index(self.index_path, data)

    def put(self, entry: TranscriptCacheEntry) -> None:
        # read-modify-write 必须整体在 cache-index flock 临界区内（v0.3 A3），
        # 并发 put 不丢条目
        with flock_ctx(self.lock_path, timeout=CACHE_LOCK_TIMEOUT):
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
        return _load_index(self.index_path)

    def _write(self, data: dict) -> None:
        _save_index(self.index_path, data)

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
