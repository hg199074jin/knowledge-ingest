"""TG7 通知运行时：幂等、降级、日报。"""

import json
import urllib.request
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.telegram.event_store import TelegramEventStore
from knowledge_ingest.telegram.notification import (
    WxPusherAdapter,
    build_digest,
    load_wxpusher_credentials,
    notify,
)


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "state.db")


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


class FakePort:
    def __init__(self):
        self.sent = []

    def send(self, uid, summary, content):
        self.sent.append((uid, summary, content))
        return True


def test_notify_is_idempotent_by_event_id(tmp_path):
    store = make_store(tmp_path)
    port = FakePort()
    assert notify(store, port, event_id="e1", channel="wxpusher",
                  summary="s", content="c", uid="u") is True
    assert notify(store, port, event_id="e1", channel="wxpusher",
                  summary="s", content="c", uid="u") is False  # 重放不重发
    assert len(port.sent) == 1
    assert store.has_notification("e1")


def test_notify_failure_keeps_event_undelivered(tmp_path):
    store = make_store(tmp_path)

    class FailPort:
        def send(self, uid, summary, content):
            return False

    assert notify(store, FailPort(), event_id="e2", channel="wxpusher",
                  summary="s", content="c", uid="u") is False
    row = store._conn.execute(
        "SELECT delivered_at FROM notifications WHERE event_id='e2'") \
        .fetchone()
    assert row["delivered_at"] is None   # 失败不标已投递（可重试语义由调用方定）


def test_wxpusher_credentials_missing_or_partial(tmp_path):
    assert load_wxpusher_credentials(tmp_path / "none.env") == {}
    p = tmp_path / "wxpusher.env"
    p.write_text("WXPUSHER_APP_TOKEN=tok\n", encoding="utf-8")
    assert load_wxpusher_credentials(p) == {}            # 缺 UID 不算配置
    p.write_text("# 注释\nWXPUSHER_APP_TOKEN=tok\n"
                 "WXPUSHER_UID=UID_1\n", encoding="utf-8")
    creds = load_wxpusher_credentials(p)
    assert creds == {"WXPUSHER_APP_TOKEN": "tok", "WXPUSHER_UID": "UID_1"}


def test_wxpusher_adapter_posts_and_parses(tmp_path, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))

        class Resp:
            def read(self):
                return b'{"code": 1000, "msg": "ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = WxPusherAdapter("tok", "UID_1", api_url="http://x/api")
    assert adapter.send("UID_1", "摘要", "正文") is True
    assert captured["url"] == "http://x/api"
    assert captured["payload"]["appToken"] == "tok"
    assert captured["payload"]["uids"] == ["UID_1"]
    assert captured["payload"]["summary"] == "摘要"


def test_digest_lists_reviews_and_budget(tmp_path):
    store = make_store(tmp_path)
    store.add_source("tg_a", chat_id=-1, start_at=None, display_name="A")
    store.create_source_item("tg_a:1", "tg_a", "pdf", [1],
                             processing_status="interest_review")
    store.create_review("tg_a:1", "interest_uncertain", kind="pdf",
                        size_bytes=12345)
    digest = build_digest(store)
    assert "待人工审阅（REVIEW）：1 条" in digest
    assert "tg_a:1" in digest
    assert "通道账本" in digest


def test_digest_agent_plist_schedule(tmp_path, monkeypatch):
    import plistlib

    from knowledge_ingest.telegram import digest_agent

    monkeypatch.setattr(digest_agent, "_home", lambda: tmp_path)
    config = AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "m"),
        "docchunk_project": str(tmp_path / "d"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "dc"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "b", "quark": "q", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })
    data = plistlib.loads(digest_agent.generate_plist_bytes(
        config, "/repo/.venv/bin/knowledge-ingest"))
    assert data["Label"] == f"com.{tmp_path.name}.ki-telegram-digest"
    assert data["StartCalendarInterval"] == [
        {"Hour": 8, "Minute": 0}, {"Hour": 12, "Minute": 0},
        {"Hour": 20, "Minute": 0}]
    assert data["ProgramArguments"] == [
        "/repo/.venv/bin/knowledge-ingest", "telegram", "digest", "--send"]


def test_digest_cli_dry_run_then_send(tmp_path, capsys, monkeypatch):
    """digest：默认干跑只打印；--send 走端口并推进游标。"""
    from argparse import Namespace

    from knowledge_ingest.cli import _cmd_telegram_digest

    config = make_config(tmp_path)
    store = TelegramEventStore(config.pipeline_root / "telegram" / "state.db")
    store.add_source("tg_a", chat_id=-1, start_at=None, display_name="A")
    store.create_source_item("tg_a:1", "tg_a", "pdf", [1],
                             processing_status="interest_review")
    store.create_review("tg_a:1", "interest_uncertain", kind="pdf")

    monkeypatch.setattr(
        "knowledge_ingest.cli._tg_ports",
        lambda config: (FakePort(), "UID_1"))
    rc = _cmd_telegram_digest(config, Namespace(send=True))
    out = capsys.readouterr().out
    assert rc == 0 and "digest sent" in out
    assert store.get_digest_state("digest:last_sent_at") is not None

    # 游标推进后，同样的内容不再重复（新建 Item 为 0）
    rc = _cmd_telegram_digest(config, Namespace(send=False))
    out = capsys.readouterr().out
    assert "新建 Item（自" in out
