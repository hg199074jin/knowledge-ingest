"""TG2: Telegram State Core — SQLite 事实层 / Registry / review & status CLI.

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §6/§8/§16/§23。
并发契约 §8.4：WAL + foreign_keys + busy_timeout + maintenance lock。
"""

import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import (
    _cmd_telegram_review_list,
    _cmd_telegram_review_resolve,
    _cmd_telegram_sources_list,
    _cmd_telegram_status,
)
from knowledge_ingest.config import AppConfig
from knowledge_ingest.telegram.event_store import (
    MessageBeforeStartError,
    ReviewConflictError,
    TelegramEventStore,
    UnsupportedSchemaVersionError,
)
from knowledge_ingest.telegram.locks import (
    MaintenanceLockBusy,
    maintenance_lock,
)
from knowledge_ingest.telegram.registry import (
    set_enabled,
)

T0 = "2026-09-18T10:00:00+08:00"        # start_at
T1 = "2026-09-18T10:01:00+08:00"        # after start
T_BEFORE = "2026-09-18T09:59:00+08:00"  # before start


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


def make_source(store: TelegramEventStore, source_id: str = "tg_a") -> str:
    store.add_source(source_id=source_id, chat_id=-1001234567890,
                     display_name="AI探索指南", start_at=T0)
    return source_id


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive", "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


# ---------- 初始化 / PRAGMA ----------

def test_db_init_idempotent(tmp_path: Path):
    db = tmp_path / "telegram" / "state.db"
    TelegramEventStore(db)
    second = TelegramEventStore(db)  # 再打开不破坏
    second.add_source(source_id="tg_x", start_at=T0)
    assert TelegramEventStore(db).count_messages("tg_x") == 0


def test_user_version_is_1(tmp_path: Path):
    store = make_store(tmp_path)
    assert store.user_version() == 1


def test_wal_mode_enabled(tmp_path: Path):
    store = make_store(tmp_path)
    assert store.journal_mode() == "wal"


def test_foreign_keys_enforced(tmp_path: Path):
    """FK 关闭时该插入会静默成孤儿行；开启后必须抛 IntegrityError。"""
    store = make_store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_source_item("item-x", "ghost", "text", [1])


def test_busy_timeout_set(tmp_path: Path):
    store = make_store(tmp_path)
    assert store.busy_timeout_ms() == 5000


def test_unknown_schema_version_fail_fast(tmp_path: Path):
    db = tmp_path / "telegram" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    with pytest.raises(UnsupportedSchemaVersionError):
        TelegramEventStore(db)


# ---------- sources ----------

def test_source_add_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0)
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0)
    assert len(store.list_sources()) == 1


def test_source_add_twice_keeps_original_start_at(tmp_path: Path):
    """start_at 一经写入不倒退：重复 add 不得改写启用边界。"""
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", start_at=T0)
    store.add_source(source_id="tg_a", start_at=T_BEFORE)
    assert store.get_source("tg_a")["start_at"] == T0


