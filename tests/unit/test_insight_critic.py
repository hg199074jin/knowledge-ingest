"""V3 M6: Quality Critic——独立复核契约与一次修订编排。

冻结语义（设计 §18；实施方案 Task 6 Step 5–7）：
- Critic 输出字段：8 个质量维度（pass/partial/fail）+ genericity_detected
  + revision_required + revision_instructions[]；
- 编排：think → review →（revision_required 时）revise → review，
  最多一次修订 = 恰好最多 2 次 thinker 调用；
- 终审仍失败 → status=needs_review，无 quality_status=passed，
  不产生 Cognition Proposal；
- Critic 自身失败 → blocked（可恢复），绝不降级成普通摘要卡；
- 陌生人测试 / 三个月后测试随 prompt 下发。
"""

import pytest

from knowledge_ingest.insight.content_resolver import ContentPart
from knowledge_ingest.insight.critic import (
    QualityCritic,
    ThinkingOrchestrator,
)
from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import (
    EvidencePack,
    PersonalContextRef,
)

NOW = "2026-09-28T12:00:00+00:00"

PACK = EvidencePack(
    source_claims=("判定模型可承担部分决策",),
    personal_context=(PersonalContextRef(
        record_id="PC-001", relation_reason="可能新增判定层",
        state="CONFIRMED", kind="cognition", text="两级筛选架构"),),
    source_parts=(ContentPart(part_id="main", text="原文全文",
                              source_ref="/x"),))

DRAFT = {
    "bottom_line": "b", "source_understanding": "s", "mechanism": "m",
    "challenge": "c", "personal_connections": [], "project_impacts": [],
    "business_opportunity": None, "own_version": "o",
    "cognition_delta": "REVISE", "actions": [], "final_verdict": "CARD",
    "human_gate_recommendation": "WATCH",
}

CRITIC_PASS = {
    "source_understanding": "pass", "critical_reasoning": "pass",
    "personal_connection": "pass", "cognition_delta": "pass",
    "own_version": "pass", "actionability": "pass",
    "business_rigor": "pass", "traceability": "pass",
    "genericity_detected": False, "revision_required": False,
    "revision_instructions": [],
}

CRITIC_FAIL = {
    "source_understanding": "pass", "critical_reasoning": "fail",
    "personal_connection": "fail", "cognition_delta": "fail",
    "own_version": "fail", "actionability": "pass",
    "business_rigor": "pass", "traceability": "pass",
    "genericity_detected": True, "revision_required": True,
    "revision_instructions": ["own_version 只是换说法；补机制级重构",
                              "personal_connection 是主题级硬关联"],
}


class FakeThinker:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = 0

    def think(self, pack):
        self.calls += 1
        return self.drafts[0]

    def revise(self, pack, draft, instructions):
        self.calls += 1
        return self.drafts[min(1, len(self.drafts) - 1)]


class FakeCritic:
    def __init__(self, reviews):
        self.reviews = list(reviews)
        self.calls = 0
        self.fail = False
        self._parser = QualityCritic(port=None)

    def review(self, pack, draft):
        self.calls += 1
        if self.fail:
            raise ModelBadOutputError("critic bad output")
        return self._parser.parse(self.reviews.pop(0))


def make_orchestrator(reviews, drafts=None):
    thinker = FakeThinker(drafts or [DRAFT, DRAFT])
    critic = FakeCritic(reviews)
    orch = ThinkingOrchestrator(thinker, critic)
    return orch, thinker, critic


def pack_of(_x=None):
    return PACK


# ---------- happy path ----------

def test_first_pass_success_single_thinker_call():
    orch, thinker, critic = make_orchestrator([dict(CRITIC_PASS)])
    outcome = orch.run(PACK)
    assert outcome.status == "passed"
    assert thinker.calls == 1
    assert critic.calls == 1
    assert outcome.draft == DRAFT


# ---------- revision loop ----------

def test_revision_exactly_two_thinker_calls_then_pass():
    orch, thinker, critic = make_orchestrator(
        [dict(CRITIC_FAIL), dict(CRITIC_PASS)])
    outcome = orch.run(PACK)
    assert outcome.status == "passed"
    assert thinker.calls == 2                  # 初稿 + 恰好一次修订
    assert critic.calls == 2
    assert outcome.final_review.revision_required is False


def test_still_failing_after_revision_needs_review():
    orch, thinker, _critic = make_orchestrator(
        [dict(CRITIC_FAIL), dict(CRITIC_FAIL)])
    outcome = orch.run(PACK)
    assert outcome.status == "needs_review"
    assert thinker.calls == 2                  # 硬停，无无限循环
    assert outcome.draft["cognition_delta"] == "REVISE"


# ---------- critic failure ----------

def test_critic_failure_yields_blocked_not_summary_card():
    orch, _thinker, critic = make_orchestrator([dict(CRITIC_PASS)])
    critic.fail = True
    outcome = orch.run(PACK)
    assert outcome.status == "blocked"
    assert outcome.draft is not None           # 草稿保留待恢复
    assert outcome.final_review is None


def test_revision_instructions_forwarded():
    orch, thinker, _ = make_orchestrator([dict(CRITIC_FAIL), dict(CRITIC_PASS)])
    seen = {}

    def fake_revise(pack, draft, instructions):
        seen["instructions"] = instructions
        return DRAFT

    thinker.revise = fake_revise
    orch.run(PACK)
    assert seen["instructions"] == CRITIC_FAIL["revision_instructions"]


# ---------- critic parse contract ----------

def test_critic_missing_field_raises_retryable():
    critic = QualityCritic(port=None)
    bad = {k: v for k, v in CRITIC_FAIL.items()
           if k != "genericity_detected"}
    with pytest.raises(ModelBadOutputError):
        critic.parse(bad)


def test_critic_bool_fields_must_be_bool():
    critic = QualityCritic(port=None)
    bad = dict(CRITIC_PASS, genericity_detected="yes")
    with pytest.raises(ModelBadOutputError):
        critic.parse(bad)


# ---------- prompt carries the two hard tests ----------

def test_prompt_carries_stranger_and_three_month_tests():
    captured = {}

    class Port:
        def run(self, stage, payload):
            captured.update(payload)
            return dict(CRITIC_PASS)

    critic = QualityCritic(port=Port())
    critic.review(PACK, DRAFT)
    blob = str(captured)
    assert "陌生人" in blob and "三个月" in blob
