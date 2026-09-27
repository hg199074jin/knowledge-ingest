"""V3 M2: Telegram shadow adapter — 只读投影，V2 Intake 的下游。

设计边界（设计 §4.1/§27）：不重实现 noise/interest 策略、不写任何
Telegram 行；Insight Engine 不进入 telegram/ 内核——本模块是唯一
薄 Adapter，把 Source Item 投影为源无关的 InsightSourceView。

资格规则（实施方案 Task 2；生产状态词汇 2026-09-28 实测）：
- text：processing_status == 'materialized'（KEEP 且已物化）；
- pdf/video：handoff_completed == 1（V2 已接受该内容）；
  job.yaml → docchunk.corpus_path 可达 → corpus_verified；
  corpus 未就绪 → view 仍存在（full_text_available=False，
  WAITING_CONTENT 可恢复），绝不 REJECT（Review Focus #1）；
- 其余一切状态（open/review/PENDING_RESOURCE/skipped_*/empty/deleted、
  cloud_link/file 类型）→ None：V2 摄取未完成或不产 Insight。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.source_view import (
    compute_corpus_fingerprint,
    compute_text_fingerprint,
)

from .event_store import TelegramEventStore

_TEXT_ELIGIBLE_STATUS = "materialized"
_INELIGIBLE_STATUSES = frozenset({
    "open", "noise_review", "interest_review", "PENDING_RESOURCE",
    "skipped_noise", "skipped_interest", "skipped_unsupported",
    "empty_item", "deleted_source",
})
_MEDIA_KINDS = frozenset({"pdf", "video"})


def _corpus_path_from_job(pipeline_root: Path, job_id: str | None) -> str | None:
    """job.yaml → docchunk.corpus_path（唯一权威来源；不发明路径）。"""
    if not job_id:
        return None
    job_yaml = Path(pipeline_root) / "jobs" / job_id / "job.yaml"
    if not job_yaml.is_file():
        return None
    try:
        import yaml
        data = yaml.safe_load(job_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    path = ((data.get("docchunk") or {}) .get("corpus_path")) if isinstance(
        data, dict) else None
    return str(path) if path else None


class TelegramInsightAdapter:
    """TelegramEventStore → InsightSourceView 的只读投影器。"""

    def __init__(self, store: TelegramEventStore, *, pipeline_root):
        self.store = store
        self.pipeline_root = Path(pipeline_root)

    # ---- single item ----

    def to_view(self, item_id: str) -> InsightSourceView | None:
        item = self.store.get_source_item(item_id)
        if item is None:
            return None
        status = item["processing_status"]
        kind = item["kind"]
        if status in _INELIGIBLE_STATUSES:
            return None
        if kind == "text":
            if status != _TEXT_ELIGIBLE_STATUS:
                return None
            return self._text_view(item)
        if kind in _MEDIA_KINDS:
            if not item["handoff_completed"]:
                return None
            return self._media_view(item)
        return None                     # cloud_link / file：不产 Insight

    # ---- kind-specific projections ----

    def _text_view(self, item) -> InsightSourceView | None:
        item_id = item["item_id"]
        mat_path = item["materialized_path"]
        texts = [row["text"] for row in self.store.get_item_messages(item_id)
                 if row["text"] and not row["deleted_at"]]
        visible = "\n\n".join(texts)
        full_text = False
        fingerprint = compute_text_fingerprint(visible)
        if mat_path and Path(mat_path).is_file():
            try:
                raw = Path(mat_path).read_bytes()
                raw.decode("utf-8")
                full_text = True
                fingerprint = compute_fingerprint_bytes(raw)
            except (OSError, UnicodeDecodeError):
                full_text = False       # 物化文件不可读 → WAITING
        return self._view(item, content_kind="text",
                          visible_text=visible,
                          materialized_path=mat_path if full_text else None,
                          full_text_available=full_text,
                          verification_status="source_only",
                          content_fingerprint=fingerprint)

    def _media_view(self, item) -> InsightSourceView | None:
        message = self.store.get_message(item["source_id"],
                                         item["last_message_id"])
        caption = (message["text"] if message else None) or ""
        filename = (message["document_name"] if message else None) or ""
        visible = "\n".join(part for part in (caption, filename) if part)
        corpus_path = _corpus_path_from_job(
            self.pipeline_root, item["knowledge_ingest_job_id"])
        if corpus_path:
            batches_dir = Path(corpus_path) / "batches"
            index = Path(corpus_path) / "index.jsonl"
            if index.is_file() and batches_dir.is_dir():
                batches = sorted(batches_dir.glob("B*.md"))
                if batches and self._batches_readable(batches):
                    stats = [(b.name, b.stat().st_size) for b in batches]
                    return self._view(
                        item, content_kind=(
                            "pdf" if item["kind"] == "pdf"
                            else "video_transcript"),
                        visible_text=visible,
                        materialized_path=corpus_path,
                        full_text_available=True,
                        verification_status="corpus_verified",
                        content_fingerprint=compute_corpus_fingerprint(stats))
        # V2 已接受但 corpus 未就绪：注册 view，WAITING_CONTENT，不 REJECT
        return self._view(item, content_kind=(
            "pdf" if item["kind"] == "pdf" else "video_transcript"),
            visible_text=visible, materialized_path=None,
            full_text_available=False, verification_status="source_only",
            content_fingerprint=compute_text_fingerprint(visible))

    @staticmethod
    def _batches_readable(batches) -> bool:
        for batch in batches:
            try:
                batch.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return False
        return True

    def _view(self, item, *, content_kind: str, visible_text: str,
              materialized_path: str | None, full_text_available: bool,
              verification_status: str, content_fingerprint: str
              ) -> InsightSourceView:
        message = self.store.get_message(item["source_id"],
                                         item["last_message_id"])
        return InsightSourceView(
            provider="telegram",
            source_item_id=item["item_id"],
            content_kind=content_kind,
            title=None,
            visible_text=visible_text,
            materialized_path=materialized_path,
            full_text_available=full_text_available,
            verification_status=verification_status,
            content_fingerprint=content_fingerprint,
            captured_at=item["created_at"],
            author=message["sender_id"] if message else None,
            source_deleted=bool(item["processing_status"] == "deleted_source"),
            original_refs=({"message_id": item["last_message_id"]},))

    # ---- batch iteration ----

    def iter_views(self, *, limit: int | None = None):
        """按 item_id 序投影全部合格 Source Item（shadow 只读）。"""
        count = 0
        for row in self.store.list_source_items():
            if limit is not None and count >= limit:
                return
            view = self.to_view(row["item_id"])
            if view is not None:
                count += 1
                yield view


def compute_fingerprint_bytes(data: bytes) -> str:
    return "fp." + hashlib.sha256(data).hexdigest()[:16]
