"""TG8: Retention（§10.4）——SKIP 载荷 30 天清理，provenance 永留。

语义（冻结 §18.2）：
- KEEP：原始 PDF / 正文文件长期保留；
- SKIP：原始载荷约 30 天后清理（文件删除，下载行改 `purged` 留证）；
- REVIEW：未解决不清理；
- 已删除的 Telegram 源消息按 §20 语义保留审计事实，retention 不触碰
  tg_messages / classifier_audit / reviews。

流程强制：dry-run（默认）→ 明确候选数 → maintenance lock → delete。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledge_ingest.config import AppConfig

from .event_store import TelegramEventStore
from .locks import maintenance_lock

DEFAULT_DAYS = 30


def candidates(store: TelegramEventStore, *, days: int) -> list[dict]:
    """SKIP 终态且超期的下载载荷候选（KEEP/REVIEW 永不入选）。"""
    cutoff = (datetime.now(UTC)
              - timedelta(days=days)).isoformat(timespec="seconds")
    rows = store._conn.execute(
        """SELECT d.item_id, d.local_path, d.sha256
           FROM downloads d JOIN source_items i ON i.item_id = d.item_id
           WHERE d.status = 'complete' AND d.local_path IS NOT NULL
             AND i.processing_status IN ('skipped_noise',
                                         'skipped_interest',
                                         'skipped_unsupported')
             AND COALESCE(i.finalized_at, i.created_at) < ?
             AND NOT EXISTS (SELECT 1 FROM reviews r
                             WHERE r.item_id = d.item_id
                               AND r.resolved_at IS NULL)
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def run(config: AppConfig, *, days: int = DEFAULT_DAYS,
        execute: bool = False) -> int:
    db = config.pipeline_root / "telegram" / "state.db"
    if not db.is_file():
        print("retention: state.db 不存在，无事可做")
        return 0
    store = TelegramEventStore(db)
    cands = candidates(store, days=days)
    mode = "execute" if execute else "dry-run"
    print(f"retention ({mode}, days={days}): {len(cands)} 个候选载荷")
    for c in cands:
        print(f"  candidate: {c['item_id']} sha={str(c['sha256'])[:12]}…")
    if not cands:
        return 0
    if not execute:
        print("dry-run：未删除任何文件（加 --execute 执行）")
        return 0
    deleted = 0
    with maintenance_lock(db):
        # 锁内重算候选（迁移/并发可能已改变事实）
        cands = candidates(store, days=days)
        for c in cands:
            path = Path(c["local_path"])
            if path.is_file():
                path.unlink()
            store.mark_download_purged(c["item_id"])
            deleted += 1
    print(f"retention: deleted {deleted} payload(s); "
          "provenance/审计行保留（downloads.status=purged）")
    return 0
