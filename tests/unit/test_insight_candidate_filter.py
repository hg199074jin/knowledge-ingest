"""V3 M4: HighRecallCandidateFilter — 高召回第一层契约。

冻结语义：
- 输出契约 {candidate, possible_value, reasons, confidence, trend_key}；
- 低置信 + 可能认知价值 → 仍 pass（Recall 优先于 Precision）；
- 明显无价值可返回 candidate=false（模型判断，非本层硬编码）；
- 模型坏输出/未知枚举 → 类型化可重试错误，绝不伪造 candidate=false；
- 本层无 store 依赖、不产生任何"value-SKIP 学习规则"。
"""

import dataclasses

import pytest

from knowledge_ingest.insight.candidate_filter import (
    HIGH_RECALL_DIRECTIVE,
    HighRecallCandidateFilter,
)
from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import InsightSourceView

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "Jev 判定模型", "visible_text": "正文",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


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


TASTE = "AI/Agent、审计财税、商业副业、认知成长、民宿本地生活"


def make_filter(script, taste=TASTE):
    port = FakePort(script)
    return HighRecallCandidateFilter(port, taste_profile=taste), port


GOOD = {"candidate": True, "possible_value": ["COGNITION"],
        "reasons": ["新判定层范式"], "confidence": 0.62,
        "trend_key": "jev-decision-layer"}


def test_known_value_types_parse_to_decision():
    flt, port = make_filter([dict(GOOD)])
    decision = flt.evaluate(make_view())
    assert decision.candidate is True
    assert decision.possible_value == ("COGNITION",)
    assert decision.confidence == 0.62
    assert decision.trend_key == "jev-decision-layer"
    stage, payload = port.calls[0]
    assert stage == "candidate_filter"
    assert payload["visible_text"] == "正文"
    assert payload["kind"] == "text"


def test_low_confidence_with_plausible_value_still_passes():
    flt, _ = make_filter([{**GOOD, "confidence": 0.3}])
    decision = flt.evaluate(make_view())
    assert decision.candidate is True       # 高召回：低置信不拒绝


def test_obvious_non_value_can_return_false():
    flt, _ = make_filter([{"candidate": False, "possible_value": [],
                           "reasons": ["纯广告"], "confidence": 0.9}])
    decision = flt.evaluate(make_view())
    assert decision.candidate is False


def test_malformed_output_raises_retryable_not_false():
    flt, _ = make_filter([{"no_candidate_field": True}])
    with pytest.raises(ModelBadOutputError):
        flt.evaluate(make_view())


def test_unknown_value_type_raises_not_coerced():
    flt, _ = make_filter([{**GOOD, "possible_value": ["MAGIC"]}])
    with pytest.raises(ModelBadOutputError):
        flt.evaluate(make_view())


def test_candidate_must_be_bool():
    flt, _ = make_filter([{**GOOD, "candidate": "yes"}])
    with pytest.raises(ModelBadOutputError):
        flt.evaluate(make_view())


def test_prompt_embeds_high_recall_directive_and_taste_profile():
    flt, port = make_filter([dict(GOOD)])
    flt.evaluate(make_view())
    # prompt 随 payload 传给模型端口（payload 携带指令上下文）
    # 高召回指令与画像必须出现在发送内容中
    sent = port.calls[0][1]
    assert HIGH_RECALL_DIRECTIVE[:12] in json_blob(sent)
    assert TASTE[:6] in json_blob(sent)


def json_blob(payload) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)


def test_filter_has_no_store_and_no_rule_learning_surface():
    """禁止 value-SKIP 自动学习：过滤器不持有 store，接口上不存在
    写规则的方法（设计 §7.6）。"""
    flt, _ = make_filter([dict(GOOD)])
    assert not hasattr(flt, "store")
    assert not any("learn" in f or "skip_rule" in f
                   for f in dir(flt) if not f.startswith("__"))


def test_filter_is_immutable_config():
    flt, _ = make_filter([dict(GOOD)])
    with pytest.raises(dataclasses.FrozenInstanceError):
        flt.taste_profile = "改画像"      # type: ignore[misc]
