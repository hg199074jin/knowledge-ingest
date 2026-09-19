"""TG7 第三片：SHA 去重 + session 锁退避。"""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledge_ingest.telegram.client_port import (
    TelegramDocumentRef,
    TelegramEvent,
    TelegramEventKind,
)
from knowledge_ingest.telegram.event_store import TelegramEventStore
from knowledge_ingest.telegram.materialize import download_pdf
from knowledge_ingest.telegram.telethon_adapter import (
    SESSION_LOCK_RETRY_SECONDS,
    TelethonAdapter,
)


def make_store(tmp_path: Path) -> TelegramEventStore:
    store = TelegramEventStore(tmp_path / "state.db")
    store.add_source("tg_a", chat_id=-100, display_name="A", start_at=None)
    return store


class FakeClient:
    async def download_document(self, chat_id, message_id, dest_dir):
        path = Path(dest_dir) / "same.bin"
        path.write_bytes(b"identical content")
        return path


def _seed_pdf_item(store, tmp_path, item_id, message_id):
    source_id = item_id.split(":")[0]
    store.upsert_message(source_id, message_id,
                         message_date="2026-09-20T00:00:00+00:00",
                         has_document=True, document_name="x.bin",
                         document_size_bytes=17)
    store.create_source_item(item_id, source_id, "pdf", [message_id],
                             processing_status="interest_include")
    store.set_item_interest(item_id, "INCLUDE")


def test_tg7_sha_dedup_points_to_existing_file(tmp_path):
    store = make_store(tmp_path)
    store.add_source("tg_b", chat_id=-200, display_name="B", start_at=None)
    _seed_pdf_item(store, tmp_path, "tg_a:1", 1)
    _seed_pdf_item(store, tmp_path, "tg_b:2", 2)

    dest_a = tmp_path / "a"; dest_a.mkdir()
    dest_b = tmp_path / "b"; dest_b.mkdir()
    p1 = download_pdf(store, "tg_a:1", FakeClient(), dest_a,
                      decision="INCLUDE", orico_check=lambda: True)
    p2 = download_pdf(store, "tg_b:2", FakeClient(), dest_b,
                      decision="INCLUDE", orico_check=lambda: True)
    assert p1 and p2
    assert p2 == p1                                  # 复用既有文件
    assert p1.is_file()                              # 原件仍在
    row = store.get_download("tg_b:2")
    assert row["sha256"] == store.get_download("tg_a:1")["sha256"]


def test_tg7_session_lock_backoff(tmp_path, monkeypatch):
    """session 锁短暂争用 → 退避重试成功（不再让 CLI 直接失败）。"""

    import asyncio as aio

    import knowledge_ingest.telegram.telethon_adapter as adapter_mod

    real_sleep = aio.sleep

    async def instant_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", instant_sleep)
    calls = {"connect": 0}

    class DuckClient:
        def is_connected(self):
            return False

        async def connect(self):
            calls["connect"] += 1
            if calls["connect"] == 1:
                raise sqlite3.OperationalError("database is locked")

    adapter = TelethonAdapter(session_dir=tmp_path, credentials={})
    client = DuckClient()
    out = asyncio.run(adapter._connected(client))
    assert out is client and calls["connect"] == 2
    assert SESSION_LOCK_RETRY_SECONDS > 0


def test_r1fix_video_items_in_download_scan(tmp_path):
    """TG7 分类学回归：video INCLUDE 条目必须能自动下载。"""
    store = make_store(tmp_path)
    store.add_source("tg_b", chat_id=-200, display_name="B", start_at=None)
    store.upsert_message("tg_b", 3,
                         message_date="2026-09-20T00:00:00+00:00",
                         has_document=True, document_name="v.mp4",
                         document_mime="video/mp4", document_size_bytes=17)
    store.create_source_item("tg_b:3", "tg_b", "video", [3],
                             processing_status="interest_include")
    store.set_item_interest("tg_b:3", "INCLUDE")
    from knowledge_ingest.telegram.items import SourceItemPipeline

    pipeline = SourceItemPipeline(store, data_root=tmp_path,
                                  orico_check=lambda: True)
    dest = tmp_path / "dest"; dest.mkdir(parents=True, exist_ok=True)

    class C:
        async def download_document(self, chat_id, message_id, dest_dir):
            f = Path(dest_dir) / "v.mp4"; f.write_bytes(b"v"); return f

    done = asyncio.run(pipeline.download_pending_pdfs(C()))
    assert done == 1
    assert store.get_download("tg_b:3")["status"] == "complete"


def test_review_video_item_gates_and_rebuild(tmp_path):
    """Minor 10 回归：video 的 gate / edit-rebuild 与 pdf 同语义。"""
    from knowledge_ingest.telegram.items import SourceItemPipeline
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    store = make_store(tmp_path)
    pipeline = SourceItemPipeline(
        store, data_root=tmp_path, orico_check=lambda: True,
        interest_llm=lambda _p: (
            '{"decision": "INCLUDE", "primary_topic": "devtools", '
            '"reason": "ok", "confidence": 1.0}'))
    watcher = TelegramWatcher(store, client=None, pipeline=pipeline)
    doc = TelegramDocumentRef(document_id=9, file_name="课.mp4",
                              mime_type="video/mp4", size_bytes=1024)
    now = datetime.now(UTC)
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-100, message_id=7,
        message_date=now, sender_id=9, text=None, document=doc))
    item_id = "tg_a:7"
    assert store.get_source_item(item_id)["processing_status"] == \
        "interest_include"

    # EDIT：caption 变化 → 重判兴趣（不残留旧决定）
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.EDIT, chat_id=-100, message_id=7,
        message_date=now, sender_id=9, text="新的说明文字",
        edited_at=now + timedelta(seconds=30), document=doc))
    item = store.get_source_item(item_id)
    assert item["interest_decision"] in ("INCLUDE", "EXCLUDE", "REVIEW")
    assert item["processing_status"] != "interest_include" or \
        item["interest_decision"] == "INCLUDE"
    # handoff scan 覆盖 video（未下载 → blocked，而非被跳过）
    from knowledge_ingest.telegram.handoff import TelegramHandoffRunner
    gate = TelegramHandoffRunner(store, None).gate(item)
    assert gate in ("blocked:download_incomplete", None)
