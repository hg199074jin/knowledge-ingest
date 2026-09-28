"""V3 M7: Cognitive Change Proposal——入口门禁与 provenance 继承。

用户冻结约束（M6 复核后追加）：
- Proposal 入口必须同时满足 quality_status == passed 且
  cognition_delta != NONE（P1 passed+NONE 也不生成；"没有 Proposal"
  不是验收失败）；
- Proposal 完整继承 provenance：source_revision → context_pack_id →
  card_id → evidence refs → related cognition refs；
- 初始状态 pending；同卡幂等（同 proposal_id）。
"""

import dataclasses

import pytest

from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.proposals import (
    InsightCard,
    build_cognition_proposal,
)

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "card_id": "abc123def456",
        "insight_source_id": "isv.test0000000001",
        "source_item_id": "tg_a:1",
        "source_revision": 1,
        "context_pack_id": "ctxpack.49bfeb7ce6c7",
        "card_path": "/tmp/insight/cards/2026/09/28/insight-abc123def456-jev.md",
        "quality_status": "passed",
        "cognition_delta": "ADD",
        "title": "Jev 范式映射为三级决策结构",
        "own_version": "规则→判定→生成三级结构，加层取决于调用量与经济性",
        "mechanism": "成本差两个数量级 → 高频小决策迁移到判定层",
        "challenge": "误判率数据缺失",
        "human_gate_recommendation": "EXPERIMENT",
        "evidence_refs": ("/x/B0001.md",),
        "related_cognition_refs": ("PC-002",),
        "related_cognitions": ({"record_id": "PC-002",
                                "text": "两级筛选架构",
                                "connection": "可扩展为三级"},),
        "topics": ("ai",)}
    base.update(overrides)
    return InsightSourceView(**base)


def make_card(**overrides) -> InsightCard:
    base = {
        "card_id": "abc123def456",
        "insight_source_id": "isv.test0000000001",
        "source_item_id": "tg_a:1",
        "source_revision": 1,
        "context_pack_id": "ctxpack.49bfeb7ce6c7",
        "card_path": "/tmp/insight/cards/2026/09/28/insight-abc123def456-jev.md",
        "quality_status": "passed",
        "cognition_delta": "ADD",
        "title": "Jev 范式映射为三级决策结构",
        "own_version": "规则→判定→生成三级结构，加层取决于调用量与经济性",
        "mechanism": "成本差两个数量级 → 高频小决策迁移到判定层",
        "challenge": "误判率数据缺失",
        "human_gate_recommendation": "EXPERIMENT",
        "evidence_refs": ("/x/B0001.md",),
        "related_cognition_refs": ("PC-002",),
        "related_cognitions": ({"record_id": "PC-002",
                                "text": "两级筛选架构",
                                "connection": "可扩展为三级"},),
        "topics": ("ai",)}
    base.update(overrides)
    return InsightCard(**base)


# ---------- 入口门禁 ----------

def test_passed_with_delta_creates_pending_proposal():
    card = make_card()
    proposal = build_cognition_proposal(card)
    assert proposal is not None
    assert proposal.card_id == "abc123def456"
    assert proposal.change_type == "ADD"
    assert proposal.gate_status == "pending"
    assert proposal.gate_decision is None


def test_passed_with_none_delta_creates_nothing():
    """P1 真实案例回归：passed + NONE ≠ Proposal。"""
    card = make_card(cognition_delta="NONE")
    assert build_cognition_proposal(card) is None


def test_needs_review_creates_nothing_even_with_delta():
    """P3 真实案例回归：needs_review 不准生成 Proposal。"""
    card = make_card(quality_status="needs_review")
    assert build_cognition_proposal(card) is None


def test_unknown_quality_status_rejected():
    with pytest.raises(ValueError):
        build_cognition_proposal(make_card(quality_status="draft"))


# ---------- provenance 继承 ----------

def test_proposal_preserves_full_provenance_chain():
    proposal = build_cognition_proposal(make_card())
    prov = proposal.provenance
    assert prov["source_revision"] == 1
    assert prov["context_pack_id"] == "ctxpack.49bfeb7ce6c7"
    assert prov["card_id"] == "abc123def456"
    assert list(prov["evidence_refs"]) == ["/x/B0001.md"]
    assert list(prov["related_cognition_refs"]) == ["PC-002"]
    assert prov["card_path"].endswith(".md")
    assert proposal.source_item_id == "tg_a:1"


def test_revise_proposal_carries_old_cognition_from_related_records():
    card = make_card(
        cognition_delta="REVISE",
        related_cognition_refs=("PC-002",),
        related_cognitions=({"record_id": "PC-002",
                             "text": "两级筛选架构",
                             "connection": "可扩展为三级"},))
    proposal = build_cognition_proposal(card)
    assert proposal.change_type == "REVISE"
    assert proposal.old_cognition[0]["text"] == "两级筛选架构"
    assert proposal.old_cognition[0]["connection"] == "可扩展为三级"
    assert proposal.proposed_cognition.startswith("规则→判定→生成")


def test_add_proposal_has_empty_old_cognition():
    proposal = build_cognition_proposal(make_card(cognition_delta="ADD"))
    assert proposal.old_cognition == ()


# ---------- 不可变 / 幂等 ----------

def test_proposal_is_frozen():
    proposal = build_cognition_proposal(make_card())
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.gate_status = "adopted"  # type: ignore[misc]


def test_same_card_yields_same_proposal_id():
    p1 = build_cognition_proposal(make_card())
    p2 = build_cognition_proposal(make_card())
    assert p1.proposal_id == p2.proposal_id


def test_different_delta_yields_different_id():
    p_add = build_cognition_proposal(make_card(cognition_delta="ADD"))
    p_rev = build_cognition_proposal(
        make_card(cognition_delta="REVISE",
                  related_cognitions=({"record_id": "PC-002",
                                       "text": "t", "connection": "c"},)))
    assert p_add.proposal_id != p_rev.proposal_id
