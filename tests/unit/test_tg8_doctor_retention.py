"""TG8：telegram doctor（只读体检）与 retention（SKIP 载荷清理）。"""

import pathlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knowledge_ingest.config import AppConfig
from knowledge_ingest.telegram import doctor
from knowledge_ingest.telegram.event_store import TelegramEventStore


def make_config(tmp_path: Path):
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "m"),
        "docchunk_project": str(tmp_path / "d"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "dc"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "b", "quark": "q", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })


@pytest.fixture()
def healthy_store(tmp_path, monkeypatch):
    """健康基线：session/orico/home 全部指向 tmp。"""
    config = make_config(tmp_path)
    monkeypatch.setattr(doctor, "session_dir",
                        lambda config: tmp_path / "session")
    sess = tmp_path / "session"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "session.session").write_bytes(b"x")
    import os as _os

    _os.chmod(sess / "session.session", 0o600)
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        doctor, "ORICO_ROOT", pathlib.Path("/Volumes/ORICO"))
    monkeypatch.setattr(
        doctor, "orico_online",
        lambda: real_is_dir(pathlib.Path("/Volumes/ORICO")))
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source("tg_a", chat_id=-1, start_at=None, display_name="A")
    # 最新 reconcile 时间
    store.touch_source_cursor("tg_a", last_reconciled_at=_iso_now())
    return config, store


def _iso_now():
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def test_doctor_healthy_baseline_passes(tmp_path, healthy_store, capsys):
    config, store = healthy_store
    rc = doctor.run(config, store=store)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 FAIL" in out
    assert "user_version" in out and "ORICO 在线" in out


def test_doctor_flags_missing_materialized_file(tmp_path, healthy_store,
                                                capsys):
    config, store = healthy_store
    store.create_source_item("tg_a:1", "tg_a", "text", [1],
                             processing_status="materialized")
    store.set_item_materialized("tg_a:1", str(tmp_path / "gone.md"))
    rc = doctor.run(config, store=store)
    out = capsys.readouterr().out
    assert rc == 1 and "materialized 文件存在" in out \
        and "[FAIL" in out


def test_doctor_flags_stale_reconcile(tmp_path, healthy_store, capsys):
    config, store = healthy_store
    stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat(
        timespec="seconds")
    store.touch_source_cursor("tg_a", last_reconciled_at=stale)
    rc = doctor.run(config, store=store)
    out = capsys.readouterr().out
    assert rc == 1 or "WARN" in out
    assert "reconcile 时效" in out


def test_doctor_flags_broken_download(tmp_path, healthy_store, capsys):
    config, store = healthy_store
    store.create_source_item("tg_a:9", "tg_a", "pdf", [9],
                             processing_status="interest_include")
    store.upsert_download("tg_a:9", 9, status="complete", attempts=1,
                          expected_size_bytes=10,
                          local_path=str(tmp_path / "missing.bin"),
                          sha256="ab" * 32)
    rc = doctor.run(config, store=store)
    out = capsys.readouterr().out
    assert rc == 1 and "下载文件存在且 SHA 匹配" in out \
        and "[FAIL" in out


# ---------- retention ----------


def _seed_skip_payload(tmp_path: Path):
    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source("tg_a", chat_id=-1, start_at=None, display_name="A")
    store.upsert_message("tg_a", 1,
                         message_date="2026-09-01T00:00:00+00:00",
                         has_document=True, document_name="junk.bin",
                         document_size_bytes=100)
    store.create_source_item("tg_a:1", "tg_a", "pdf", [1],
                             processing_status="skipped_noise")
    store.finalize_item("tg_a:1", status="skipped_noise")
    store.set_item_noise("tg_a:1", "SKIP")
    payload = config.pipeline_root / "telegram" / "attachments" / "tg_a:1"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "junk.bin").write_bytes(b"x" * 10)
    store.upsert_download("tg_a:1", 1, status="complete", attempts=1,
                          expected_size_bytes=10,
                          local_path=str(payload / "junk.bin"),
                          sha256="a" * 64)
    # KEEP 对照：materialized 文本（正文文件）不清理
    keep_dir = config.pipeline_root / "telegram" / "materialized" / "tg_a:2"
    keep_dir.mkdir(parents=True, exist_ok=True)
    (keep_dir / "message.md").write_text("保留的正文", encoding="utf-8")
    store.upsert_message("tg_a", 2,
                         message_date="2026-09-01T00:00:00+00:00",
                         text="保留的正文")
    store.create_source_item("tg_a:2", "tg_a", "text", [2],
                             processing_status="materialized")
    store.set_item_materialized("tg_a:2", str(keep_dir / "message.md"))
    return config, store, payload / "junk.bin", keep_dir / "message.md"


def test_retention_dry_run_counts_without_deleting(tmp_path, capsys):
    from knowledge_ingest.telegram.retention import run as retention_run

    config, store, payload, keep = _seed_skip_payload(tmp_path)
    # 把 SKIP 决策时间拨到 31 天前
    with store._conn:
        store._conn.execute(
            "UPDATE source_items SET finalized_at = ? WHERE item_id='tg_a:1'",
            ((datetime.now(UTC) - timedelta(days=31))
             .isoformat(timespec="seconds"),))
    rc = retention_run(config, days=30, execute=False)
    out = capsys.readouterr().out
    assert rc == 0 and "dry-run" in out
    assert payload.exists()                       # 未删除
    assert keep.exists()                          # KEEP 正文不动


def test_retention_execute_deletes_skip_payload_keeps_provenance(
        tmp_path, capsys):
    from knowledge_ingest.telegram.retention import run as retention_run

    config, store, payload, keep = _seed_skip_payload(tmp_path)
    with store._conn:
        store._conn.execute(
            "UPDATE source_items SET finalized_at = ? WHERE item_id='tg_a:1'",
            ((datetime.now(UTC) - timedelta(days=31))
             .isoformat(timespec="seconds"),))
    rc = retention_run(config, days=30, execute=True)
    out = capsys.readouterr().out
    assert rc == 0 and "deleted" in out
    assert not payload.exists()                   # SKIP 载荷被清
    assert keep.exists()                          # KEEP 正文不动
    row = store.get_download("tg_a:1")
    assert row["status"] == "purged"              # 下载行留证（审计）
    assert store.get_source_item("tg_a:1") is not None  # Item 行留证
