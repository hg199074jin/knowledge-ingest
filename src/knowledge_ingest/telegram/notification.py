"""TG7: Notification Runtime（§9）——通知与日报，事件幂等。

通用件（NotificationPort / WxPusherAdapter / 凭据加载 / 降级端口）
已抽取到 knowledge_ingest.notification（A5）；本模块保留 Telegram
专属的幂等落库 notify() 与日报聚合 build_digest()。
"""

from __future__ import annotations

from knowledge_ingest.notification import (
    NotificationPort,
    WxPusherAdapter,
    load_wxpusher_credentials,
    null_port,
    wxpusher_env_path,
)

from .event_store import TelegramEventStore

__all__ = [
    "NotificationPort",
    "WxPusherAdapter",
    "build_digest",
    "load_wxpusher_credentials",
    "notify",
    "null_port",
    "wxpusher_env_path",
]


def notify(store: TelegramEventStore, port: NotificationPort, *,
           event_id: str, channel: str, summary: str, content: str,
           uid: str) -> bool:
    """幂等通知：事件先落库；**已成功投递**的事件拒绝重放。

    投递失败/异常 → 事件保留在未投递态，同一 event_id 再次调用会重试
    （评审 I6）。
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
