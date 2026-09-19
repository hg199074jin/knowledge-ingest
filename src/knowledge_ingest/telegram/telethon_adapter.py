"""TG3: TelethonAdapter — TelegramClientPort 的 Telethon 实现（§7.4）。

§5.2 无 extra 契约：本模块顶层不 import Telethon；真正需要
Telethon 的路径经 _require_telethon() 延迟导入，未安装 extra 时
抛 TelegramDependencyMissingError，业务层绝不看到裸 ImportError。
FloodWait / RPCError 在 `_guard` 内映射为领域错误（§7.1）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from .client_port import (
    TelegramDependencyMissingError,
    TelegramDialog,
    TelegramDocumentRef,
    TelegramEvent,
    TelegramEventKind,
    TelegramFloodWaitError,
    TelegramRPCError,
)

CLOUD_LINK_HINTS = ("quark.cn", "pan.baidu.com")


def _require_telethon():
    try:
        import telethon
    except ModuleNotFoundError as exc:
        raise TelegramDependencyMissingError(
            "telegram extra not installed; "
            "run: uv sync --extra telegram") from exc
    return telethon


def _document_filename(message) -> str | None:
    for attr in ("file", "document"):
        obj = getattr(message, attr, None)
        name = getattr(obj, "name", None) if obj is not None else None
        if name:
            return name
    return None


def _to_event(kind: TelegramEventKind, message) -> TelegramEvent:
    """Telethon message → 领域事件（鸭子类型访问，便于 fake 测试）。"""
    document = getattr(message, "document", None)
    ref = None
    if document is not None:
        ref = TelegramDocumentRef(
            document_id=getattr(document, "id", 0),
            file_name=(getattr(document, "name", None)
                       or _document_filename(message)),
            mime_type=getattr(document, "mime_type", None),
            size_bytes=getattr(document, "size", None))
    return TelegramEvent(
        kind=kind,
        chat_id=getattr(message, "chat_id", 0) or 0,
        message_id=message.id,
        message_date=getattr(message, "date", None),
        sender_id=getattr(message, "sender_id", None),
        text=getattr(message, "message", None),
        document=ref,
    )


def extract_cloud_links(text: str | None) -> tuple[str, ...]:
    """§9.3：识别夸克/百度网盘链接；识别不出为空（unknown 由 TG4 判）。"""
    if not text:
        return ()
    return tuple(line.strip() for line in text.splitlines()
                 if any(hint in line for hint in CLOUD_LINK_HINTS))


def _proxy_from_credentials(credentials: dict):
    """可选代理配置（PROXY_TYPE / PROXY_HOST / PROXY_PORT）。

    国内网络直连 Telegram DC 被墙时，经本机代理（如 Clash Verge
    的 mixed 口）连接；三个键都不在 = 直连（返回 None）。
    PROXY_TYPE 一旦出现，HOST/PORT 必须齐全且合法。
    """
    proxy_type = credentials.get("PROXY_TYPE")
    if not proxy_type:
        return None
    from python_socks import ProxyType

    mapping = {"socks5": ProxyType.SOCKS5, "socks4": ProxyType.SOCKS4,
               "http": ProxyType.HTTP}
    key = str(proxy_type).strip().lower()
    if key not in mapping:
        raise ValueError(
            f"unsupported PROXY_TYPE: {proxy_type!r}; expected one of "
            f"{sorted(mapping)}")
    host = credentials.get("PROXY_HOST")
    if not host:
        raise ValueError("PROXY_HOST is required when PROXY_TYPE is set")
    raw_port = credentials.get("PROXY_PORT")
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid PROXY_PORT: {raw_port!r}") from exc
    return (mapping[key], str(host), port)


class TelethonAdapter:
    """延迟导入 Telethon 的 TelegramClientPort 实现。"""

    def __init__(self, session_dir: str | Path, credentials: dict):
        self.session_dir = Path(session_dir)
        self.credentials = credentials
        self._client = None

    # ---- 生命周期 ----

    def session_path(self) -> Path:
        name = self.credentials.get("SESSION_NAME", "session")
        return self.session_dir / f"{name}.session"

    def _require_telethon(self):
        return _require_telethon()

    def _build_client(self):
        telethon = self._require_telethon()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        client = telethon.TelegramClient(
            str(self.session_path()),
            int(self.credentials["API_ID"]),
            self.credentials["API_HASH"],
            proxy=_proxy_from_credentials(self.credentials))
        self._client = client
        return client

    def _client_or_new(self):
        return self._client if self._client is not None \
            else self._build_client()

    async def _guard(self, coro):
        """把 Telethon 专属错误收口映射为领域错误（§7.1）。"""
        try:
            return await coro
        except (TelegramDependencyMissingError, TelegramFloodWaitError,
                TelegramRPCError):
            raise
        except Exception as exc:
            errors = _require_telethon().errors
            if isinstance(exc, errors.FloodWaitError):
                raise TelegramFloodWaitError(
                    int(getattr(exc, "seconds", 0))) from exc
            if isinstance(exc, errors.RPCError):
                raise TelegramRPCError(
                    str(exc), code=getattr(exc, "code", None)) from exc
            raise

    # ---- H1 认证 ----

    def interactive_sign_in(self) -> Path:
        """阻塞式交互登录：手机号/验证码/2FA 由 telethon 在终端提示。

        验证码与 2FA 只经内存，telethon 不落日志；成功后 session
        由 run_auth 统一收紧为 600。
        """
        client = self._build_client()
        client.start()
        client.disconnect()
        return self.session_path()

    def list_dialogs_sync(self) -> list[TelegramDialog]:
        """同步入口（无 extra 契约测试与简单排查用）。"""
        _require_telethon()
        return asyncio.run(self.list_dialogs())

    # ---- port 实现 ----

    async def list_dialogs(self) -> list[TelegramDialog]:
        client = self._client_or_new()

        async def call():
            return [dialog async for dialog in client.iter_dialogs()]

        raw = await self._guard(call())
        return [TelegramDialog(
            chat_id=dialog.id,
            title=getattr(dialog, "name", None) or "",
            username=getattr(dialog, "username", None),
        ) for dialog in raw]

    async def resolve_chat(self, ref: str) -> TelegramDialog:
        telethon = self._require_telethon()
        client = self._client_or_new()

        async def call():
            return await client.get_entity(ref)

        entity = await self._guard(call())
        return TelegramDialog(
            chat_id=telethon.utils.get_peer_id(entity),
            title=getattr(entity, "title", None)
            or getattr(entity, "username", None) or ref,
            username=getattr(entity, "username", None),
        )

    async def latest_message(self, chat_id: int) -> TelegramEvent | None:
        client = self._client_or_new()

        async def call():
            return await client.get_messages(chat_id, limit=1)

        messages = await self._guard(call())
        return _to_event(TelegramEventKind.NEW, messages[0]) \
            if messages else None

    async def fetch_messages_after(
        self, chat_id: int, after_message_id: int) -> list[TelegramEvent]:
        client = self._client_or_new()

        async def call():
            return await client.get_messages(
                chat_id, min_id=after_message_id, reverse=True, limit=1000)

        messages = await self._guard(call())
        return [_to_event(TelegramEventKind.NEW, message)
                for message in messages]

    async def get_message(self, chat_id: int,
                          message_id: int) -> TelegramEvent | None:
        client = self._client_or_new()

        async def call():
            return await client.get_messages(chat_id, ids=message_id)

        messages = await self._guard(call())
        if not messages or messages[0] is None:
            return None
        return _to_event(TelegramEventKind.NEW, messages[0])

    async def download_document(self, chat_id: int, message_id: int,
                                dest_dir: Path) -> Path:
        client = self._client_or_new()
        messages = await self._guard(
            client.get_messages(chat_id, ids=message_id))
        if not messages or messages[0] is None:
            raise TelegramRPCError(
                f"message not found: {chat_id}#{message_id}")

        async def call():
            return await client.download_media(messages[0],
                                               file=str(dest_dir))

        path = await self._guard(call())
        return Path(path)

    async def watch_updates(self) -> AsyncIterator[TelegramEvent]:
        from telethon import events  # 延迟导入（§5.2）

        client = self._client_or_new()
        if not client.is_connected():
            await self._guard(client.connect())
        queue: asyncio.Queue[TelegramEvent] = asyncio.Queue()

        def make_handler(kind: TelegramEventKind):
            def handler(update_event):
                message = getattr(update_event, "message", None)
                chat_id = (getattr(update_event, "chat_id", None)
                           or getattr(message, "chat_id", 0) or 0)
                if message is not None:
                    queue.put_nowait(_to_event(kind, message))
                    return
                for deleted_id in (getattr(update_event, "deleted_ids",
                                           None) or []):
                    queue.put_nowait(TelegramEvent(
                        kind=TelegramEventKind.DELETE,
                        chat_id=chat_id, message_id=deleted_id))
            return handler

        client.add_event_handler(
            make_handler(TelegramEventKind.NEW), events.NewMessage)
        client.add_event_handler(
            make_handler(TelegramEventKind.EDIT), events.MessageEdited)
        client.add_event_handler(
            make_handler(TelegramEventKind.DELETE), events.MessageDeleted)
        try:
            while True:
                yield await self._guard(queue.get())
        finally:
            client.remove_event_handler(
                make_handler(TelegramEventKind.NEW), events.NewMessage)
            client.remove_event_handler(
                make_handler(TelegramEventKind.EDIT), events.MessageEdited)
            client.remove_event_handler(
                make_handler(TelegramEventKind.DELETE),
                events.MessageDeleted)
