"""TG4: Source Item Builder / materialize / DOWNLOAD_ONCE（方案 §6）。

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §9/§12/§13/
§20/§22；全部驱动走 TelegramWatcher + pipeline（生产同路径）。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_ingest.telegram.client_port import (
    TelegramDocumentRef,
    TelegramEvent,
    TelegramEventKind,
)
from knowledge_ingest.telegram.event_store import TelegramEventStore
from knowledge_ingest.telegram.materialize import (
    OricoUnavailableError,
    download_pdf,
    materialize_text_item,
)
from knowledge_ingest.telegram.telethon_adapter import extract_cloud_links
from knowledge_ingest.telegram.watcher import TelegramWatcher

T0 = "2026-09-18T10:00:00+08:00"

_BASE = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)


def dt(offset_seconds):
    """窗口测试统一用锚点偏移量（秒）。"""
    return _BASE + timedelta(seconds=offset_seconds)


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


def make_event(message_id=7, second=1, text="知识正文", document=None):
    return TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-1001234567890,
        message_id=message_id, message_date=dt(second),
        sender_id=99, text=text, document=document,
        cloud_links=extract_cloud_links(text))


class FakeDownloadClient:
    def __init__(self, payload=b"%PDF-1.4 fake"):
        self.payload = payload

    async def download_document(self, chat_id, message_id, dest_dir):
        path = Path(dest_dir) / "document.pdf"
        path.write_bytes(self.payload)
        return path


def make_pipeline(tmp_path: Path, store: TelegramEventStore,
                  client=None, orico_ok=True):
    from knowledge_ingest.telegram.items import SourceItemPipeline

    pipeline = SourceItemPipeline(
        store, data_root=tmp_path, window_seconds=300,
        orico_check=(lambda: orico_ok))
    watcher = TelegramWatcher(store, client=client)
    watcher.pipeline = pipeline
    return pipeline, watcher


def ready_store(tmp_path: Path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     display_name="AI", start_at="2026-09-18T09:00:00+08:00")
    return store


def text_item_count(store):
    return len([row for row in store._conn.execute(
        "SELECT * FROM source_items WHERE kind='text'")])


# ---------- 5 分钟固定窗口（first-message anchored） ----------

def test_window_joins_within_anchor_300s(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="a"))
    watcher.handle_event(make_event(message_id=2, second=2, text="b"))  # +1s
    watcher.handle_event(make_event(message_id=3, second=60, text="c"))  # +59s
    assert text_item_count(store) == 1
    item = store.find_open_text_item("tg_a")
    assert item["message_ids_json"] == "[1, 2, 3]"


def test_window_splits_at_301s_not_rolling(tmp_path):
    """锚定窗口：+301s 拆新 Item；滚动窗口（按上一条算）不得发生。"""
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="a"))
    watcher.handle_event(make_event(message_id=2, second=30, text="b"))
    # 若滚动：距 b 仅 41s 会并入；锚定：距 a 121s… 仍在 300 内 → 并入
    watcher.handle_event(make_event(message_id=3, second=121, text="c"))
    assert text_item_count(store) == 1
    # 距 a 301s → 必拆；且旧窗口关闭
    watcher.handle_event(make_event(message_id=4, second=302, text="d"))
    assert text_item_count(store) == 2
    old = store.get_source_item("tg_a:1")
    assert old["processing_status"] == "materialized"  # 关窗即物化
    assert store.find_open_text_item("tg_a")["item_id"] == "tg_a:4"


def test_window_boundary_300s_exact_joins_301_splits(tmp_path):
    store2 = ready_store(tmp_path / "w2")
    _pipeline2, watcher2 = make_pipeline(tmp_path / "w2", store2)
    watcher2.handle_event(make_event(message_id=1, second=0))
    watcher2.handle_event(make_event(message_id=2, second=300))   # 恰好 300 → 并
    assert text_item_count(store2) == 1
    watcher2.handle_event(make_event(message_id=3, second=301))   # 301 → 拆
    assert text_item_count(store2) == 2


def test_window_different_sender_splits(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1))
    other = make_event(message_id=2, second=2)
    other = TelegramEvent(kind=TelegramEventKind.NEW,
                          chat_id=other.chat_id, message_id=2,
                          message_date=other.message_date, sender_id=777,
                          text="别人的")
    watcher.handle_event(other)
    assert text_item_count(store) == 2  # 不同 sender 不合并


def test_pdf_breaks_text_merge_but_text_window_survives(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    doc = TelegramDocumentRef(document_id=5, file_name="课.pdf",
                              mime_type="application/pdf", size_bytes=1024)
    watcher.handle_event(make_event(message_id=1, second=1, text="前言"))
    watcher.handle_event(make_event(message_id=2, second=2, text=None,
                                    document=doc))
    watcher.handle_event(make_event(message_id=3, second=3, text="继续"))
    kinds = [row["kind"] for row in store._conn.execute(
        "SELECT kind FROM source_items ORDER BY item_id")]
    assert kinds.count("pdf") == 1 and kinds.count("text") == 1
    text_item = store.find_open_text_item("tg_a")
    assert text_item["message_ids_json"] == "[1, 3]"  # PDF 未混入


# ---------- cloud link / provider_guess / Stub ----------

def test_cloud_link_kind_and_provider_guess(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(
        message_id=10, text="课程 https://pan.quark.cn/s/abc 取 x1"))
    item = store.get_source_item("tg_a:10")
    assert item["kind"] == "cloud_link"
    assert item["processing_status"] == "PENDING_RESOURCE"
    from knowledge_ingest.telegram.items import provider_guess
    assert provider_guess(["https://pan.quark.cn/s/abc"]) == "quark"
    assert provider_guess(["https://pan.baidu.com/s/xyz"]) == "baidu"
    assert provider_guess(["https://example.com/file"]) == "unknown"


def test_resource_collection_stub_no_download(tmp_path):
    store = ready_store(tmp_path)
    client = FakeDownloadClient()
    _pipeline, watcher = make_pipeline(tmp_path, store, client=client)
    watcher.handle_event(make_event(
        message_id=20,
        text="2755 份 / 176G 英语动画 https://pan.quark.cn/s/big"))
    item = store.get_source_item("tg_a:20")
    assert item["kind"] == "cloud_link"
    assert item["processing_status"] == "PENDING_RESOURCE"
    from knowledge_ingest.telegram.items import is_resource_collection_candidate
    assert is_resource_collection_candidate(
        "2755 份 / 176G 英语动画") is True
    assert is_resource_collection_candidate("普通一条链接") is False
    assert store.list_downloads() == []  # 绝不自动下载


# ---------- materialize ----------

def test_materialize_text_item_header_and_body(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="第一段"))
    watcher.handle_event(make_event(message_id=2, second=5, text="第二段"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    path = materialize_text_item(store, item_id, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert content.startswith("# Telegram Knowledge Item")
    assert "第一段" in content and "第二段" in content
    assert "tg_a" not in content  # provenance 不混入正文
    assert store.get_source_item(item_id)["materialized_path"] == str(path)


def test_materialize_excludes_deleted_messages(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="保留"))
    watcher.handle_event(make_event(message_id=2, second=2, text="被删"))
    store.mark_message_deleted("tg_a", 2)
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    content = materialize_text_item(store, item_id, tmp_path).read_text(
        encoding="utf-8")
    assert "保留" in content and "被删" not in content


def test_materialize_orico_fail_closed(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store, orico_ok=False)
    watcher.handle_event(make_event(message_id=1, second=1, text="x"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    with pytest.raises(OricoUnavailableError):
        materialize_text_item(store, item_id, tmp_path, orico_check=(
            lambda: False))


# ---------- 编辑 / 删除四态（§6.4） ----------

def test_edit_case_a_open_window_absorbed(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="v1"))
    edited = TelegramEvent(kind=TelegramEventKind.EDIT,
                           chat_id=-1001234567890, message_id=1,
                           message_date=dt(1), sender_id=99, text="v2",
                           edited_at=dt(30))
    watcher.handle_event(edited)
    assert text_item_count(store) == 1  # 不产生第二个 Item
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    content = materialize_text_item(store, "tg_a:1", tmp_path).read_text(
        encoding="utf-8")
    assert "v2" in content and "v1" not in content


def test_edit_case_b_rebuild_same_item_id(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="v1"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    materialize_text_item(store, item_id, tmp_path)
    old_path = store.get_source_item(item_id)["materialized_path"]

    edited = TelegramEvent(kind=TelegramEventKind.EDIT,
                           chat_id=-1001234567890, message_id=1,
                           message_date=dt(1), sender_id=99, text="新版",
                           edited_at=dt(60))
    watcher.handle_event(edited)
    assert text_item_count(store) == 1  # 同一 item_id 重建
    assert store.get_source_item(item_id)["processing_status"] == \
        "materialized"
    assert Path(old_path).read_text(encoding="utf-8").find("新版") >= 0


def test_edit_case_c_handoff_creates_review(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="v1"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    materialize_text_item(store, item_id, tmp_path)
    store.set_item_handoff(item_id, "job-123")  # 已进入 K2C 链路

    edited = TelegramEvent(kind=TelegramEventKind.EDIT,
                           chat_id=-1001234567890, message_id=1,
                           message_date=dt(1), sender_id=99, text="v2",
                           edited_at=dt(60))
    watcher.handle_event(edited)
    review = store.list_open_reviews()[0]
    assert review["item_id"] == item_id
    assert review["reason"] == "SOURCE_EDITED_AFTER_HANDOFF"
    assert "v1" in Path(store.get_source_item(
        item_id)["materialized_path"]).read_text(encoding="utf-8")  # Corpus 不动


def test_delete_case_blocks_pending_handoff(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="x"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.finalize_item(item_id)
    deleted = TelegramEvent(kind=TelegramEventKind.DELETE,
                            chat_id=-1001234567890, message_id=1)
    watcher.handle_event(deleted)
    assert store.get_source_item(item_id)["processing_status"] == \
        "deleted_source"  # 阻止尚未开始的自动 handoff


def test_delete_case_post_handoff_provenance_only(tmp_path):
    store = ready_store(tmp_path)
    _pipeline, watcher = make_pipeline(tmp_path, store)
    watcher.handle_event(make_event(message_id=1, second=1, text="x"))
    item_id = store.find_open_text_item("tg_a")["item_id"]
    store.set_item_handoff(item_id, "job-9")
    deleted = TelegramEvent(kind=TelegramEventKind.DELETE,
                            chat_id=-1001234567890, message_id=1)
    watcher.handle_event(deleted)
    item = store.get_source_item(item_id)
    assert item["handoff_completed"] == 1  # 不跨层回滚
    assert any(m["deleted_at"] for m in store.get_item_messages(item_id))


# ---------- DOWNLOAD_ONCE 闸门 + 下载（§6.7） ----------

def test_download_within_50mib_flows(tmp_path):
    store = ready_store(tmp_path)
    doc = TelegramDocumentRef(document_id=5, file_name="课.pdf",
                              mime_type="application/pdf",
                              size_bytes=50 * 1024 * 1024)
    payload = b"%" + b"x" * (1024 * 1024)  # 1 MiB 真实字节
    _pipeline, watcher = make_pipeline(
        tmp_path, store, client=FakeDownloadClient(payload))
    watcher.handle_event(make_event(message_id=7, text=None, document=doc))
    download_pdf(store, "tg_a:7", FakeDownloadClient(payload),
                        tmp_path, decision="INCLUDE", orico_check=lambda: True)
    row = store.list_downloads()[0]
    assert row["status"] == "complete"
    assert row["attempts"] == 1
    assert row["sha256"] and len(row["sha256"]) == 64


def test_over_50mib_requires_download_once(tmp_path):
    store = ready_store(tmp_path)
    doc = TelegramDocumentRef(document_id=5, file_name="大课.pdf",
                              mime_type="application/pdf",
                              size_bytes=80 * 1024 * 1024)
    _pipeline, watcher = make_pipeline(
        tmp_path, store, client=FakeDownloadClient(b"big"))
    watcher.handle_event(make_event(message_id=9, text=None, document=doc))
    item_id = "tg_a:9"

    assert store.download_gate(item_id) == "denied"  # >50MiB 无授权不下载
    assert download_pdf(store, item_id, FakeDownloadClient(b"big"),
                        tmp_path, decision="INCLUDE",
                        orico_check=lambda: True) is None
    review_id = store.create_review(item_id, "size over 50 MiB",
                                    kind="pdf",
                                    size_bytes=80 * 1024 * 1024)
    store.resolve_review(review_id, "DOWNLOAD_ONCE")

    path = download_pdf(store, item_id, FakeDownloadClient(b"big"),
                        tmp_path, decision="INCLUDE",
                        orico_check=lambda: True)
    assert path is not None and path.exists()
    row = store.list_downloads()[0]
    assert row["status"] == "complete"
    review = store.get_review(review_id)
    assert review["decision_consumed_at"] is not None  # 授权已消费


def test_download_once_retry_replay_no_double(tmp_path):
    store = ready_store(tmp_path)
    doc = TelegramDocumentRef(document_id=5, file_name="大.pdf",
                              mime_type="application/pdf",
                              size_bytes=80 * 1024 * 1024)
    _pipeline, watcher = make_pipeline(
        tmp_path, store, client=FakeDownloadClient(b"big"))
    watcher.handle_event(make_event(message_id=9, text=None, document=doc))
    item_id = "tg_a:9"
    review_id = store.create_review(item_id, "oversize", kind="pdf",
                                    size_bytes=80 * 1024 * 1024)
    store.resolve_review(review_id, "DOWNLOAD_ONCE")

    class FlakyClient(FakeDownloadClient):
        def __init__(self):
            super().__init__(b"big")
            self.calls = 0

        async def download_document(self, chat_id, message_id, dest_dir):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient network")
            return await super().download_document(chat_id, message_id,
                                                   dest_dir)

    flaky = FlakyClient()
    with pytest.raises(RuntimeError):
        download_pdf(store, item_id, flaky, tmp_path, decision="INCLUDE",
                     orico_check=lambda: True)
    row = store.list_downloads()[0]
    assert row["attempts"] == 1 and row["status"] == "failed"
    assert row["last_error"] == "transient network"

    # 同授权 retry（watcher 重启/续传）→ 沿用同一 downloads 行
    path = download_pdf(store, item_id, flaky, tmp_path,
                        decision="INCLUDE", orico_check=lambda: True)
    assert path is not None
    rows = store.list_downloads()
    assert len(rows) == 1 and rows[0]["status"] == "complete"
    assert flaky.calls == 2

    # 完成后 replay → 不再二次下载
    path2 = download_pdf(store, item_id, flaky, tmp_path,
                         decision="INCLUDE", orico_check=lambda: True)
    assert path2 is None
    assert flaky.calls == 2
    assert store.count_messages("tg_a") == 1  # 无重复 Item/Job 副作用


def test_pipeline_default_orico_check_resolves(tmp_path, monkeypatch):
    """未注入 orico_check 时走真实 orico_ready（默认路径不许 NameError）。"""
    from knowledge_ingest.telegram import materialize as mat_mod
    from knowledge_ingest.telegram.items import SourceItemPipeline

    store = ready_store(tmp_path)
    pipeline = SourceItemPipeline(store, data_root=tmp_path)  # orico_check=None
    watcher = TelegramWatcher(store, pipeline=pipeline)
    monkeypatch.setattr(mat_mod, "ORICO_ROOT", tmp_path)  # 模拟已挂载

    watcher.handle_event(make_event(message_id=1, second=1, text="x"))
    watcher.handle_event(make_event(message_id=2, second=302, text="y"))
    assert store.get_source_item("tg_a:1")["materialized_path"] is not None
