"""TG4: Source Item Builder（冻结设计 §9/§20/§22；方案 §6.2/§6.4/§6.6）。

窗口要点（§9.1）：first-message anchored 固定 300 秒，不是滚动窗口；
同 source + 同 sender 才合并；PDF / 网盘链接消息打断合并。
编辑四态（§6.4）以 handoff_completed 为不可逆边界；Item 内容延迟
派生——materialize 时按 message_ids 回读 tg_messages 重建，因此
open 窗口内的 EDIT 天然被吸收。
"""

import re
from datetime import datetime
from pathlib import Path

from .client_port import TelegramEventKind
from .event_store import TelegramEventStore

WINDOW_SECONDS = 300
_QUARK_HINT = "quark.cn"
_BAIDU_HINT = "pan.baidu.com"
_COLLECTION_RE = re.compile(
    r"\d+\s*(?:份|个文件|个文档|集)|\d+(?:\.\d+)?\s*[GT]B",
    re.IGNORECASE)


def classify_kind(event) -> str:
    if event.document is not None:
        return "pdf"
    if event.cloud_links:
        return "cloud_link"
    return "text"


def provider_guess(links) -> str:
    """§22：与 baidu/quark provider 枚举对齐；识别不出 = unknown。"""
    joined = " ".join(links)
    if _QUARK_HINT in joined:
        return "quark"
    if _BAIDU_HINT in joined:
        return "baidu"
    return "unknown"


def is_resource_collection_candidate(text: str | None) -> bool:
    """§22 大合集描述启发式（"2755 份 / 176G" 类）；只做标记，
    V2 绝不自动下载/遍历。"""
    return bool(text) and bool(_COLLECTION_RE.search(text))


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


def _sender_str(sender_id) -> str | None:
    return str(sender_id) if sender_id is not None else None


class SourceItemPipeline:
    """Raw 事实之上的 Item 层；由 watcher 在落库后回调（§19.2）。"""

    def __init__(self, store: TelegramEventStore, *,
                 data_root: str | Path,
                 window_seconds: int = WINDOW_SECONDS,
                 orico_check=None):
        self.store = store
        self.data_root = Path(data_root)
        self.window_seconds = window_seconds
        self._orico_check = orico_check

    # ---- watcher 回调入口 ----

    def on_raw(self, event, status: str, source_id: str | None):
        if source_id is None:
            return None
        if event.kind is TelegramEventKind.NEW and status == "stored":
            return self.ingest_new(event, source_id)
        if event.kind is TelegramEventKind.EDIT and status == "updated":
            return self.ingest_edit(event, source_id)
        if event.kind is TelegramEventKind.DELETE and status == "deleted":
            return self.ingest_delete(event, source_id)
        return None

    # ---- NEW：分类 + 窗口合并 ----

    def ingest_new(self, event, source_id: str):
        kind = classify_kind(event)
        if kind == "text":
            return self._merge_or_open_text(event, source_id)
        item_id = f"{source_id}:{event.message_id}"
        self.store.create_source_item(item_id, source_id, kind,
                                      [event.message_id])
        self.store.finalize_item(item_id)
        if kind == "cloud_link":
            # PENDING_RESOURCE 必须写在 finalize 之后（finalize 会改状态）
            self.store.set_item_processing_status(item_id,
                                                  "PENDING_RESOURCE")
        return item_id

    def _merge_or_open_text(self, event, source_id: str) -> str:
        self._close_expired_windows(event, source_id)
        existing = self.store.find_open_text_item(source_id)
        if existing is not None:
            last = self.store.get_message(source_id,
                                          existing["last_message_id"])
            same_sender = (
                last is not None
                and last["sender_id"] == _sender_str(event.sender_id))
            if same_sender and self._within_window(
                    source_id, existing, event.message_date):
                self.store.append_item_message(existing["item_id"],
                                               event.message_id)
                return existing["item_id"]
            self.store.finalize_item(existing["item_id"])
            self._materialize(existing["item_id"])
        item_id = f"{source_id}:{event.message_id}"
        self.store.create_source_item(item_id, source_id, "text",
                                      [event.message_id])
        return item_id

    def _close_expired_windows(self, event, source_id: str) -> None:
        if event.message_date is None:
            return
        open_item = self.store.find_open_text_item(source_id)
        if open_item is None:
            return
        if not self._within_window(source_id, open_item,
                                   event.message_date):
            self.store.finalize_item(open_item["item_id"])
            self._materialize(open_item["item_id"])

    def _within_window(self, source_id: str, item, event_date) -> bool:
        if event_date is None:
            return True
        first = self.store.get_message(source_id, item["first_message_id"])
        if first is None:
            return False
        anchor = _aware(first["message_date"])
        return (event_date - anchor).total_seconds() <= self.window_seconds

    # ---- EDIT：§6.4 四态 ----

    def ingest_edit(self, event, source_id: str):
        item = self.store.find_item_by_message(source_id, event.message_id)
        if item is None:
            return self.ingest_new(event, source_id)  # 未见过的编辑=事实补录
        if item["handoff_completed"]:
            self.store.create_review(item["item_id"],
                                     "SOURCE_EDITED_AFTER_HANDOFF",
                                     kind=item["kind"])
            return "handoff_review"          # §6.4 C：Corpus 不动
        if item["materialized_path"]:
            self._materialize(item["item_id"])
            return "rebuilt"                 # §6.4 B：同 item_id 重建
        return "absorbed"                    # §6.4 A：open 窗口派生吸收

    # ---- DELETE：§6.4 D ----

    def ingest_delete(self, event, source_id: str):
        item = self.store.find_item_by_message(source_id, event.message_id)
        if item is None:
            return None
        if item["handoff_completed"]:
            return "deleted_provenance"      # 仅追加事实，不跨层回滚
        self.store.set_item_processing_status(item["item_id"],
                                              "deleted_source")
        return "blocked"                     # 阻止尚未开始的自动 handoff

    # ---- 内部 ----

    def _materialize(self, item_id: str):
        from .materialize import materialize_text_item, orico_ready

        check = self._orico_check
        if check is None:
            check = orico_ready
        materialize_text_item(self.store, item_id, self.data_root,
                              orico_check=check)
