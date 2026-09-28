"""V3 M6: Personal Thinking Engine——结构化输出契约与五阶段协议。

冻结语义（实施方案 Task 6 Step 3/4）：
- 输出为结构化契约（非纯 Markdown）：bottom_line / source_understanding /
  mechanism / challenge / personal_connections[] / project_impacts[] /
  business_opportunity|null / own_version / cognition_delta / actions[] /
  human_gate_recommendation / final_verdict；
- actions ≤3，类型 ∈ IMMEDIATE|EXPERIMENT|WATCH|NONE；
- cognition_delta ∈ ADD|REINFORCE|REVISE|OVERTURN|NONE；
- business_opportunity 必须拆 market_opportunity / personal_opportunity；
- 深思后允许 final_verdict = ARCHIVE | REJECT（即使已过 Value Gate）；
- prompt 保持 Understand→Challenge→Connect→Reconstruct→Decide 顺序；
- 坏输出/未知枚举 → 类型化可重试错误。
"""

import pytest

from knowledge_ingest.insight.content_resolver import (
    ContentPart,
)
from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import (
    EvidencePack,
    PersonalContextRef,
)
from knowledge_ingest.insight.thinker import (
    FINAL_VERDICT_TYPES,
    PersonalThinkingEngine,
)

NOW = "2026-09-28T12:00:00+00:00"

REF = PersonalContextRef(record_id="PC-001",
                         relation_reason="可能新增判定层", state="CONFIRMED",
                         kind="cognition", text="两级筛选架构")


def make_pack(**overrides) -> EvidencePack:
    base = {
        "source_claims": ("判定模型可承担部分决策",),
        "source_evidence": ("tev1 17 美元 25 分钟微调",),
        "source_inferences": ("高频小决策应迁移到判定层",),
        "unknown_variables": ("实际误判率",),
        "personal_context": (REF,),
        "verification_flags": (),
        "source_parts": (ContentPart(part_id="main", text="原文全文",
                                     source_ref="/x"),)}
    base.update(overrides)
    return EvidencePack(**base)


THOUGHT = {
    "bottom_line": "Jev 范式在本管道的映射是第三层判定",
    "source_understanding": "作者主张判定式模型承接 Agent 高频小决策",
    "mechanism": "成本差两个数量级 → 决策迁移",
    "challenge": "误判率数据缺失；DomA 案例显示纯 LLM 仍最稳",
    "personal_connections": [
        {"record_id": "PC-001", "connection": "两级筛选架构可扩展为三级"}],
    "project_impacts": [
        {"project": "knowledge-ingest", "impact": "budget_denied 下降"}],
    "business_opportunity": None,
    "own_version": "规则→判定→生成的三级结构，是否加中间层取决于调用量与经济性",
    "cognition_delta": "REVISE",
    "actions": [{"action": "EXPERIMENT", "detail": "拿 50 条积压样本试判定层"}],
    "human_gate_recommendation": "EXPERIMENT",
    "final_verdict": "CARD",
}


def make_engine(outputs):
    class FakePort:
        def __init__(self):
            self.calls = []

        def run(self, stage, payload):
            self.calls.append((stage, payload))
            out = outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

    port = FakePort()
    return PersonalThinkingEngine(port), port


# ---------- happy path ----------

def test_valid_thought_parses_to_structured_dict():
    engine, port = make_engine([dict(THOUGHT)])
    thought = engine.think(make_pack())
    assert thought["cognition_delta"] == "REVISE"
    assert thought["final_verdict"] == "CARD"
    stage, payload = port.calls[0]
    assert stage == "thinking"
    # 五阶段协议顺序入 prompt；Evidence before Advice
    blob = str(payload)
    for phase in ("Understand", "Challenge", "Connect", "Reconstruct",
                  "Decide"):
        assert phase in blob
    assert "Evidence" in blob


def test_pack_content_sent_including_personal_context():
    engine, port = make_engine([dict(THOUGHT)])
    engine.think(make_pack())
    payload = port.calls[0][1]
    assert payload["pack"]["source_claims"] == ["判定模型可承担部分决策"]
    assert payload["pack"]["personal_context"][0]["record_id"] == "PC-001"
    assert payload["pack"]["source_parts"][0]["text"] == "原文全文"


# ---------- frozen enum enforcement ----------

def test_max_three_actions_enforced():
    bad = dict(THOUGHT, actions=[{"action": "WATCH", "detail": str(i)}
                                 for i in range(4)])
    engine, _ = make_engine([bad])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())


def test_unknown_action_type_rejected():
    bad = dict(THOUGHT, actions=[{"action": "SOMEDAY", "detail": "x"}])
    engine, _ = make_engine([bad])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())


def test_unknown_cognition_delta_rejected():
    engine, _ = make_engine([dict(THOUGHT, cognition_delta="UPGRADED")])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())


def test_unknown_final_verdict_rejected():
    engine, _ = make_engine([dict(THOUGHT, final_verdict="MAYBE")])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())


def test_final_verdict_archive_is_legal_even_after_deep_read():
    engine, _ = make_engine([dict(THOUGHT, final_verdict="ARCHIVE",
                                  actions=[])])
    thought = engine.think(make_pack())
    assert thought["final_verdict"] == "ARCHIVE"


# ---------- business opportunity lens ----------

def test_business_opportunity_requires_both_sides():
    bad = dict(THOUGHT, business_opportunity={"market_opportunity": "成立"})
    engine, _ = make_engine([bad])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())
    ok = dict(THOUGHT, business_opportunity={
        "market_opportunity": "判定层工具需求真实",
        "personal_opportunity": "用户有分类管道做试验场"})
    engine2, _ = make_engine([ok])
    assert engine2.think(make_pack())["business_opportunity"][
        "personal_opportunity"].startswith("用户有")


def test_business_opportunity_null_is_legal():
    engine, _ = make_engine([dict(THOUGHT)])
    assert engine.think(make_pack())["business_opportunity"] is None


# ---------- personal connections ----------

def test_connection_without_record_id_or_reason_rejected():
    bad = dict(THOUGHT, personal_connections=[{"connection": "相关"}])
    engine, _ = make_engine([bad])
    with pytest.raises(ModelBadOutputError):
        engine.think(make_pack())
    bad2 = dict(THOUGHT, personal_connections=[
        {"record_id": "PC-001", "connection": ""}])
    engine2, _ = make_engine([bad2])
    with pytest.raises(ModelBadOutputError):
        engine2.think(make_pack())


# ---------- revise ----------

def test_revise_includes_draft_and_instructions():
    engine, port = make_engine([dict(THOUGHT), dict(THOUGHT)])
    draft = engine.think(make_pack())
    engine.revise(make_pack(), draft,
                  ["personal_connections 缺少真实因果，补机制级关联"])
    _, payload = port.calls[1]
    assert "personal_connections 缺少真实因果" in str(payload)
    assert "Jev 范式" in str(payload)          # 上一稿随附


def test_final_verdict_types_frozen():
    assert FINAL_VERDICT_TYPES == ("CARD", "ARCHIVE", "REJECT")
