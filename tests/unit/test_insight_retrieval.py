"""V3 M5: PersonalReasoningRetriever — 判断命题检索与硬上限。

冻结语义（实施方案 Task 5 + Review Focus #3/#4/#5）：
- Planner 产出 3–5 个"判断问题"（非主题关键词）；>5 截断、<3 坏输出；
- 默认只检索 CONFIRMED/TENTATIVE/EXPERIMENTING；SUPERSEDED/REJECTED/
  ARCHIVED 仅当 planner 显式 include_history=true；
- Selector 可选零条（合法）；每条入选必须给 relation_reason；
- 硬上限：planner queries 5 / external candidates 20 / selected 12；
- 冲突不静默消除：selector 标记冲突对 → 双方 ref 带 conflict_with。
"""

import dataclasses

import pytest

from knowledge_ingest.insight.content_resolver import ContentPart, ResolvedContent
from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import (
    InsightSourceView,
    PersonalContextRef,
    PersonalKnowledgeRecord,
)
from knowledge_ingest.insight.retrieval import PersonalReasoningRetriever

NOW = "2026-09-28T12:00:00+00:00"


def make_view() -> InsightSourceView:
    return InsightSourceView(
        provider="telegram", source_item_id="tg_a:1", content_kind="text",
        title="Jev 判定模型", visible_text="正文",
        materialized_path=None, full_text_available=True,
        verification_status="source_only", content_fingerprint="fp-1",
        captured_at=NOW)


CONTENT = ResolvedContent(
    parts=(ContentPart(part_id="main", text="正文", source_ref="/x"),),
    verification_status="source_only")


def rec(record_id, text="认知", state="CONFIRMED", kind="cognition"):
    return PersonalKnowledgeRecord(record_id=record_id, kind=kind,
                                   state=state, text=text,
                                   source_ref=f"src://{record_id}",
                                   updated_at=NOW)


class FakeKnowledgePort:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def search(self, queries, *, statuses, limit):
        self.calls.append({"queries": list(queries), "statuses": list(statuses),
                           "limit": limit})
        return self.records[:limit]


class FakeModelPort:
    def __init__(self, planner_out, selector_out):
        self.planner_out = planner_out
        self.selector_out = selector_out
        self.calls = []

    def run(self, stage, payload):
        self.calls.append((stage, payload))
        if stage == "retrieval_query_plan":
            return self.planner_out
        if stage == "retrieval_select":
            return self.selector_out
        raise AssertionError(f"unexpected stage {stage}")


PLAN = {"queries": ["Q1 用户如何判断决策层架构？", "Q2 有没有放弃过类似方向？",
                    "Q3 当前哪些项目会被影响？"],
        "include_history": False}

SELECTED = {"selected": [
    {"record_id": "PC-001", "relation_reason": "可能新增判定层，改变现有两级结构",
     "state": "CONFIRMED"},
    {"record_id": "PC-002", "relation_reason": "此前的否决理由可能被新成本数据推翻",
     "state": "REJECTED"},
], "conflicts": []}


def make_retriever(records=None, planner=PLAN, selector=SELECTED, **kwargs):
    if records is None:
        records = [rec("PC-001", "三级决策结构"),
                   rec("PC-002", "曾放弃类似方向", state="REJECTED")]
    knowledge = FakeKnowledgePort(list(records))
    model = FakeModelPort(planner, selector)
    retriever = PersonalReasoningRetriever(model, knowledge, **kwargs)
    return retriever, model, knowledge


# ---------- planner ----------

def test_planner_questions_sent_to_knowledge_port_with_default_statuses():
    retriever, _model, knowledge = make_retriever()   # 默认记录含 PC-001/002
    refs = retriever.retrieve(make_view(), CONTENT)
    assert refs
    call = knowledge.calls[0]
    assert call["queries"] == PLAN["queries"]
    assert call["statuses"] == ["CONFIRMED", "TENTATIVE", "EXPERIMENTING"]
    assert call["limit"] == 20


def test_planner_more_than_five_queries_clamped():
    planner = {"queries": [f"Q{i}？" for i in range(8)],
               "include_history": False}
    retriever, model, _k = make_retriever(planner=planner)
    retriever.retrieve(make_view(), CONTENT)
    # 钳制发生在传给 selector 之前：selector 调用可见 ≤5 个问题
    selector_stage, selector_payload = model.calls[1]
    assert selector_stage == "retrieval_select"
    assert len(selector_payload["queries"]) == 5


