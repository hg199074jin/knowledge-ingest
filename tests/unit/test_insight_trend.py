"""V3 M4: TrendAggregator — 弱信号聚合触发（确定性、幂等）。

冻结默认（实施方案 Task 4 Step 5）：
- 窗口 30 天；同归一化 trend_key 累计 3 条 WATCH 信号 → 聚类 ready；
- 重复扫描幂等（同 source+revision+key 不重复计数）；
- 聚类不因 count>=3 而宣称来源独立。
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.store import InsightStore
from knowledge_ingest.insight.trend import TrendAggregator, normalize_trend_key

NOW = "2026-09-28T12:00:00+00:00"


def make_store(tmp_path) -> InsightStore:
    return InsightStore(tmp_path / "insight" / "state.db")


def register_source(store, source_item_id) -> str:
    """信号溯源要求来源已注册：最小 text view。"""
    view = InsightSourceView(
        provider="telegram", source_item_id=source_item_id,
        content_kind="text", title=None, visible_text="body",
        materialized_path=None, full_text_available=True,
        verification_status="source_only", content_fingerprint="fp",
        captured_at=NOW)
    return store.register_source_view(view)


def record_three_signals(tmp_path, key="jev-decision-layer"):
    store = make_store(tmp_path)
    agg = TrendAggregator(store)
    sids = [register_source(store, f"tg_a:{i}") for i in range(3)]
    for sid in sids:
        agg.record(sid, 1, key, reason="信号")
    return store, agg, sids


def test_normalize_trend_key_case_and_punct():
    assert normalize_trend_key("AI 门店短视频！") == \
        normalize_trend_key("ai 门店短视频")
    assert normalize_trend_key("  Jev-Decisions  ") == "jevdecisions"


def test_threshold_reached_creates_ready_cluster(tmp_path):
    _store, agg, _sids = record_three_signals(tmp_path)
    clusters = agg.ready_clusters()
    assert len(clusters) == 1
    assert clusters[0]["trend_key"] == "jevdecisionlayer"
    assert clusters[0]["signal_count"] == 3


def test_below_threshold_not_ready(tmp_path):
    store = make_store(tmp_path)
    agg = TrendAggregator(store)
    sid = register_source(store, "tg_a:1")
    agg.record(sid, 1, "jev-decision-layer")
    assert agg.ready_clusters() == []


def test_repeated_scan_idempotent(tmp_path):
    _store, agg, sids = record_three_signals(tmp_path)
    for sid in sids:                        # 重放同样三条
        agg.record(sid, 1, "jev-decision-layer", reason="信号")
    clusters = agg.ready_clusters()
    assert len(clusters) == 1
    assert clusters[0]["signal_count"] == 3


def test_window_excludes_signals_older_than_30_days(tmp_path):
    store = make_store(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    for i in range(3):
        sid = register_source(store, f"tg_old:{i}")
        store.add_watch_signal(sid, 1, "trend-key", "旧信号", created_at=old)
    agg = TrendAggregator(store)
    agg.record(register_source(store, "tg_new:1"), 1, "trend-key")
    assert agg.ready_clusters() == []   # 窗口内只有 1 条


def test_distinct_keys_form_distinct_clusters(tmp_path):
    store = make_store(tmp_path)
    agg = TrendAggregator(store)
    for i in range(3):
        agg.record(register_source(store, f"tg_a:{i}"), 1, "alpha-key")
    for i in range(3):
        agg.record(register_source(store, f"tg_b:{i}"), 1, "beta-key")
    clusters = agg.ready_clusters()
    assert {c["trend_key"] for c in clusters} == {"alphakey", "betakey"}


def test_record_unknown_source_rejected(tmp_path):
    store = make_store(tmp_path)
    agg = TrendAggregator(store)
    with pytest.raises(ValueError):
        agg.record("isv.missing", 1, "some-key")   # 源必须已注册


def test_cluster_does_not_claim_source_independence(tmp_path):
    _store, agg, _sids = record_three_signals(tmp_path)
    cluster = agg.ready_clusters()[0]
    meta = json.loads(cluster["meta_json"])
    assert meta.get("independent") is False   # count>=3 不宣称来源独立
