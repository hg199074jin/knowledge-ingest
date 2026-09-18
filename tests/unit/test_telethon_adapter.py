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
