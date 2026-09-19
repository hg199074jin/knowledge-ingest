"""TG3: TelethonAdapter 专属测试（§5.4）。

本文件整体 importorskip：未安装 telegram extra（如 CI 的裸
uv sync）时整文件跳过；watcher / 契约测试在
tests/unit/test_telegram_client.py，必须始终运行。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("telethon")


def make_adapter(tmp_path: Path):
    from knowledge_ingest.telegram.telethon_adapter import TelethonAdapter

    return TelethonAdapter(session_dir=tmp_path / "s", credentials={})


def test_lazy_import_available():
    from knowledge_ingest.telegram.telethon_adapter import (
        TelethonAdapter,
        _require_telethon,
    )

    assert _require_telethon() is not None
    adapter = TelethonAdapter(session_dir=Path("/tmp/x"), credentials={})
    assert adapter is not None
    assert adapter.session_path().name == "session.session"


# ---------- 代理配置（国内网络经本机代理连 Telegram DC） ----------


def test_proxy_from_credentials_absent():
    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    assert _proxy_from_credentials({}) is None
    assert _proxy_from_credentials({"API_ID": "1"}) is None


def test_proxy_from_credentials_socks5():
    from python_socks import ProxyType

    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    proxy = _proxy_from_credentials({
        "PROXY_TYPE": "socks5", "PROXY_HOST": "127.0.0.1",
        "PROXY_PORT": "7897"})
    assert proxy == (ProxyType.SOCKS5, "127.0.0.1", 7897)


def test_proxy_from_credentials_http():
    from python_socks import ProxyType

    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    proxy = _proxy_from_credentials({
        "PROXY_TYPE": "HTTP", "PROXY_HOST": "127.0.0.1",
        "PROXY_PORT": "7897"})
    assert proxy == (ProxyType.HTTP, "127.0.0.1", 7897)


def test_proxy_from_credentials_bad_type():
    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    with pytest.raises(ValueError, match="PROXY_TYPE"):
        _proxy_from_credentials({
            "PROXY_TYPE": "mtproto", "PROXY_HOST": "h",
            "PROXY_PORT": "1"})


def test_proxy_from_credentials_bad_port():
    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    with pytest.raises(ValueError, match="PROXY_PORT"):
        _proxy_from_credentials({
            "PROXY_TYPE": "socks5", "PROXY_HOST": "h",
            "PROXY_PORT": "abc"})


def test_proxy_from_credentials_missing_host():
    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    with pytest.raises(ValueError, match="PROXY_HOST"):
        _proxy_from_credentials({"PROXY_TYPE": "socks5"})


def test_proxy_port_range_validated():
    from knowledge_ingest.telegram.telethon_adapter import (
        _proxy_from_credentials,
    )

    for bad in ("0", "70000", "-1"):
        with pytest.raises(ValueError, match="PROXY_PORT"):
            _proxy_from_credentials({
                "PROXY_TYPE": "socks5", "PROXY_HOST": "127.0.0.1",
                "PROXY_PORT": bad})


def test_to_event_maps_edit_date_and_cloud_links():
    """真实路径：edit_date → edited_at；正文中的网盘链接 → cloud_links。"""
    from knowledge_ingest.telegram.client_port import TelegramEventKind
    from knowledge_ingest.telegram.telethon_adapter import _to_event

    message = SimpleNamespace(
        id=9, chat_id=-1001234567890,
        date=datetime(2026, 9, 18, 2, 0, tzinfo=UTC),
        edit_date=datetime(2026, 9, 18, 2, 5, tzinfo=UTC),
        sender_id=1,
        message="资源：https://pan.quark.cn/s/abc 提取码 x1y2",
        document=None)
    event = _to_event(TelegramEventKind.EDIT, message)
    assert event.edited_at == datetime(2026, 9, 18, 2, 5, tzinfo=UTC)
    assert any("pan.quark.cn" in link for link in event.cloud_links)


def test_fetch_messages_after_marks_edited(tmp_path):
    """§5.7：reconcile 发现的已编辑消息走 EDIT 事实更新。"""
    from knowledge_ingest.telegram.client_port import TelegramEventKind

    class DuckFetchClient:
        def __init__(self):
            self._connected = True

        def is_connected(self):
            return self._connected

        async def connect(self):
            self._connected = True

        async def get_messages(self, chat_id, **kwargs):
            return [
                SimpleNamespace(
                    id=5, chat_id=chat_id,
                    date=datetime(2026, 9, 18, 2, 0, tzinfo=UTC),
                    edit_date=datetime(2026, 9, 18, 2, 5, tzinfo=UTC),
                    sender_id=1, message="v2", document=None),
                SimpleNamespace(
                    id=6, chat_id=chat_id,
                    date=datetime(2026, 9, 18, 2, 1, tzinfo=UTC),
                    edit_date=None,
                    sender_id=1, message="new", document=None),
            ]

    adapter = make_adapter(tmp_path)
    adapter._client = DuckFetchClient()
    events = asyncio.run(adapter.fetch_messages_after(-1001234567890, 4))
    kinds = {event.message_id: event.kind for event in events}
    assert kinds[5] is TelegramEventKind.EDIT
    assert kinds[6] is TelegramEventKind.NEW


def test_watch_updates_handlers_removed_on_close(tmp_path):
    """C1 回归：闭包身份必须与注册时相同，否则 telethon 移除是空操作。"""
    import contextlib

    from knowledge_ingest.telegram.telethon_adapter import TelethonAdapter

    class DuckTelethonClient:
        def __init__(self):
            self.handlers = []
            self._connected = True

        def is_connected(self):
            return self._connected

        async def connect(self):
            self._connected = True

        def add_event_handler(self, callback, event):
            self.handlers.append(callback)

        def remove_event_handler(self, callback, event):
            if callback in self.handlers:
                self.handlers.remove(callback)

    duck = DuckTelethonClient()
    adapter = TelethonAdapter(session_dir=tmp_path / "s", credentials={})
    adapter._client = duck

    async def drive():
        generator = adapter.watch_updates()
        task = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0.02)  # 注册发生在此
        registered = len(duck.handlers)
        # 与生产路径一致：取消挂起的 anext，取消穿透生成器触发 finally
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return registered

    assert asyncio.run(drive()) == 3
    assert duck.handlers == []


def test_flood_wait_mapping(tmp_path):
    from telethon.errors import FloodWaitError

    from knowledge_ingest.telegram.client_port import TelegramFloodWaitError

    async def boom():
        raise FloodWaitError(request=None)

    adapter = make_adapter(tmp_path)
    with pytest.raises(TelegramFloodWaitError) as exc_info:
        asyncio.run(adapter._guard(boom()))
    assert exc_info.value.seconds >= 0


def test_rpc_error_mapping(tmp_path):
    from telethon.errors import RPCError

    from knowledge_ingest.telegram.client_port import TelegramRPCError

    async def boom():
        raise RPCError(request=None, message="boom", code=400)

    adapter = make_adapter(tmp_path)
    with pytest.raises(TelegramRPCError):
        asyncio.run(adapter._guard(boom()))


def test_to_event_mapping():
    from knowledge_ingest.telegram.client_port import TelegramEventKind
    from knowledge_ingest.telegram.telethon_adapter import _to_event

    message = SimpleNamespace(
        id=7, chat_id=-1001234567890,
        date=datetime(2026, 9, 18, 2, 1, tzinfo=UTC),
        sender_id=99, message="正文", document=None,
    )
    event = _to_event(TelegramEventKind.NEW, message)
    assert event.message_id == 7
    assert event.chat_id == -1001234567890
    assert event.text == "正文"
    assert event.document is None


def test_to_event_with_document():
    from knowledge_ingest.telegram.client_port import TelegramEventKind
    from knowledge_ingest.telegram.telethon_adapter import _to_event

    document = SimpleNamespace(id=555, name="课程.pdf",
                               mime_type="application/pdf", size=1024)
    message = SimpleNamespace(
        id=8, chat_id=-1001234567890,
        date=datetime(2026, 9, 18, 2, 2, tzinfo=UTC),
        sender_id=99, message="", document=document,
    )
    event = _to_event(TelegramEventKind.NEW, message)
    assert event.document is not None
    assert event.document.file_name == "课程.pdf"
    assert event.document.size_bytes == 1024


def test_r8_live_handlers_are_awaitable(tmp_path):
    """R8（生产日志实测）：telethon 1.45 的派发是 `await callback(event)`；
    同步 handler 返回 None → 每条 update 在 telethon 内部抛
    TypeError（消息仍入队，但每条日志一个 traceback，异常还发生在
    别人的派发器里）。handler 必须是协程函数。"""
    import contextlib
    import inspect
    from types import SimpleNamespace

    from knowledge_ingest.telegram.telethon_adapter import TelethonAdapter

    class DuckTelethonClient:
        def __init__(self):
            self.handlers = []

        def is_connected(self):
            return True

        async def connect(self):
            return None

        def add_event_handler(self, callback, event):
            self.handlers.append(callback)

        def remove_event_handler(self, callback, event):
            if callback in self.handlers:
                self.handlers.remove(callback)

    duck = DuckTelethonClient()
    adapter = TelethonAdapter(session_dir=tmp_path / "s", credentials={})
    adapter._client = duck
    message = SimpleNamespace(
        id=11, chat_id=-1001234567890,
        date=datetime(2026, 9, 18, 2, 0, tzinfo=UTC), edit_date=None,
        sender_id=1, message="hi", document=None)
    update_event = SimpleNamespace(message=message,
                                   chat_id=-1001234567890)

    async def drive():
        generator = adapter.watch_updates()
        task = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0.02)
        handlers = list(duck.handlers)
        assert len(handlers) == 3
        for handler in handlers:          # telethon 的派发路径
            await handler(update_event)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return handlers

    handlers = asyncio.run(drive())
    assert all(inspect.iscoroutinefunction(handler)
               for handler in handlers)
