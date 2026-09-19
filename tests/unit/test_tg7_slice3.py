"""TG7 第三片：SHA 去重 + session 锁退避。"""

import asyncio
import sqlite3
from pathlib import Path

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
