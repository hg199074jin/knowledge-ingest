"""V3 M2: source-neutral content resolver — parts contract, no path invention.

A2 修订冻结：ResolvedContent.parts 多 part 契约；TEXT 权威路径=
view 携带的 materialized_path（存在才读，绝不按 item_id 重建）；
corpus 入口= index.jsonl + batches/B*.md（§17，无 combined.md fallback）；
任何不可读 → None（WAITING_CONTENT 可恢复），绝不抛错、绝不伪造内容。
"""

import dataclasses
import json
from pathlib import Path

import pytest

from knowledge_ingest.insight.content_resolver import (
    ContentPart,
    PartsContentResolver,
    ResolvedContent,
)
from knowledge_ingest.insight.models import InsightSourceView

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": False,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


def write_text(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# ---------- contract shape ----------

def test_content_part_and_resolved_content_frozen():
    part = ContentPart(part_id="main", text="body", source_ref="/x/message.md")
    resolved = ResolvedContent(parts=(part,), verification_status="source_only")
    assert resolved.parts[0].part_id == "main"
    with pytest.raises(dataclasses.FrozenInstanceError):
        part.part_id = "other"        # frozen


def test_resolver_is_a_content_resolver_port():
    from knowledge_ingest.insight.content_resolver import ContentResolverPort
    assert issubclass(PartsContentResolver, ContentResolverPort)


# ---------- TEXT: authoritative materialized_path ----------

def test_text_single_part_from_materialized_path(tmp_path):
    path = write_text(tmp_path, "message.md", "# Telegram Knowledge Item\n\n正文")
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=path, full_text_available=True))
    assert resolved is not None
    assert len(resolved.parts) == 1
    assert resolved.parts[0].part_id == "main"
    assert "正文" in resolved.parts[0].text
    assert resolved.parts[0].source_ref == path
    assert resolved.verification_status == "source_only"


def test_text_missing_file_returns_none_not_empty(tmp_path):
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(tmp_path / "gone.md")))
    assert resolved is None                  # WAITING_CONTENT, never fake text


def test_text_invalid_utf8_returns_none(tmp_path):
    p = tmp_path / "message.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(p)))
    assert resolved is None                  # recoverable, no crash


def test_no_materialized_path_returns_none():
    assert PartsContentResolver().resolve(make_view()) is None


# ---------- CORPUS: index.jsonl + batches/B*.md, one part per batch ----------

def make_corpus(tmp_path: Path, batch_names=("B0001", "B0002")) -> str:
    corpus = tmp_path / "document-set-abc"
    (corpus / "batches").mkdir(parents=True)
    (corpus / "index.jsonl").write_text(
        "\n".join(json.dumps({"atomic_id": f"a{i}"}) for i in range(3)),
        encoding="utf-8")
    for i, name in enumerate(batch_names):
        (corpus / "batches" / f"{name}.md").write_text(
            f"# batch {name}\n内容{i}", encoding="utf-8")
    return str(corpus)


def test_corpus_yields_one_part_per_batch_sorted(tmp_path):
    corpus = make_corpus(tmp_path, batch_names=("B0002", "B0001"))
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=corpus, verification_status="corpus_verified",
                  full_text_available=True))
    assert resolved is not None
    assert [p.part_id for p in resolved.parts] == ["B0001", "B0002"]
    assert all(p.source_ref.startswith(corpus) for p in resolved.parts)
    assert resolved.verification_status == "corpus_verified"
    assert "内容1" in resolved.parts[0].text   # B0001（名字序第一）


def test_corpus_without_batches_returns_none(tmp_path):
    corpus = tmp_path / "document-set-empty"
    corpus.mkdir()
    (corpus / "index.jsonl").write_text("{}", encoding="utf-8")
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(corpus),
                  verification_status="corpus_verified"))
    assert resolved is None


def test_corpus_without_index_returns_none(tmp_path):
    corpus = tmp_path / "document-set-noidx"
    (corpus / "batches").mkdir(parents=True)
    (corpus / "batches" / "B0001.md").write_text("x", encoding="utf-8")
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(corpus),
                  verification_status="corpus_verified"))
    assert resolved is None


def test_corpus_path_missing_returns_none(tmp_path):
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(tmp_path / "gone-corpus"),
                  verification_status="corpus_verified"))
    assert resolved is None


def test_no_combined_md_fallback(tmp_path):
    """A2 冻结：combined.md 不是入口——只有 batches 缺失时必须 WAITING，
    绝不静默降级读 combined.md。"""
    corpus = tmp_path / "document-set-only-combined"
    corpus.mkdir()
    (corpus / "combined.md").write_text("全部内容", encoding="utf-8")
    resolved = PartsContentResolver().resolve(
        make_view(materialized_path=str(corpus),
                  verification_status="corpus_verified"))
    assert resolved is None
