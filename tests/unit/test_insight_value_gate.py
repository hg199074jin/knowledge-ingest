"""V3 M4: DeepValueGate — 第二层多维价值门契约。

冻结语义（Review §5 修订）：
- 接口为 evaluate(view, candidate)：**无 personal_preview 参数**——
  完整 Personal Retrieval 只在 DEEP_READ 之后由 M5 执行；
- 输出多维结构化字段 + 显式 decision（四态）；
  无总分阈值；
- contradiction_value 允许 unknown：此时无可靠个人认知证据，
  绝不为填字段推测用户旧认知；
- 契约违约（M10-R3）：首次违约触发一次有界 contract repair，
  两次仍非法 → 类型化可重试错误（BLOCKED_MODEL_BAD_OUTPUT）。
"""

import inspect

import pytest

from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import DeepValueDecision, InsightSourceView
from knowledge_ingest.insight.value_gate import DeepValueGate

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "年入500万的私域算法",
        "visible_text": "1000个群×每天5单×每单5元……",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


CANDIDATE_SUMMARY = {"possible_value": ["BUSINESS_OPPORTUNITY"],
                     "reasons": ["交易结构可检验"], "confidence": 0.7}

GATE_OK = {
    "novelty": "high", "cognitive_delta_potential": "high",
    "business_potential": "medium", "project_relevance": "low",
    "transferability": "high", "evidence_quality": "medium",
    "contradiction_value": "unknown", "thinking_space": "high",
    "decision": "DEEP_READ", "reason": "Excel 乘法≠商业模式，值得拆",
}


class FakePort:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def run(self, stage, payload):
        self.calls.append((stage, payload))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_gate(script):
    port = FakePort(script)
    return DeepValueGate(port), port


def test_full_dimensions_parse_to_decision():
    gate, port = make_gate([dict(GATE_OK)])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert isinstance(decision, DeepValueDecision)
    assert decision.decision == "DEEP_READ"
    assert decision.contradiction_value == "unknown"
    stage, payload = port.calls[0]
    assert stage == "deep_value_gate"
    assert payload["candidate"] == CANDIDATE_SUMMARY
    assert "1000个群" in payload["visible_text"]


def test_poor_writing_but_valuable_structure_still_deep_read():
    gate, _ = make_gate([dict(GATE_OK, evidence_quality="low")])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert decision.decision == "DEEP_READ"   # 文笔差≠价值低（设计 §9.4）


def test_watch_archive_reject_all_valid():
    for state in ("WATCH", "ARCHIVE_ONLY", "REJECT"):
        gate, _ = make_gate([dict(GATE_OK, decision=state)])
        assert gate.evaluate(make_view(), CANDIDATE_SUMMARY).decision == state


def test_missing_dimensions_default_none_not_fabricated():
    gate, _ = make_gate([{"decision": "WATCH", "reason": "证据不足"}])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert decision.novelty is None
    assert decision.contradiction_value is None


def test_contradiction_unknown_is_legal():
    gate, _ = make_gate([dict(GATE_OK, contradiction_value="unknown")])
    assert gate.evaluate(make_view(), CANDIDATE_SUMMARY).contradiction_value \
        == "unknown"


def test_unknown_decision_raises_retryable():
    # M10-R3：首次违约触发一次 contract repair；两次仍非法才 fail-closed
    gate, port = make_gate([dict(GATE_OK, decision="MAYBE"),
                            dict(GATE_OK, decision="MAYBE")])
    with pytest.raises(ModelBadOutputError) as exc:
        gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert len(port.calls) == 2
    assert "BLOCKED_MODEL_BAD_OUTPUT" in str(exc.value)


def test_bad_output_raises_retryable():
    # M10-R3：缺 decision → 一次 repair → 仍缺 → fail-closed
    gate, port = make_gate([{"novelty": "high"}, {"novelty": "high"}])
    with pytest.raises(ModelBadOutputError) as exc:
        gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert len(port.calls) == 2
    assert "BLOCKED_MODEL_BAD_OUTPUT" in str(exc.value)


def test_interface_has_no_personal_preview_parameter():
    """Review §5：完整 Personal Retrieval 在 DEEP_READ 之后（M5），
    第二层不得依赖它。"""
    params = inspect.signature(DeepValueGate.evaluate).parameters
    assert "personal_preview" not in params
    assert list(params)[1] == "view" and list(params)[2] == "candidate"


def test_prompt_forbids_speculating_past_cognition():
    gate, port = make_gate([dict(GATE_OK)])
    gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    import json
    blob = json.dumps(port.calls[0][1], ensure_ascii=False)
    assert "unknown" in blob and "不要推测" in blob
