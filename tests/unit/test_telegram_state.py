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
    db = config.pipeline_root / "telegram" / "state.db"
    TelegramEventStore(db)  # 已初始化但全空的库
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


# ---------- 评审修复（review round 1） ----------

def test_review_resolve_lost_race_does_not_overwrite(tmp_path, monkeypatch):
    """并发竞态：读取 open 行后另一连接先解决为 SKIP，
    本连接的 UPDATE 必须被 resolved_at IS NULL 守卫拦下。"""
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    rid = store.create_review("item-1", "race")

    other = TelegramEventStore(store.db_path)
    other.resolve_review(rid, "SKIP")

    stale = dict(store.get_review(rid))
    stale["resolved_at"] = None
    stale["decision"] = None  # 模拟 A 在 B 提交前读到的 open 行
    monkeypatch.setattr(store, "get_review", lambda _rid: stale)

    with pytest.raises(ReviewConflictError):
        store.resolve_review(rid, "KEEP")
    assert other.get_review(rid)["decision"] == "SKIP"  # 未被覆盖


def test_review_resolve_lost_race_same_decision_noop(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    sid = make_source(store)
    store.create_source_item("item-1", sid, "pdf", [9])
    rid = store.create_review("item-1", "race")

    other = TelegramEventStore(store.db_path)
    other.resolve_review(rid, "KEEP")

    stale = dict(store.get_review(rid))
    stale["resolved_at"] = None
    stale["decision"] = None
    monkeypatch.setattr(store, "get_review", lambda _rid: stale)

    assert store.resolve_review(rid, "KEEP") == "no-op"


def test_add_source_chat_id_conflict_raises(tmp_path):
    """chat_id 已被其他 source 占用时不得静默吞掉注册。"""
    store = make_store(tmp_path)
    make_source(store, "tg_a")  # chat_id=-1001234567890
    with pytest.raises(ValueError, match="tg_a"):
        store.add_source(source_id="tg_b", chat_id=-1001234567890,
                         start_at=T1)
    assert store.get_source("tg_b") is None


def test_add_source_rebind_chat_id_raises(tmp_path):
    store = make_store(tmp_path)
    make_source(store, "tg_a")
    with pytest.raises(ValueError, match="chat_id"):
        store.add_source(source_id="tg_a", chat_id=-999, start_at=T1)
    assert store.get_source("tg_a")["chat_id"] == -1001234567890


def test_add_source_fills_missing_chat_id(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_c", start_at=T0)
    store.add_source(source_id="tg_c", chat_id=-777, start_at=T0)
    assert store.get_source("tg_c")["chat_id"] == -777


def test_status_includes_last_seen_summary(tmp_path):
    """方案 §4.6：status 至少可见 last_seen / last_reconciled 摘要。"""
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0,
                     last_seen_message_id=42)
    summary = store.status_summary()
    entry = summary["sources_last_seen"][0]
    assert entry["source_id"] == "tg_a"
    assert entry["last_seen_message_id"] == 42
    assert "last_reconciled_at" in entry


def test_message_delete_keeps_earliest_timestamp(tmp_path):
    """重复 DELETE 幂等：保留最早 deleted_at（审计事实，§18/§20）。"""
    store = make_store(tmp_path)
    sid = make_source(store)
    store.upsert_message(sid, 9, message_date=T1, text="正文")
    store.mark_message_deleted(sid, 9, deleted_at=T1)
    store.mark_message_deleted(sid, 9,
                               deleted_at="2026-09-18T23:00:00+08:00")
    assert store.get_message(sid, 9)["deleted_at"] == T1


def test_readonly_cli_does_not_create_db(tmp_path, capsys):
    """方案 §4.6：status/sources list/review list 只读，不创建 state.db。"""
    config = make_config(tmp_path)
    db = config.pipeline_root / "telegram" / "state.db"
    assert _cmd_telegram_status(config, Namespace()) == 0
    assert "not initialized" in capsys.readouterr().out
    assert not db.exists()
    assert _cmd_telegram_sources_list(config, Namespace()) == 0
    capsys.readouterr()
    assert _cmd_telegram_review_list(config, Namespace()) == 0
    capsys.readouterr()
    assert not db.exists()


def test_cli_status_renders_last_seen(tmp_path, capsys):
    config = make_config(tmp_path)
    db = config.pipeline_root / "telegram" / "state.db"
    store = TelegramEventStore(db)
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0,
                     last_seen_message_id=7)
    assert _cmd_telegram_status(config, Namespace()) == 0
    out = capsys.readouterr().out
    assert "last_seen" in out
    assert "tg_a" in out