def test_planner_fewer_than_three_questions_is_bad_output():
    planner = {"queries": ["只有一个"], "include_history": False}
    retriever, _m, _k = make_retriever(planner=planner)
    with pytest.raises(ModelBadOutputError):
        retriever.retrieve(make_view(), CONTENT)


def test_include_history_adds_superseded_states():
    planner = {"queries": PLAN["queries"], "include_history": True}
    retriever, _m, knowledge = make_retriever(planner=planner)
    retriever.retrieve(make_view(), CONTENT)
    statuses = knowledge.calls[0]["statuses"]
    assert "SUPERSEDED" in statuses and "REJECTED" in statuses
    assert "ARCHIVED" in statuses


# ---------- selector ----------

def test_selected_refs_carry_record_payload_and_reason():
    retriever, _m, _k = make_retriever(
        records=[rec("PC-001", "三级决策结构"), rec("PC-002", "曾放弃 jev 方向",
                                                    state="REJECTED")])
    refs = retriever.retrieve(make_view(), CONTENT)
    by_id = {r.record_id: r for r in refs}
    assert by_id["PC-001"].relation_reason.startswith("可能新增判定层")
    assert by_id["PC-001"].text == "三级决策结构"
    assert by_id["PC-001"].kind == "cognition"
    assert by_id["PC-002"].state == "REJECTED"   # include_history 才可能入选


def test_selector_may_select_zero():
    retriever, _m, _k = make_retriever(selector={"selected": []})
    refs = retriever.retrieve(make_view(), CONTENT)
    assert refs == ()                            # 合法空结果


def test_selected_without_relation_reason_is_malformed():
    selector = {"selected": [{"record_id": "PC-001", "relation_reason": ""}]}
    retriever, _m, _k = make_retriever(records=[rec("PC-001")],
                                       selector=selector)
    with pytest.raises(ModelBadOutputError):
        retriever.retrieve(make_view(), CONTENT)


def test_selection_of_unknown_record_rejected():
    retriever, _m, _k = make_retriever(records=[])   # selector 选了不存在的
    with pytest.raises(ModelBadOutputError):
        retriever.retrieve(make_view(), CONTENT)


def test_max_selected_enforced(tmp_path=None):
    records = [rec(f"PC-{i:03d}") for i in range(15)]
    selector = {"selected": [{"record_id": r.record_id,
                              "relation_reason": f"理由{i}"}
                             for i, r in enumerate(records)]}
    retriever, _m, _k = make_retriever(records=records, selector=selector)
    refs = retriever.retrieve(make_view(), CONTENT)
    assert len(refs) == 12                       # 硬上限


def test_hard_caps_frozen_in_signature_defaults():
    import inspect
    sig = inspect.signature(PersonalReasoningRetriever.__init__)
    assert sig.parameters["max_queries"].default == 5
    assert sig.parameters["max_candidates"].default == 20
    assert sig.parameters["max_selected"].default == 12


# ---------- conflicts ----------

def test_conflicting_cognitions_both_survive_with_marker():
    selector = {"selected": [
        {"record_id": "PC-001", "relation_reason": "支持轻量判定层",
         "state": "CONFIRMED"},
        {"record_id": "PC-002", "relation_reason": "坚持单一生成模型",
         "state": "CONFIRMED"}],
        "conflicts": [{"record_ids": ["PC-001", "PC-002"],
                       "reason": "对判定层必要性的判断相反"}]}
    retriever, _m, _k = make_retriever(
        records=[rec("PC-001", "要轻量判定层"),
                 rec("PC-002", "只要生成模型")],
        selector=selector)
    refs = retriever.retrieve(make_view(), CONTENT)
    by_id = {r.record_id: r for r in refs}
    assert by_id["PC-001"].conflict_with == ("PC-002",)
    assert by_id["PC-002"].conflict_with == ("PC-001",)
    assert len(refs) == 2                        # 冲突双方都保留


# ---------- ref immutability ----------

def test_context_ref_is_frozen():
    ref = PersonalContextRef(record_id="PC-1", relation_reason="r",
                             state="CONFIRMED")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.relation_reason = "改"
