"""V3 M3: separate Insight AI budget — 7-stage 独立账本（设计 §22.2）。

与 telegram source_ai_budget 完全隔离（不读不写）；permit 幂等；
empty/rate_limit 连续熔断；denied 只映射为可恢复 stage 状态，
绝不映射为 REJECT/无价值（Review Focus #2）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import InsightStore

BUDGET_OUTCOMES = ("success", "empty", "rate_limit", "error")


@dataclass(frozen=True)
class InsightAIBudgetGuard:
    """对 InsightStore.insight_ai_budget / insight_ai_permits 的门面。"""

    store: InsightStore
    breaker_empty_limit: int = 3
    breaker_rate_limit: int = 2

    STAGES = (
        "candidate_filter", "deep_value_gate", "retrieval_query_plan",
        "retrieval_select", "evidence_extract", "thinking", "critic")

    def acquire(self, stage: str, request_id: str) -> tuple[str, str]:
        if stage not in self.STAGES:
            raise ValueError(f"unknown insight stage: {stage!r}")
        if self.store.get_budget_permit(stage, request_id) is not None:
            return "allowed", f"{stage}:{request_id}"     # 重放幂等
        row = self.store.get_budget_row(stage)
        if row is not None:
            if int(row["consecutive_empty"]) >= self.breaker_empty_limit:
                return "denied", "breaker_empty"
            if (int(row["consecutive_rate_limit"])
                    >= self.breaker_rate_limit):
                return "denied", "breaker_rate_limit"
        self.store.acquire_budget_permit(stage, request_id)
        return "allowed", f"{stage}:{request_id}"

    def outcome(self, permit_id: str, result: str) -> None:
        if result not in BUDGET_OUTCOMES:
            raise ValueError(f"unknown budget outcome: {result!r}")
        if ":" not in permit_id:
            raise ValueError(f"malformed permit id: {permit_id!r}")
        stage, request_id = permit_id.split(":", 1)
        self.store.settle_budget_permit(stage, request_id, result)
        self.store.bump_budget_outcome(stage, result)

    def reset(self, stage: str | None = None) -> None:
        self.store.reset_budget(stage)