# ---------- 评审 R4：分类通道接线（§7.6）与启动横幅 ----------


def test_r4_classifier_channel_absent_without_env(tmp_path, monkeypatch):
    from knowledge_ingest.cli import _tg_classifier_channel

    store = TelegramEventStore(tmp_path / "state.db")
    monkeypatch.delenv("KI_TELEGRAM_LLM_CMD", raising=False)
    assert _tg_classifier_channel(store) == (None, None)


def test_r4_classifier_channel_runs_configured_command(tmp_path, monkeypatch):
    import json
    import sys

    from knowledge_ingest.cli import _tg_classifier_channel

    store = TelegramEventStore(tmp_path / "state.db")
    script = tmp_path / "channel.py"
    script.write_text(
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'KEEP', 'reason_code': 'ok',\n"
        "                  'confidence': 1.0}))\n", encoding="utf-8")
    monkeypatch.setenv("KI_TELEGRAM_LLM_CMD",
                       f"{sys.executable} {script}")
    channel, budget = _tg_classifier_channel(store)
    assert channel is not None and budget is not None
    assert json.loads(channel("提示"))["decision"] == "KEEP"


def test_r4_channel_failure_becomes_review(tmp_path, monkeypatch):
    """通道进程失败 → llm_failure → REVIEW（绝不静默 SKIP 知识）。"""
    import sys

    from knowledge_ingest.cli import _tg_classifier_channel
    from knowledge_ingest.telegram.classify import classify_noise

    store = TelegramEventStore(tmp_path / "state.db")
    script = tmp_path / "boom.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setenv("KI_TELEGRAM_LLM_CMD", f"{sys.executable} {script}")
    channel, budget = _tg_classifier_channel(store)
    decision = classify_noise("私聊领取完整版资料", llm=channel, budget=budget,
                              source_id="tg_a")
    assert decision.decision == "REVIEW"
    assert decision.reason_code == "llm_failure"


def test_r4_watch_banner_reports_channel_state():
    from knowledge_ingest.cli import _tg_watch_banner

    off = _tg_watch_banner(handoff_enabled=False, channel_enabled=False)
    assert "classifier channel: none" in off
    assert "handoff scan: disabled" in off
    on = _tg_watch_banner(handoff_enabled=True, channel_enabled=True)
    assert "classifier channel: env(KI_TELEGRAM_LLM_CMD)" in on
    assert "handoff scan: ENABLED" in on


# ---------- 评审 R3：telegram download 人工入口 ----------


class _FakeDocClient:
    def __init__(self, payload=b"%PDF-1.4 fake"):
        self.payload = payload
        self.calls = 0

    async def download_document(self, chat_id, message_id, dest_dir):
        self.calls += 1
        path = Path(dest_dir) / "document.pdf"
        path.write_bytes(self.payload)
        return path


def _pdf_item_store(config, *, size_bytes=1024):
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0,
                     display_name="A")
    store.upsert_message("tg_a", 7, message_date="2026-09-18T10:00:00+08:00",
                         has_document=True, document_name="a.pdf",
                         document_size_bytes=size_bytes)
    store.create_source_item("tg_a:7", "tg_a", "pdf", [7],
                             processing_status="interest_include")
    store.set_item_interest("tg_a:7", "INCLUDE")
    return store


def test_r3_cli_download_command(tmp_path, capsys):
    from knowledge_ingest.cli import _cmd_telegram_download

    config = make_config(tmp_path)
    store = _pdf_item_store(config)
    client = _FakeDocClient()
    rc = _cmd_telegram_download(config, Namespace(item_id="tg_a:7"),
                                client=client)
    out = capsys.readouterr().out
    assert rc == 0 and "downloaded:" in out and client.calls == 1
    assert store.get_download("tg_a:7")["status"] == "complete"

    rc = _cmd_telegram_download(config, Namespace(item_id="tg_a:7"),
                                client=client)
    assert rc == 0 and "already downloaded" in capsys.readouterr().out
    assert client.calls == 1          # 幂等：不重复下载


