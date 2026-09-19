"""TG7: Notification Runtime（§9）——通知与日报，事件幂等。

NotificationPort 是唯一发送边界；WxPusherAdapter 是其 HTTP 实现
（urllib，无新增依赖）。事件幂等由 state.db 的 notifications 表保证：
同一 event_id 只投递一次，重放安全。凭据来自
~/.config/knowledge-ingest/telegram/wxpusher.env（H2 人工环节）：
    WXPUSHER_APP_TOKEN=...
    WXPUSHER_UID=...
凭据缺失时 notify/digest 明确降级为本地输出，绝不静默丢弃事件
（先落 notifications 表，补凭据后可重发）。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Protocol

from .event_store import TelegramEventStore

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


class NotificationPort(Protocol):
    def send(self, uid: str, summary: str, content: str) -> bool: ...


def wxpusher_env_path(session_dir: Path | None = None) -> Path:
    return (session_dir or (Path.home() / ".config" / "knowledge-ingest"
                            / "telegram")) / "wxpusher.env"


def load_wxpusher_credentials(path: Path | None = None) -> dict[str, str]:
    """解析 wxpusher.env（KEY=VALUE）；缺失/不完整 → 空 dict（降级）。"""
    path = path or wxpusher_env_path()
    if not path.is_file():
        return {}
    creds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        creds[key.strip()] = value.strip()
    if not creds.get("WXPUSHER_APP_TOKEN") or not creds.get("WXPUSHER_UID"):
        return {}
    return creds


class WxPusherAdapter:
    """NotificationPort 的 WxPusher 实现（HTTP，urllib）。"""

    def __init__(self, app_token: str, uid: str, *,
                 api_url: str = WXPUSHER_API, timeout: int = 15):
        self.app_token = app_token
        self.uid = uid
        self.api_url = api_url
        self.timeout = timeout

    def send(self, uid: str, summary: str, content: str) -> bool:
        payload = json.dumps({
            "appToken": self.app_token, "content": content,
            "summary": summary[:99], "contentType": 1,
            "uids": [uid or self.uid],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.api_url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("code") == 1000)


def null_port(uid: str, summary: str, content: str) -> bool:
    """无凭据时的降级端口：本地打印，返回 False（未真正投递）。"""
    print(f"[notify:degraded] {summary}\n{content}")
    return False


def notify(store: TelegramEventStore, port: NotificationPort, *,
           event_id: str, channel: str, summary: str, content: str,
           uid: str) -> bool:
    """幂等通知：事件先落库；**已成功投递**的事件拒绝重放。

    投递失败/异常 → 事件保留在未投递态，同一 event_id 再次调用会重试
    （评审 I6：原实现失败后事件被永久消费，"补凭据后可重发"没有实现）。
    """
    fresh = store.record_notification(event_id, channel, summary, content)
    if not fresh and store.notification_delivered(event_id):
        return False                                  # 已投递过：重放安全
    try:
        delivered = port.send(uid, summary, content)
    except Exception:                                 # noqa: BLE001
        return False                                  # 未送达：保留重试
    if delivered:
        store.mark_notification_delivered(event_id)
    return delivered


def build_digest(store: TelegramEventStore, *,
                 since: str | None = None) -> str:
    """构造日报文本：待审 REVIEW、通道健康、新入库摘要。"""
    lines = ["Telegram 采集日报"]
    reviews = store.list_open_reviews()
    lines.append(f"待人工审阅（REVIEW）：{len(reviews)} 条")
    for r in reviews[:5]:
        lines.append(f"  review={r['review_id']} item={r['item_id']} "
                     f"reason={r['reason']} size={r['size_bytes'] or '-'}")
    budgets = store.list_ai_budgets()
    hot = [b for b in budgets if b["consecutive_empty"]
           or b["consecutive_rate_limit"]]
    lines.append(f"通道账本：{len(budgets)} 行，熔断异常 {len(hot)} 行")
    for b in hot[:3]:
        lines.append(f"  {b['source_id']}:{b['classifier_kind']} "
                     f"empty={b['consecutive_empty']} "
                     f"rate={b['consecutive_rate_limit']}")
    if since:
        rows = store._conn.execute(
            "SELECT item_id, source_id, kind, processing_status "
            "FROM source_items WHERE created_at > ? "
            "ORDER BY created_at DESC LIMIT 10", (since,)).fetchall()
        lines.append(f"新建 Item（自 {since}）：{len(rows)} 条")
        for r in rows:
            lines.append(f"  {r['item_id']} [{r['kind']}] "
                         f"{r['processing_status']}")
        # §9.7：分类分布与进入 KI 计数（TG7 评审 I7 补齐的日报内容）
        counts = store._conn.execute(
            """SELECT kind, processing_status, COUNT(*) n
               FROM source_items WHERE created_at > ?
               GROUP BY kind, processing_status ORDER BY n DESC""",
            (since,)).fetchall()
        breakdown = ", ".join(f"{r['kind']}/{r['processing_status']}={r['n']}"
                              for r in counts)
        lines.append(f"  分布：{breakdown or '无'}")
        handed = store._conn.execute(
            "SELECT COUNT(*) FROM source_items WHERE created_at > ? "
            "AND handoff_completed = 1", (since,)).fetchone()[0]
        lines.append(f"  进入 KI（handoff）：{handed} 条")
    return "\n".join(lines)