def test_enable_disable_roundtrip(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    set_enabled(store, sid, False)
    assert store.get_source(sid)["enabled"] == 0
    set_enabled(store, sid, True)
    assert store.get_source(sid)["enabled"] == 1


# ---------- messages / start_at ----------

def test_message_upsert_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.upsert_message(sid, 7, message_date=T1, text="v1")
    store.upsert_message(sid, 7, message_date=T1, text="v2",
                         edited_at="2026-09-18T10:03:00+08:00")
    assert store.count_messages(sid) == 1
    row = store.get_message(sid, 7)
    assert row["text"] == "v2"
    assert row["edited_at"] == "2026-09-18T10:03:00+08:00"


def test_start_at_boundary_rejects_older_message(tmp_path: Path):
    """冻结 §6.3：message_date < start_at 的消息永不进入事实层。"""
    store = make_store(tmp_path)
    sid = make_source(store)
    with pytest.raises(MessageBeforeStartError):
        store.upsert_message(sid, 1, message_date=T_BEFORE, text="旧消息")
    assert store.count_messages(sid) == 0


def test_message_delete_marks_not_deletes(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.upsert_message(sid, 9, message_date=T1, text="正文")
    store.mark_message_deleted(sid, 9,
                               deleted_at="2026-09-18T11:00:00+08:00")
    row = store.get_message(sid, 9)
    assert row is not None  # 行仍在
    assert row["deleted_at"] == "2026-09-18T11:00:00+08:00"


# ---------- items / downloads ----------

def test_source_item_roundtrip(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "text", [7, 8])
    item = store.get_source_item("item-1")
    assert item["kind"] == "text"
    assert item["processing_status"] == "open"
    store.set_item_processing_status("item-1", "PENDING_RESOURCE")
    assert store.get_source_item("item-1")["processing_status"] == \
        "PENDING_RESOURCE"


def test_downloads_composite_pk(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    store.upsert_download("item-1", 9, status="downloading", attempts=1)
    store.upsert_download("item-1", 9, status="complete", attempts=2,
                          sha256="abc")
    rows = store.list_downloads()
    assert len(rows) == 1
    assert rows[0]["status"] == "complete"
    assert rows[0]["attempts"] == 2


# ---------- reviews ----------

def test_review_lifecycle(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    rid = store.create_review("item-1", "size over 50 MiB",
                              kind="pdf", size_bytes=80 * 1024 * 1024)
    assert [r["review_id"] for r in store.list_open_reviews()] == [rid]
    outcome = store.resolve_review(rid, "KEEP")
    assert outcome == "resolved"
    row = store.get_review(rid)
    assert row["decision"] == "KEEP"
    assert row["resolved_at"] is not None


def test_review_list_only_unresolved(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    store.create_source_item("item-2", sid, "pdf", [10])
    rid1 = store.create_review("item-1", "r1")
    store.create_review("item-2", "r2")
    store.resolve_review(rid1, "SKIP")
    open_items = {r["item_id"] for r in store.list_open_reviews()}
    assert open_items == {"item-2"}


def test_review_resolve_same_decision_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    rid = store.create_review("item-1", "r")
    assert store.resolve_review(rid, "KEEP") == "resolved"
    assert store.resolve_review(rid, "KEEP") == "no-op"
    row = store.get_review(rid)
    assert row["decision"] == "KEEP"


def test_review_resolve_conflict_rejected(tmp_path: Path):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    rid = store.create_review("item-1", "r")
    store.resolve_review(rid, "SKIP")
    with pytest.raises(ReviewConflictError):
        store.resolve_review(rid, "KEEP")


def test_download_once_persists_authorization_without_download(tmp_path: Path):
    """DOWNLOAD_ONCE 只落 item 级授权事实，TG2 不触发任何下载。"""
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-big", sid, "pdf", [9])
    rid = store.create_review("item-big", "size over 50 MiB",
                              kind="pdf", size_bytes=80 * 1024 * 1024)
    outcome = store.resolve_review(rid, "DOWNLOAD_ONCE")
    assert outcome == "resolved"
    row = store.get_review(rid)
    assert row["decision"] == "DOWNLOAD_ONCE"
    assert row["decision_consumed_at"] is None  # 未消费，TG4 负责消费
    assert store.list_downloads() == []  # 没有任何下载副作用


def test_review_unknown_id_rejected(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.resolve_review(424242, "KEEP")


# ---------- maintenance lock ----------

def test_maintenance_lock_blocks_second_holder(tmp_path: Path):
    db = tmp_path / "telegram" / "state.db"
    with maintenance_lock(db), pytest.raises(MaintenanceLockBusy), maintenance_lock(db):
        pass
    # 释放后可再次获取
    with maintenance_lock(db):
        pass


# ---------- status 只读 ----------

def test_status_summary_readonly_and_empty_ok(tmp_path: Path):
    store = make_store(tmp_path)
    empty = store.status_summary()
    assert empty["schema_version"] == 1
    assert empty["sources_enabled"] == 0
    assert empty["open_reviews"] == 0

    sid = make_source(store)
    store.create_source_item("item-1", sid, "cloud_link", [3],
                             processing_status="PENDING_RESOURCE")
    store.upsert_download("item-1", 3, status="pending")
    before = store.status_summary()
    after = store.status_summary()
    assert before == after  # 两次调用结果一致（只读）
    assert after["sources_enabled"] == 1
    assert after["pending_resources"] == 1
    assert after["downloads_by_status"] == {"pending": 1}


# ---------- CLI ----------

def test_cli_telegram_status_empty_db(tmp_path, capsys):
    config = make_config(tmp_path)
    rc = _cmd_telegram_status(config, Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "schema" in out


def test_cli_sources_list_and_status_with_data(tmp_path, capsys):
    config = make_config(tmp_path)
    db = config.pipeline_root / "telegram" / "state.db"
    store = TelegramEventStore(db)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "cloud_link", [3],
                             processing_status="PENDING_RESOURCE")
    rc = _cmd_telegram_sources_list(config, Namespace())
    assert rc == 0
    assert "tg_a" in capsys.readouterr().out
    rc = _cmd_telegram_status(config, Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "PENDING_RESOURCE" in out or "pending_resources" in out


def test_cli_review_resolve_flow(tmp_path, capsys):
    config = make_config(tmp_path)
    db = config.pipeline_root / "telegram" / "state.db"
    store = TelegramEventStore(db)
    sid = make_source(store)
    store.create_source_item("item-big", sid, "pdf", [9])
    rid = store.create_review("item-big", "size over 50 MiB", kind="pdf",
                              size_bytes=80 * 1024 * 1024)

    rc = _cmd_telegram_review_list(config, Namespace())
    assert rc == 0
    assert "size over 50 MiB" in capsys.readouterr().out

    rc = _cmd_telegram_review_resolve(
        config, Namespace(review_id=rid, decision="DOWNLOAD_ONCE"))
    assert rc == 0
    row = store.get_review(rid)
    assert row["decision"] == "DOWNLOAD_ONCE"
    assert row["decision_consumed_at"] is None
    # 未消费的 DOWNLOAD_ONCE 在 status 中可见
    _cmd_telegram_status(config, Namespace())
    assert "DOWNLOAD_ONCE" in capsys.readouterr().out
