"""TG2: Source Registry 纯本地部分（冻结设计 §6）。

start_at 在首次 add 时写入且不倒退（§6.3 硬边界）；
重复 add 幂等；enable/disable 是唯一运行开关。
discover/add 真实账号入口由 TG3 接 TelegramClientPort 后提供。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .event_store import TelegramEventStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def register_source(store: TelegramEventStore, source_id: str, *,
                    chat_id: int | None = None, display_name: str = "",
                    enabled: bool = True) -> None:
    """首次登记即写 start_at（部署启用时刻），重复调用幂等。"""
    store.add_source(source_id=source_id, chat_id=chat_id,
                     display_name=display_name, start_at=_now_iso(),
                     enabled=enabled)


def set_enabled(store: TelegramEventStore, source_id: str,
                enabled: bool) -> None:
    store.set_source_enabled(source_id, enabled)
