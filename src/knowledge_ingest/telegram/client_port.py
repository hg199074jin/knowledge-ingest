"""TG3: TelegramClientPort — MTProto client 隔离层（冻结设计 §7）。

业务层只消费本模块定义的 TelegramEvent / TelegramDocumentRef /
TelegramDialog；Telethon 对象不得穿透（§7.1）。Telethon 专属错误
（FloodWait / RPCError）在 Adapter 内映射为这里的领域错误。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


class TelegramEventKind(str, Enum):
    NEW = "NEW"
    EDIT = "EDIT"
    DELETE = "DELETE"


class TelegramDependencyMissingError(RuntimeError):
    """telegram extra 未安装（§5.2 无 extra 契约）。"""


class TelegramAuthRequiredError(RuntimeError):
    """缺少可用 session / 凭据（§5.5，人工环节 H1）。"""


class TelegramFloodWaitError(RuntimeError):
    """FloodWait：正常运行状态，按秒数等待后重试（§3.1/§19）。"""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"telegram flood wait: {seconds}s")


class TelegramRPCError(RuntimeError):
    """Telethon RPCError 的领域映射。"""

    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TelegramDialog:
    chat_id: int
    title: str
    username: str | None = None


@dataclass(frozen=True)
class TelegramDocumentRef:
    document_id: int
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class TelegramEvent:
    """统一事件契约。§5.3：DELETE 只需身份字段（chat_id + message_id），
    即便拿不到正文也足以定位 Raw Event 行。"""

    kind: TelegramEventKind
    chat_id: int
    message_id: int
    message_date: datetime | None = None
    sender_id: int | None = None
    text: str | None = None
    edited_at: datetime | None = None
    document: TelegramDocumentRef | None = None
    cloud_links: tuple[str, ...] = ()


@runtime_checkable
class TelegramClientPort(Protocol):
    """MTProto client 的最小业务面；实现方以 Adapter 提供。"""

    async def list_dialogs(self) -> list[TelegramDialog]: ...

    async def watch_updates(self) -> AsyncIterator[TelegramEvent]: ...

    async def fetch_messages_after(
        self, chat_id: int, after_message_id: int) -> list[TelegramEvent]: ...

    async def latest_message(self, chat_id: int) -> TelegramEvent | None: ...

    async def get_message(self, chat_id: int,
                          message_id: int) -> TelegramEvent | None: ...

    async def download_document(self, chat_id: int, message_id: int,
                                dest_dir: Path) -> Path: ...

    async def resolve_chat(self, ref: str) -> TelegramDialog: ...
