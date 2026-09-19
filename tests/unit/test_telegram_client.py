"""TG3: TelegramClientPort / watcher / 无 extra 契约 / adapter 映射.

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §7/§19，
FINAL 方案 §5（含 §5.2 无 extra CI 契约、§5.8 TDD 清单）。
业务层一律走 FakeTelegramClient；adapter 专属测试用 importorskip。
"""

import asyncio
import subprocess
import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_ingest.cli import (
    _cmd_telegram_auth,
    _cmd_telegram_sources_add,
)
from knowledge_ingest.config import AppConfig
from knowledge_ingest.telegram.client_port import (
    TelegramDocumentRef,
    TelegramEvent,
    TelegramEventKind,
)
from knowledge_ingest.telegram.event_store import (
    MessageBeforeStartError,
    TelegramEventStore,
)
from knowledge_ingest.telegram.watcher import TelegramWatcher

T0 = "2026-09-18T10:00:00+08:00"
T1 = "2026-09-18T10:01:00+08:00"


def dt(year, month, day, hour, minute, second=0):
    return datetime(2026, month, day, hour, minute, second,
                    tzinfo=UTC)


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


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


class FakeTelegramClient:
    """§5.8：业务层测试专用 Fake（实现 TelegramClientPort 语义）。"""

    def __init__(self, updates=(), after_events=(), latest=None,
                 dialog=None):
        self._updates = list(updates)
        self._after_events = list(after_events)
        self._latest = latest
        self._dialog = dialog
        self.fetched_after: list[tuple[int, int]] = []

    async def watch_updates(self):
        for event in self._updates:
            yield event
        await asyncio.Event().wait()  # 无更多更新：挂起等待

    async def fetch_messages_after(self, chat_id: int,
                                   after_message_id: int):
        self.fetched_after.append((chat_id, after_message_id))
        return list(self._after_events)

    async def latest_message(self, chat_id: int):
        return self._latest

    async def list_dialogs(self):
        return []

    async def resolve_chat(self, ref: str):
        if self._dialog is None:
            raise NotImplementedError
        return self._dialog

    async def download_document(self, chat_id: int, message_id: int,
                                dest_dir):
        raise NotImplementedError

    async def get_message(self, chat_id: int, message_id: int):
        return None


def make_event(kind=TelegramEventKind.NEW, chat_id=-1001234567890,
               message_id=7, **kwargs):
    defaults = {"message_date": dt(2026, 9, 18, 2, 1),
                "sender_id": 99, "text": "知识正文"}
    defaults.update(kwargs)
    return TelegramEvent(kind=kind, chat_id=chat_id,
                         message_id=message_id, **defaults)


def make_ready_store(tmp_path: Path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     display_name="AI探索指南", start_at=T0)
    return store


# ---------- 事件类型 ----------

def test_telegram_event_kinds():
    assert TelegramEventKind.NEW.value == "NEW"
    assert TelegramEventKind.EDIT.value == "EDIT"
    assert TelegramEventKind.DELETE.value == "DELETE"


def test_delete_event_identity_only():
    """§5.3：DELETE 至少携带 (chat_id, message_id) 身份字段。"""
    event = TelegramEvent(kind=TelegramEventKind.DELETE,
                          chat_id=-1001234567890, message_id=9)
    assert event.message_date is None
    assert event.text is None


# ---------- watcher：NEW / EDIT / DELETE ----------