def test_r3_cli_download_without_authorization_denied(tmp_path, capsys):
    from knowledge_ingest.cli import _cmd_telegram_download

    config = make_config(tmp_path)
    store = _pdf_item_store(config, size_bytes=80 * 1024 * 1024)
    client = _FakeDocClient(b"big")
    rc = _cmd_telegram_download(config, Namespace(item_id="tg_a:9"),
                                client=client)
    assert rc == 2                                        # item 不存在
    rc = _cmd_telegram_download(config, Namespace(item_id="tg_a:7"),
                                client=client)
    out = capsys.readouterr().out
    assert rc == 1 and "no download performed" in out
    assert client.calls == 0                              # 无授权不下载
    assert store.get_download("tg_a:7") is None


def test_r2_cli_resolve_applies_decision(tmp_path, capsys):
    """R2：resolve 当场生效（不再只写数据库）。"""
    from knowledge_ingest.cli import _cmd_telegram_review_resolve

    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0,
                     display_name="A")
    store.upsert_message("tg_a", 7, message_date="2026-09-18T10:00:00+08:00",
                         text="正文内容足够长的一段知识描述。")
    store.create_source_item("tg_a:7", "tg_a", "text", [7],
                             processing_status="noise_review")
    store.finalize_item("tg_a:7", status="noise_review")
    store.set_item_noise("tg_a:7", "REVIEW")
    review_id = store.create_review("tg_a:7", "noise_uncertain", kind="text")

    rc = _cmd_telegram_review_resolve(
        config, Namespace(review_id=review_id, decision="KEEP"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied:" in out
    item = store.get_source_item("tg_a:7")
    assert item["processing_status"] == "materialized"
    assert Path(item["materialized_path"]).is_file()


# ---------- 评审 R9：分类通道运维面（预算可见/可恢复 + 输出容错） ----------


def test_r9_budget_show_and_reset(tmp_path, capsys, monkeypatch):
    from knowledge_ingest.cli import _cmd_telegram_budget
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    monkeypatch.setenv("KI_TELEGRAM_LLM_MAX_CALLS", "5")
    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    guard = AIBudgetGuard(store, max_calls=5, breaker_empty=3)
    guard.acquire("tg_a", "noise_classifier", "r1")
    guard.outcome("tg_a", "noise_classifier", "r1", "empty")

    assert _cmd_telegram_budget(
        config, Namespace(telegram_budget_command="show")) == 0
    out = capsys.readouterr().out
    assert "tg_a:noise_classifier" in out and "calls=1/5" in out
    assert "breaker=closed" in out

    assert _cmd_telegram_budget(
        config, Namespace(telegram_budget_command="reset", source=None,
                          kind=None)) == 0
    assert "1 row(s) removed" in capsys.readouterr().out
    assert store.list_ai_budgets() == []


def test_r9_budget_show_marks_open_breaker(tmp_path, capsys, monkeypatch):
    from knowledge_ingest.cli import _cmd_telegram_budget
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    # 展示口径 = 当前 env 阈值（运行中的 watcher 用的就是这套）
    monkeypatch.setenv("KI_TELEGRAM_LLM_BREAKER_EMPTY", "2")
    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    guard = AIBudgetGuard(store, max_calls=99, breaker_empty=2)
    for i in range(2):
        rid = f"r{i}"
        guard.acquire("tg_a", "noise_classifier", rid)
        guard.outcome("tg_a", "noise_classifier", rid, "empty")

    _cmd_telegram_budget(config, Namespace(telegram_budget_command="show"))
    assert "breaker=OPEN:empty" in capsys.readouterr().out


def test_r9_budget_reset_scoped(tmp_path, capsys):
    from knowledge_ingest.cli import _cmd_telegram_budget
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    guard = AIBudgetGuard(store)
    guard.acquire("tg_a", "noise_classifier", "r1")
    guard.acquire("tg_b", "noise_classifier", "r2")

    assert _cmd_telegram_budget(
        config, Namespace(telegram_budget_command="reset", source="tg_a",
                          kind=None)) == 0
    remaining = {row["source_id"] for row in store.list_ai_budgets()}
    assert remaining == {"tg_b"}


def test_r9_status_reports_channel_budget(tmp_path, capsys):
    from knowledge_ingest.cli import _cmd_telegram_status
    from knowledge_ingest.telegram.event_store import AIBudgetGuard

    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    AIBudgetGuard(store).acquire("tg_a", "noise_classifier", "r1")
    assert _cmd_telegram_status(config, Namespace()) == 0
    out = capsys.readouterr().out
    assert "classifier channel budget: tg_a:noise_classifier=1/100" in out


def test_r9_extract_json_tolerates_fences_and_prose():
    from knowledge_ingest.cli import _extract_json_text

    payload = '{"decision": "KEEP", "reason_code": "ok", "confidence": 1.0}'
    assert _extract_json_text(payload) == payload
    assert _extract_json_text(f"```json\n{payload}\n```") == payload
    assert _extract_json_text(f"好的：\n{payload}\n以上。") == payload
    assert _extract_json_text("完全没有 JSON") is None
    assert _extract_json_text("") is None
    # 嵌套对象按最外层括号配对取（字符串内的花括号不误判）
    nested = '{"decision": "KEEP", "note": "a { b }", "x": {"y": 1}}'
    assert _extract_json_text(f"前缀 {nested} 后缀") == nested


def test_r9_channel_normalizes_fenced_model_output(tmp_path, monkeypatch):
    """模型输出带围栏/寒暄也必须解析成决定（否则 3 次就熔断）。"""
    import sys

    from knowledge_ingest.cli import _tg_classifier_channel
    from knowledge_ingest.telegram.classify import classify_noise

    store = TelegramEventStore(tmp_path / "state.db")
    script = tmp_path / "channel.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        "print('```json')\n"
        "print('{\"decision\": \"KEEP\", \"reason_code\": \"ok\","
        " \"confidence\": 0.9}')\n"
        "print('```')\n", encoding="utf-8")
    monkeypatch.setenv("KI_TELEGRAM_LLM_CMD", f"{sys.executable} {script}")
    channel, budget = _tg_classifier_channel(store)
    decision = classify_noise("私聊领取完整版资料，加微信", llm=channel,
                              budget=budget, source_id="tg_a")
    assert decision.decision == "KEEP"
    row = store.get_ai_budget("tg_a", "noise_classifier")
    assert row["calls"] == 1 and row["consecutive_empty"] == 0


def test_r9_budget_limits_from_env(monkeypatch):
    from knowledge_ingest.cli import _tg_budget_limits

    monkeypatch.delenv("KI_TELEGRAM_LLM_MAX_CALLS", raising=False)
    assert _tg_budget_limits() == (100, 3, 2)
    monkeypatch.setenv("KI_TELEGRAM_LLM_MAX_CALLS", "20")
    monkeypatch.setenv("KI_TELEGRAM_LLM_BREAKER_RATE_LIMIT", "5")
    assert _tg_budget_limits() == (20, 3, 5)
    monkeypatch.setenv("KI_TELEGRAM_LLM_MAX_CALLS", "abc")
    with pytest.raises(ValueError):
        _tg_budget_limits()


def test_r9_classify_dryrun_is_read_only(tmp_path, capsys):
    """试跑：只读——不写预算账本、不改 Item 状态。"""
    import json as _json

    from knowledge_ingest.cli import _cmd_telegram_classify_dryrun

    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source(source_id="tg_a", chat_id=-1, start_at=T0,
                     display_name="A")
    store.upsert_message("tg_a", 7,
                         message_date="2026-09-18T10:00:00+08:00",
                         text="疑似广告的长正文，含私聊与领取字样。" * 20)
    store.create_source_item("tg_a:7", "tg_a", "text", [7],
                             processing_status="noise_review")
    store.finalize_item("tg_a:7", status="noise_review")
    store.set_item_noise("tg_a:7", "REVIEW")
    store.create_review("tg_a:7", "noise_uncertain", kind="text")

    def fake_channel(_prompt):
        return _json.dumps({"decision": "SKIP", "reason_code": "ad",
                            "confidence": 0.9})

    assert _cmd_telegram_classify_dryrun(config, Namespace(limit=None),
                                         channel=fake_channel) == 0
    out = capsys.readouterr().out
    assert "SKIP" in out and "decisions={'SKIP': 1}" in out
    assert "nothing was written" in out
    assert store.list_ai_budgets() == []                      # 不记账
    assert store.get_source_item("tg_a:7")["processing_status"] == \
        "noise_review"                                        # 不改状态


def test_r9_classify_dryrun_requires_channel(tmp_path, capsys, monkeypatch):
    from knowledge_ingest.cli import _cmd_telegram_classify_dryrun

    monkeypatch.delenv("KI_TELEGRAM_LLM_CMD", raising=False)
    config = make_config(tmp_path)
    TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    rc = _cmd_telegram_classify_dryrun(config, Namespace(limit=None))
    assert rc == 2
    assert "no classifier channel configured" in capsys.readouterr().err
