"""TG3: watcher — 事件进库唯一通道（冻结设计 §19.2）。

live updates 与 periodic reconcile 两条路径都只经由
TelegramEventStore 落库（先 commit 后处理）；Source Item /
materialization / policy 语义由 TG4 在库内事实之上实现，本模块
不做任何下游决策（§5.7）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from .client_port import TelegramClientPort, TelegramEventKind
from .event_store import MessageBeforeStartError, TelegramEventStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class TelegramWatcher:
    def __init__(self, store: TelegramEventStore,
                 client: TelegramClientPort | None = None):
        self.store = store
        self.client = client

    def handle_event(self, event) -> str:
        """三类 update → Raw Event；返回处理结果供测试/日志观测。"""
        source = self.store.get_source_by_chat_id(event.chat_id)
        if source is None:
            return "ignored"  # 非白名单来源
        if not source["enabled"]:
            return "skipped"
        source_id = source["source_id"]
        if event.kind is TelegramEventKind.DELETE:
            try:
                self.store.mark_message_deleted(source_id,
                                                event.message_id)
            except ValueError:
                return "ignored"  # 从未入库的消息：无需标记
            return "deleted"
        document = event.document
        try:
            self.store.upsert_message(
                source_id, event.message_id,
                message_date=(_iso(event.message_date)
                              if event.message_date else _now_iso()),
                sender_id=(str(event.sender_id)
                           if event.sender_id is not None else None),
                text=event.text,
                has_document=document is not None,
                document_name=(document.file_name
                               if document is not None else None),
                document_mime=(document.mime_type
                               if document is not None else None),
                document_size_bytes=(document.size_bytes
                                     if document is not None else None),
                cloud_links=(list(event.cloud_links) or None),
                edited_at=(_iso(event.edited_at)
                           if event.edited_at else None))
        except MessageBeforeStartError:
            return "skipped_old"  # §6.3 硬边界：永不历史回溯
        return ("updated" if event.kind is TelegramEventKind.EDIT
                else "stored")

    async def reconcile(self, source_id: str) -> int:
        """§5.7 B 路径：只补 start_at 之后、cursor 之前的缺口。"""
        if self.client is None:
            raise ValueError("watcher requires a client to reconcile")
        source = self.store.get_source(source_id)
        if source is None or not source["enabled"]:
            return 0
        events = await self.client.fetch_messages_after(
            source["chat_id"], source["last_seen_message_id"])
        stored = 0
        max_id = source["last_seen_message_id"]
        for event in events:
            status = self.handle_event(event)
            if status in ("stored", "updated"):
                stored += 1
            max_id = max(max_id, event.message_id)
        self.store.touch_source_cursor(
            source_id, last_seen_message_id=max_id,
            last_reconciled_at=_now_iso())
        return stored

    async def reconcile_all(self) -> int:
        total = 0
        for source in self.store.list_sources():
            if source["enabled"]:
                total += await self.reconcile(source["source_id"])
        return total

    async def run(self, *, max_ticks: int | None = None,
                  idle_seconds: float = 300.0) -> None:
        """live 主循环：排空 updates，空闲后跑一轮周期 reconcile。

        max_ticks 仅用于测试/一次性运行；生产常驻时为 None。
        """
        tick = 0
        while max_ticks is None or tick < max_ticks:
            tick += 1
            await self._drain_updates(timeout=idle_seconds)
            await self.reconcile_all()

    async def _drain_updates(self, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                async for event in self.client.watch_updates():
                    self.handle_event(event)
        except TimeoutError:
            return
