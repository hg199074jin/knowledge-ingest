"""TG3: watcher — 事件进库唯一通道（冻结设计 §19.2）。

live updates 与 periodic reconcile 两条路径都只经由
TelegramEventStore 落库（先 commit 后处理）；Source Item /
materialization / policy 语义由 TG4 在库内事实之上实现，本模块
不做任何下游决策（§5.7）。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from datetime import UTC, datetime

from .client_port import (
    TelegramClientPort,
    TelegramEventKind,
    TelegramFloodWaitError,
    TelegramRPCError,
)
from .event_store import MessageBeforeStartError, TelegramEventStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class TelegramWatcher:
    def __init__(self, store: TelegramEventStore,
                 client: TelegramClientPort | None = None,
                 pipeline=None):
        self.store = store
        self.client = client
        # TG4：可选 Item 层（原始事实落库后回调；§19.2 顺序不变）
        self.pipeline = pipeline

    def handle_event(self, event) -> str:
        """三类 update → Raw Event；返回处理结果供测试/日志观测。

        落库成功后回调可选 pipeline（TG4 Item 层）；§19.2 顺序：
        先 SQLite commit，后任何下游。
        """
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
            self._pipeline_safe(event, "deleted", source_id)
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
        self.store.advance_seen(source_id, event.message_id)
        status = ("updated" if event.kind is TelegramEventKind.EDIT
                  else "stored")
        self._pipeline_safe(event, status, source_id)
        return status

    def _pipeline_safe(self, event, status: str, source_id: str) -> None:
        """C1：Item 层是派生态——任何异常（ORICO 掉线等）都不得
        杀死 watcher；原始事实已 commit，失败项由 recover_stranded
        在 ORICO 恢复后补齐（§6.8"Item 保持可恢复状态"）。"""
        if self.pipeline is None:
            return
        try:
            self.pipeline.on_raw(event, status, source_id)
        except Exception as exc:            # noqa: BLE001 —— 容器化边界
            print(f"pipeline error ({status} "
                  f"{source_id}#{event.message_id}): {exc}",
                  file=sys.stderr, flush=True)

    async def reconcile(self, source_id: str) -> int:
        """§5.7 B 路径：只补 start_at 之后、cursor 之前的缺口。"""
        if self.client is None:
            return 0          # 无客户端（纯本地/handoff 模式）：无事可补
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
        if self.pipeline is not None:
            try:
                self.pipeline.recover_stranded()
            except Exception as exc:        # noqa: BLE001 —— 恢复不致命
                print(f"pipeline recovery error: {exc}",
                      file=sys.stderr, flush=True)
        return total

    async def run(self, *, max_ticks: int | None = None,
                  idle_seconds: float = 60.0,
                  reconcile_interval_seconds: float = 300.0,
                  error_backoff_seconds: float = 10.0,
                  flood_wait_cap_seconds: float = 300.0,
                  handoff_scan=None) -> None:
        """live 主循环（§5.7 A+B）：排空 updates，并按墙钟周期
        强制 reconcile 兜底——高频群 updates 不断流时也必须定期
        跑安全网，不能等空闲。max_ticks 仅用于测试/一次性运行。

        瞬时错误（FloodWait/RPC/网络）不退出（§3.1：FloodWait 是
        正常运行状态）：FloodWait 按其秒数（封顶）等待，其余退避
        error_backoff_seconds 后继续下一 tick。
        """
        tick = 0
        last_reconcile = 0.0
        while max_ticks is None or tick < max_ticks:
            tick += 1
            try:
                await self._drain_updates(timeout=idle_seconds)
                now = time.monotonic()
                if now - last_reconcile >= reconcile_interval_seconds:
                    await self.reconcile_all()
                    last_reconcile = now
                    if handoff_scan is not None:
                        try:
                            result = handoff_scan()
                            if inspect.iscoroutine(result):
                                await result
                        except Exception as exc:  # noqa: BLE001 —— C1 防线
                            print(f"handoff scan error: {exc}",
                                  file=sys.stderr, flush=True)
            except TelegramFloodWaitError as exc:
                await asyncio.sleep(
                    min(exc.seconds, flood_wait_cap_seconds))
            except (TelegramRPCError, ConnectionError, OSError):
                await asyncio.sleep(error_backoff_seconds)

    async def _drain_updates(self, timeout: float) -> None:
        if self.client is None:
            await asyncio.sleep(timeout)   # 无客户端：纯周期任务模式
            return
        try:
            async with asyncio.timeout(timeout):
                async for event in self.client.watch_updates():
                    self.handle_event(event)
        except TimeoutError:
            return
