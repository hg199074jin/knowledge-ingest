"""TG7：schema 1→2 迁移 + 附件分类学 + 窗口关闭。

冻结约束：迁移幂等、走 maintenance lock、存量误标数据回填、
新表 notifications/digest_state；kind CHECK 扩展为
text/pdf/cloud_link/video/file。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledge_ingest.telegram.client_port import (
    TelegramDocumentRef,
    TelegramEvent,
    TelegramEventKind,
)
from knowledge_ingest.telegram.event_store import (
    SCHEMA_VERSION,
    TelegramEventStore,
)
from knowledge_ingest.telegram.items import classify_kind
from knowledge_ingest.telegram.watcher import TelegramWatcher

# ---------- classify_kind：按 mime/扩展名细分 ----------

def ev(doc):
    return TelegramEvent(kind=TelegramEventKind.NEW, chat_id=-1, message_id=1,
                         message_date=datetime(2026, 9, 20, tzinfo=UTC),
                         document=doc)


def test_tg7_classify_video_by_mime_and_ext():
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=1, file_name="a.mp4",
        mime_type="video/mp4"))) == "video"
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=2, file_name="b.mov",
        mime_type="video/quicktime"))) == "video"
    # 无 mime 但扩展名是视频
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=3, file_name="c.mkv", mime_type=None))) == "video"


def test_tg7_classify_pdf_and_file():
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=4, file_name="报告.pdf",
        mime_type="application/pdf"))) == "pdf"
    # 无类型信息的文档：保守按 pdf（知识安全，走人工审）
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=5, file_name=None, mime_type=None))) == "pdf"
    # 其他文档类型 → file（进库即 skipped_unsupported）
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=6, file_name="pack.zip", mime_type="application/zip"))) == \
        "file"
    assert classify_kind(ev(TelegramDocumentRef(
        document_id=7, file_name="doc.docx",
        mime_type="application/vnd.openxmlformats-officedocument"
                  ".wordprocessingml.document"))) == "file"


# ---------- 迁移：v1 库 → v2（回填 + 新表 + CHECK 扩展） ----------

def _make_v1_db(path: Path) -> None:
    """构造一个 v1 形状的库：mp4 被误标为 pdf 的存量场景。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE tg_sources (source_id TEXT PRIMARY KEY, chat_id INTEGER
        UNIQUE, display_name TEXT NOT NULL DEFAULT '', enabled INTEGER
        NOT NULL DEFAULT 0, start_at TEXT, last_seen_message_id INTEGER
        NOT NULL DEFAULT 0, last_reconciled_at TEXT, created_at TEXT
        NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE tg_messages (source_id TEXT NOT NULL REFERENCES
        tg_sources(source_id), message_id INTEGER NOT NULL, sender_id
        TEXT, message_date TEXT NOT NULL, edited_at TEXT, deleted_at
        TEXT, text TEXT, reply_to_message_id INTEGER, has_document
        INTEGER NOT NULL DEFAULT 0, document_name TEXT, document_mime
        TEXT, document_size_bytes INTEGER, cloud_links_json TEXT,
        raw_fingerprint TEXT, status TEXT NOT NULL DEFAULT 'raw',
        PRIMARY KEY (source_id, message_id));
    CREATE TABLE source_items (item_id TEXT PRIMARY KEY, source_id TEXT
        NOT NULL REFERENCES tg_sources(source_id), kind TEXT NOT NULL
        CHECK (kind IN ('text','pdf','cloud_link')), first_message_id
        INTEGER, last_message_id INTEGER, message_ids_json TEXT NOT NULL
        DEFAULT '[]', created_at TEXT NOT NULL, finalized_at TEXT,
        noise_decision TEXT, interest_decision TEXT, processing_status
        TEXT NOT NULL DEFAULT 'open', materialized_path TEXT,
        knowledge_ingest_job_id TEXT, handoff_completed INTEGER NOT NULL
        DEFAULT 0);
    CREATE TABLE downloads (item_id TEXT NOT NULL REFERENCES
        source_items(item_id), document_message_id INTEGER NOT NULL,
        telegram_document_id TEXT, expected_size_bytes INTEGER,
        local_path TEXT, sha256 TEXT, status TEXT NOT NULL DEFAULT
        'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
        PRIMARY KEY (item_id, document_message_id));
    CREATE TABLE source_ai_budget (source_id TEXT NOT NULL,
        classifier_kind TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, consecutive_empty INTEGER
        NOT NULL DEFAULT 0, consecutive_rate_limit INTEGER NOT NULL
        DEFAULT 0, permits_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (source_id, classifier_kind));
    CREATE TABLE classifier_audit (audit_id INTEGER PRIMARY KEY
        AUTOINCREMENT, item_id TEXT NOT NULL, kind TEXT NOT NULL,
        decision TEXT NOT NULL, reason_code TEXT, confidence REAL,
        policy_version TEXT NOT NULL, classifier_version TEXT NOT NULL,
        created_at TEXT NOT NULL);
    CREATE TABLE reviews (review_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL REFERENCES source_items(item_id), reason
        TEXT NOT NULL, kind TEXT, size_bytes INTEGER, created_at TEXT
        NOT NULL, resolved_at TEXT, decision TEXT CHECK (decision IN
        ('KEEP','SKIP','DOWNLOAD_ONCE')), decision_consumed_at TEXT);
    """)
    now = "2026-09-19T10:00:00+00:00"
    conn.execute("INSERT INTO tg_sources VALUES "
                 "('tg_a', -100, 'A', 1, NULL, 0, NULL, ?, ?)", (now, now))
    # 一条被误标为 pdf 的 MP4 附件消息 + 对应 item（存量误标）
    conn.execute("INSERT INTO tg_messages VALUES ('tg_a', 10, '9', ?, "
                 "NULL, NULL, NULL, NULL, 1, 'clip.mp4', 'video/mp4', "
                 "4096, NULL, NULL, 'raw')", (now,))
    conn.execute("INSERT INTO source_items VALUES ('tg_a:10', 'tg_a', "
                 "'pdf', 10, 10, '[10]', ?, NULL, NULL, 'INCLUDE', "
                 "'interest_include', NULL, NULL, 0)", (now,))
    conn.execute("INSERT INTO downloads VALUES ('tg_a:10', 10, 'doc-77', "
                 "4096, '/x/clip.mp4', 'ff00', 'complete', 1, NULL)")
    conn.execute("INSERT INTO source_items VALUES ('tg_a:11', 'tg_a', "
                 "'pdf', 11, 11, '[11]', ?, NULL, NULL, NULL, "
                 "'skipped_interest', NULL, NULL, 0)", (now,))
    conn.commit()
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()


def test_tg7_migration_backfills_and_extends(tmp_path):
    db = tmp_path / "state.db"
    _make_v1_db(db)
    store = TelegramEventStore(db)
    assert store.user_version() == SCHEMA_VERSION == 2

    # 存量误标回填：mime=video/mp4 的 pdf → video
    row = store.get_source_item("tg_a:10")
    assert row["kind"] == "video"
    # 真 pdf 不受影响（mime 为空、名称非视频的 pdf 保持 pdf）
    row2 = store.get_source_item("tg_a:11")
    assert row2["kind"] == "pdf"

    # 新表存在且事件幂等（event_id 主键）
    assert store.record_notification("e1", "wxpusher", "t", "b") is True
    assert store.record_notification("e1", "wxpusher", "t", "b") is False
    assert store.has_notification("e1") is True
    store.set_digest_state("digest", "2026-09-20T08:00:00+00:00")
    assert store.get_digest_state("digest") == "2026-09-20T08:00:00+00:00"

    # 迁移幂等：重开同一库不再变化
    store.close()
    store2 = TelegramEventStore(db)
    assert store2.user_version() == 2
    store2.close()


def test_tg7_migration_runs_under_maintenance_lock(tmp_path, monkeypatch):
    """迁移持锁：与 watcher/其他写方互斥（§8.4）。"""
    from knowledge_ingest.telegram import locks

    db = tmp_path / "state.db"
    _make_v1_db(db)
    taken = []
    real_lock = locks.maintenance_lock

    import contextlib

    @contextlib.contextmanager
    def spy_lock(path):
        taken.append(str(path))
        with real_lock(path):
            yield

    monkeypatch.setattr("knowledge_ingest.telegram.event_store"
                        ".maintenance_lock", spy_lock)
    store = TelegramEventStore(db)
    assert taken, "迁移未持 maintenance lock"
    store.close()


# ---------- 窗口关闭：安静源的 open item 不再永悬 ----------

def test_tg7_stale_open_windows_close_on_reconcile(tmp_path):
    store = TelegramEventStore(tmp_path / "state.db")
    store.add_source("tg_a", chat_id=-100, display_name="A", start_at=None)
    from knowledge_ingest.telegram.items import SourceItemPipeline

    pipeline = SourceItemPipeline(store, data_root=tmp_path,
                                  orico_check=lambda: True)
    watcher = TelegramWatcher(store, client=None, pipeline=pipeline)
    old = datetime.now(UTC) - timedelta(seconds=600)   # 窗口早已过期
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-100, message_id=1,
        message_date=old, sender_id=9, text="安静源的孤条消息"))
    assert store.get_source_item("tg_a:1")["processing_status"] == "open"

    closed = pipeline.close_stale_windows()
    assert closed >= 1
    item = store.get_source_item("tg_a:1")
    assert item["processing_status"] in ("materialized", "skipped_noise",
                                         "noise_review", "empty_item")

    # 新鲜窗口不受影响
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.NEW, chat_id=-100, message_id=2,
        message_date=datetime.now(UTC), sender_id=9, text="新鲜消息"))
    assert store.get_source_item("tg_a:2")["processing_status"] == "open"