def test_watcher_new_stored(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    assert watcher.handle_event(make_event()) == "stored"
    assert store.count_messages("tg_a") == 1
    assert store.get_message("tg_a", 7)["text"] == "知识正文"


def test_watcher_duplicate_new_single_row(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    assert watcher.handle_event(make_event()) == "stored"
    assert watcher.handle_event(make_event(text="v2")) == "stored"
    assert store.count_messages("tg_a") == 1
    assert store.get_message("tg_a", 7)["text"] == "v2"


def test_watcher_edit_updates_same_row(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    watcher.handle_event(make_event())
    edited = make_event(kind=TelegramEventKind.EDIT, text="改后",
                        edited_at=dt(2026, 9, 18, 3, 0))
    assert watcher.handle_event(edited) == "updated"
    row = store.get_message("tg_a", 7)
    assert row["text"] == "改后"
    assert row["edited_at"] is not None
    assert store.count_messages("tg_a") == 1


def test_watcher_edit_unseen_message_creates(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    edited = make_event(kind=TelegramEventKind.EDIT, message_id=11,
                        text="补录")
    assert watcher.handle_event(edited) == "updated"
    assert store.get_message("tg_a", 11)["text"] == "补录"


def test_watcher_delete_marks_only(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    watcher.handle_event(make_event())
    deleted = make_event(kind=TelegramEventKind.DELETE, message_id=7)
    assert watcher.handle_event(deleted) == "deleted"
    row = store.get_message("tg_a", 7)
    assert row is not None and row["deleted_at"] is not None


def test_watcher_delete_unknown_message_ignored(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    deleted = make_event(kind=TelegramEventKind.DELETE, message_id=424242)
    assert watcher.handle_event(deleted) == "ignored"


def test_watcher_unknown_chat_ignored(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    event = make_event(chat_id=-999999)
    assert watcher.handle_event(event) == "ignored"
    assert store.count_messages("tg_a") == 0


def test_watcher_disabled_source_skipped(tmp_path):
    store = make_ready_store(tmp_path)
    store.set_source_enabled("tg_a", False)
    watcher = TelegramWatcher(store)
    assert watcher.handle_event(make_event()) == "skipped"
    assert store.count_messages("tg_a") == 0


def test_watcher_start_at_boundary_skipped_old(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    old = make_event(message_date=dt(2026, 9, 18, 1, 0))  # 早于 start_at
    with pytest.raises(MessageBeforeStartError):
        store.upsert_message("tg_a", 1, message_date=old.message_date.isoformat())
    assert watcher.handle_event(old) == "skipped_old"
    assert store.count_messages("tg_a") == 0


def test_watcher_new_with_document_and_links(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    document = TelegramDocumentRef(document_id=555, file_name="课程.pdf",
                                   mime_type="application/pdf",
                                   size_bytes=1024)
    event = make_event(text=None, document=document,
                       cloud_links=("https://pan.quark.cn/s/abc",))
    watcher.handle_event(event)
    row = store.get_message("tg_a", 7)
    assert row["has_document"] == 1
    assert row["document_name"] == "课程.pdf"
    assert "pan.quark.cn" in row["cloud_links_json"]


# ---------- reconcile（§5.7 B 路径） ----------

def test_reconcile_stores_and_advances_cursor(tmp_path):
    store = make_ready_store(tmp_path)
    events = [make_event(message_id=11), make_event(message_id=12)]
    client = FakeTelegramClient(after_events=events)
    watcher = TelegramWatcher(store, client=client)
    count = asyncio.run(watcher.reconcile("tg_a"))
    assert count == 2
    assert store.count_messages("tg_a") == 2
    assert client.fetched_after == [(-1001234567890, 0)]
    source = store.get_source("tg_a")
    assert source["last_seen_message_id"] == 12
    assert source["last_reconciled_at"] is not None


def test_reconcile_continues_from_cursor(tmp_path):
    store = make_ready_store(tmp_path)
    store.upsert_message("tg_a", 11, message_date=T1)
    store.touch_source_cursor("tg_a", last_seen_message_id=11)
    client = FakeTelegramClient(after_events=[make_event(message_id=13)])
    watcher = TelegramWatcher(store, client=client)
    asyncio.run(watcher.reconcile("tg_a"))
    assert client.fetched_after == [(-1001234567890, 11)]
    assert store.get_source("tg_a")["last_seen_message_id"] == 13


def test_reconcile_all_only_enabled(tmp_path):
    store = make_ready_store(tmp_path)
    store.add_source(source_id="tg_off", chat_id=-2002, start_at=T0,
                     enabled=False)
    client = FakeTelegramClient(after_events=[make_event(message_id=21)])
    watcher = TelegramWatcher(store, client=client)
    asyncio.run(watcher.reconcile_all())
    assert store.get_source("tg_a")["last_seen_message_id"] == 21
    assert store.get_source("tg_off")["last_seen_message_id"] == 0


def test_run_drains_updates_then_reconciles(tmp_path):
    store = make_ready_store(tmp_path)
    updates = [make_event(message_id=31), make_event(message_id=32)]
    client = FakeTelegramClient(updates=updates,
                                after_events=[make_event(message_id=33)])
    watcher = TelegramWatcher(store, client=client)
    asyncio.run(watcher.run(max_ticks=1, idle_seconds=0.01))
    assert store.count_messages("tg_a") == 3
    assert store.get_source("tg_a")["last_seen_message_id"] == 33


def test_watcher_live_advances_cursor(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    watcher.handle_event(make_event(message_id=41))
    source = store.get_source("tg_a")
    assert source["last_seen_message_id"] == 41


def test_watcher_cursor_never_regresses(tmp_path):
    store = make_ready_store(tmp_path)
    watcher = TelegramWatcher(store)
    watcher.handle_event(make_event(message_id=50))
    watcher.handle_event(make_event(message_id=40))  # 迟到的旧事件
    assert store.get_source("tg_a")["last_seen_message_id"] == 50


def test_run_reconciles_even_when_updates_keep_flowing(tmp_path):
    """高频群 updates 不断流时，reconcile 仍按墙钟周期兜底触发。"""

    class FlowingClient(FakeTelegramClient):
        async def watch_updates(self):
            for i in range(100, 112):
                yield make_event(message_id=i)
                await asyncio.sleep(0.03)
            await asyncio.Event().wait()

    store = make_ready_store(tmp_path)
    client = FlowingClient()
    watcher = TelegramWatcher(store, client=client)
    asyncio.run(watcher.run(max_ticks=1, idle_seconds=0.2,
                            reconcile_interval_seconds=0.0))
    assert store.get_source("tg_a")["last_reconciled_at"] is not None
    assert client.fetched_after  # reconcile 确实发起了增量拉取


# ---------- 无 extra CI 契约（§5.2） ----------

def test_no_extra_core_import_contract():
    """未安装 telethon 时：核心 import 成功、adapter 可导入可构造、
    调用 adapter 方法得到领域错误而非裸 ImportError。"""
    code = (
        "import sys\n"
        "import importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'telethon':\n"
        "            raise ModuleNotFoundError('blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import knowledge_ingest\n"
        "import knowledge_ingest.cli\n"
        "import knowledge_ingest.telegram.telethon_adapter as ta\n"
        "adapter = ta.TelethonAdapter(session_dir='/tmp/x', credentials={})\n"
        "try:\n"
        "    adapter.list_dialogs_sync()\n"
        "except ta.TelegramDependencyMissingError:\n"
        "    print('CONTRACT_OK')\n"
        "else:\n"
        "    raise SystemExit('expected TelegramDependencyMissingError')\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, timeout=60,
                            check=False)
    assert result.returncode == 0, result.stderr
    assert "CONTRACT_OK" in result.stdout


def test_adapter_connects_before_requests(tmp_path):
    """§7：所有请求前必须自动连接（Telethon 不读环境变量也不自动连）。"""
    from knowledge_ingest.telegram.telethon_adapter import TelethonAdapter

    connect_calls = []

    class DuckClient:
        def __init__(self):
            self._connected = False

        def is_connected(self):
            return self._connected

        async def connect(self):
            connect_calls.append(1)
            self._connected = True
            return self

        async def iter_dialogs(self):
            if not self._connected:
                raise ConnectionError("Cannot send requests while "
                                      "disconnected")
            yield SimpleNamespace(id=-100, name="g", username="g")

    adapter = TelethonAdapter(session_dir=tmp_path / "s", credentials={})
    adapter._client = DuckClient()
    dialogs = asyncio.run(adapter.list_dialogs())
    assert [d.chat_id for d in dialogs] == [-100]
    assert connect_calls == [1]  # 请求前自动补了连接


# ---------- CLI：sources add（H3 前的 fake 验证） ----------

def test_cli_sources_add_with_fake_client(tmp_path, capsys):
    config = make_config(tmp_path)
    dialog = SimpleNamespace(chat_id=-1001234567890, title="AI探索指南",
                             username="ai_explore")
    latest = make_event(message_id=555)
    client = FakeTelegramClient(latest=latest, dialog=dialog)
    args = Namespace(ref="@ai_explore", source_id="tg_a",
                     display_name=None)
    rc = _cmd_telegram_sources_add(config, args, client=client)
    assert rc == 0
    out = capsys.readouterr().out
    assert "tg_a" in out
    fresh = TelegramEventStore(config.pipeline_root / "telegram"
                               / "state.db")
    source = fresh.get_source("tg_a")
    assert source["chat_id"] == -1001234567890
    assert source["last_seen_message_id"] == 555
    assert source["start_at"] is not None
    assert source["enabled"] == 1


def test_cli_auth_missing_credentials(tmp_path, capsys):
    config = make_config(tmp_path)
    args = Namespace()
    rc = _cmd_telegram_auth(config, args, repo_root=tmp_path,
                            session_dir=tmp_path / "home" / "telegram",
                            signer=None)
    assert rc == 2
    assert "credentials" in capsys.readouterr().err.lower()
