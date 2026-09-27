"""V3 M3: InsightAIBudgetGuard — 7-stage 独立账本、幂等 permit、熔断。

- stage 固定 7 个：candidate_filter / deep_value_gate / retrieval_query_plan /
  retrieval_select / evidence_extract / thinking / critic；
- outcome 固定四值：success / empty / rate_limit / error；
- 同 request_id 重放幂等（attempts 不重复计）；
- empty/rate_limit 连续计数达阈值 → 后续 acquire denied（可恢复，
  绝不映射为 REJECT）；
- insight 账本与 telegram source_ai_budget 完全隔离。
"""

import pytest

from knowledge_ingest.insight.budget import InsightAIBudgetGuard
from knowledge_ingest.insight.store import InsightStore

STAGES = ("candidate_filter", "deep_value_gate", "retrieval_query_plan",
          "retrieval_select", "evidence_extract", "thinking", "critic")


def make_store(tmp_path) -> InsightStore:
    return InsightStore(tmp_path / "insight" / "state.db")


def make_guard(tmp_path, **overrides) -> InsightAIBudgetGuard:
    return InsightAIBudgetGuard(make_store(tmp_path), **overrides)


def test_seven_stages_fixed():
    assert InsightAIBudgetGuard.STAGES == STAGES


def test_acquire_creates_permit_and_counts_once(tmp_path):
    guard = make_guard(tmp_path)
    status, permit = guard.acquire("thinking", "req-1")
    assert status == "allowed"
    assert permit == "thinking:req-1"
    status2, permit2 = guard.acquire("thinking", "req-1")   # 重放幂等
    assert (status2, permit2) == ("allowed", permit)
    row = guard.store.get_budget_row("thinking")
    assert row["attempts"] == 1                              # 不重复计


def test_outcome_success_increments_calls_and_resets_breakers(tmp_path):
    guard = make_guard(tmp_path)
    _, permit = guard.acquire("critic", "r1")
    guard.outcome(permit, "empty")
    guard.outcome(guard.acquire("critic", "r2")[1], "success")
    row = guard.store.get_budget_row("critic")
    assert row["calls"] == 1
    assert row["consecutive_empty"] == 0


def test_empty_breaker_denies_after_limit(tmp_path):
    guard = make_guard(tmp_path, breaker_empty_limit=3)
    for i in range(3):
        _, permit = guard.acquire("candidate_filter", f"e{i}")
        guard.outcome(permit, "empty")
    status, reason = guard.acquire("candidate_filter", "e-next")
    assert status == "denied"
    assert reason == "breaker_empty"


def test_rate_limit_breaker_denies_after_limit(tmp_path):
    guard = make_guard(tmp_path, breaker_rate_limit=2)
    for i in range(2):
        guard.outcome(guard.acquire("deep_value_gate", f"r{i}")[1],
                      "rate_limit")
    status, reason = guard.acquire("deep_value_gate", "r-next")
    assert (status, reason) == ("denied", "breaker_rate_limit")


def test_success_clears_open_breaker(tmp_path):
    guard = make_guard(tmp_path, breaker_empty_limit=2)
    guard.outcome(guard.acquire("thinking", "a")[1], "empty")
    guard.outcome(guard.acquire("thinking", "b")[1], "empty")
    assert guard.acquire("thinking", "c")[0] == "denied"
    guard.store.reset_budget("thinking")
    guard.outcome(guard.acquire("thinking", "d")[1], "empty")
    assert guard.acquire("thinking", "e")[0] == "allowed"


def test_unknown_outcome_rejected(tmp_path):
    guard = make_guard(tmp_path)
    _, permit = guard.acquire("thinking", "r1")
    with pytest.raises(ValueError):
        guard.outcome(permit, "excellent")


def test_outcome_unknown_permit_rejected(tmp_path):
    guard = make_guard(tmp_path)
    with pytest.raises(ValueError):
        guard.outcome("thinking:no-such", "success")


def test_stage_ledgers_independent(tmp_path):
    guard = make_guard(tmp_path, breaker_empty_limit=2)
    for i in range(2):
        guard.outcome(guard.acquire("thinking", f"t{i}")[1], "empty")
    assert guard.acquire("thinking", "t-next")[0] == "denied"
    assert guard.acquire("critic", "c1")[0] == "allowed"   # 不串账


def test_insight_budget_never_touches_telegram_budget(tmp_path):
    from knowledge_ingest.telegram.event_store import TelegramEventStore
    tg = TelegramEventStore(tmp_path / "telegram" / "state.db")
    guard = InsightAIBudgetGuard(InsightStore(tmp_path / "insight" / "state.db"))
    guard.outcome(guard.acquire("thinking", "r1")[1], "empty")
    rows = tg._conn.execute(
        "SELECT COUNT(*) FROM source_ai_budget").fetchone()[0]
    assert rows == 0
