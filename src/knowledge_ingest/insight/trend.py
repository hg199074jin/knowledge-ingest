"""V3 M4: Weak-Signal Trend Aggregator（设计 §8）。

单篇弱信号进 WATCH；同归一化 trend_key 在 30 天窗口内累计
3 条独立信号 → 趋势聚类 ready（可被 Deep-Value Gate 重新评估）。
全部确定性、幂等：重复扫描不重复计数。

冻结默认（实施方案 Task 4 Step 5）：window_days=30, threshold=3，
测试冻结默认值；配置/环境覆盖留待后续。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta

from .store import InsightStore

WINDOW_DAYS = 30
THRESHOLD = 3

_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff\u3400-\u4dbf]+", re.UNICODE)


def normalize_trend_key(key: str) -> str:
    """NFKC + 去 NonWord + casefold：排版/大小写变体归一。"""
    return _STRIP_RE.sub(
        "", unicodedata.normalize("NFKC", key or "")).casefold()


class TrendAggregator:
    def __init__(self, store: InsightStore, *, window_days: int = WINDOW_DAYS,
                 threshold: int = THRESHOLD):
        self.store = store
        self.window_days = window_days
        self.threshold = threshold

    def record(self, insight_source_id: str, source_revision: int,
               trend_key: str, reason: str = "") -> None:
        normalized = normalize_trend_key(trend_key)
        if not normalized:
            raise ValueError("trend_key normalizes to empty")
        if self.store.get_source(insight_source_id) is None:
            raise ValueError(
                f"insight source not found: {insight_source_id}")
        created = self.store.add_watch_signal(
            insight_source_id, source_revision, normalized, reason)
        if not created:
            return                          # 重放幂等：不重复计数
        self._refresh_cluster(normalized)

    def _refresh_cluster(self, trend_key: str) -> None:
        since = (datetime.now(UTC) - timedelta(days=self.window_days)
                 ).isoformat()
        count = self.store.count_watch_signals(trend_key, since)
        self.store.upsert_trend_cluster(
            trend_key, count,
            ready=count >= self.threshold)

    def ready_clusters(self) -> list:
        return self.store.list_ready_clusters(self.threshold)
