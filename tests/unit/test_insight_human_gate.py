"""V3 M7: Human Gate——五决策状态机、幂等/冲突、模型不可代行、provenance。

用户冻结约束（M6 复核后追加）：
- ADOPT → 才允许写 confirmed cognition（含完整 provenance）；
- EXPERIMENT → 只建 experiment stub；WATCH → 只进观察；
  ARCHIVE/REJECT → 关闭 proposal，不改认知；
- decision source 必须显式记录为真实用户操作（resolved_by）；
- 同决策幂等，异决策 GateConflictError；
- thinker/critic/cards 等模型侧模块不得暴露 resolve()。
"""

from pathlib import Path

import pytest

from knowledge_ingest.insight.human_gate import GateConflictError, InsightHumanGate
from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.proposals import build_cognition_proposal
from knowledge_ingest.insight.store import InsightStore

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


def make_card(proposal_id_hint="abc123def456",
              insight_source_id="isv.test0000000001"):
    from knowledge_ingest.insight.proposals import InsightCard
    return InsightCard(
        card_id=proposal_id_hint,
        insight_source_id=insight_source_id,
        source_item_id="tg_a:1",
        source_revision=1,
        context_pack_id="ctxpack.49bfeb7ce6c7",
        card_path="/tmp/card.md",
        quality_status="passed",
        cognition_delta="REVISE",
        title="Jev 范式映射为三级决策结构",
        own_version="规则→判定→生成三级结构，加层取决于调用量与经济性",
        mechanism="成本差两个数量级",
        challenge="误判率数据缺失",
        human_gate_recommendation="EXPERIMENT",
        evidence_refs=("/x/B0001.md",),
        related_cognition_refs=("PC-002",),
        related_cognitions=({"record_id": "PC-002", "text": "两级筛选架构",
                             "connection": "可扩展为三级"},),
        topics=("ai",))


def make_gate(tmp_path: Path, decision="ADOPT"):
    store = InsightStore(tmp_path / "insight" / "state.db")
    view = make_view()
    sid = store.register_source_view(view)
    proposal = build_cognition_proposal(make_card(insight_source_id=sid))
    store.save_proposal(proposal)
    gate = InsightHumanGate(store, insight_root=tmp_path / "insight")
    resolution = gate.resolve(proposal.proposal_id, decision,
                              resolved_by="user")
    return store, gate, proposal, resolution


# ---------- ADOPT ----------

def test_adopt_writes_confirmed_cognition_with_provenance(tmp_path):
    store, _gate, proposal, resolution = make_gate(tmp_path, "ADOPT")
    assert resolution.gate_status == "adopted"
    projection = (tmp_path / "insight" / "knowledge" / "confirmed" /
                  f"cognition-{proposal.proposal_id}.md")
    assert projection.is_file()
    text = projection.read_text(encoding="utf-8")
    # provenance 完整：当初为什么形成这条认知全程可回溯
    for marker in ("ctxpack.49bfeb7ce6c7", proposal.card_id,
                   "tg_a:1", "两级筛选架构", "REVISE",
                   "source_revision: 1"):
        assert marker in text, marker
    rows = store.list_approved_cognition()
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == proposal.proposal_id
    assert "两级" in rows[0]["cognition"] or "三级" in rows[0]["cognition"]
    import json
    prov = json.loads(rows[0]["provenance_json"])
    assert prov["context_pack_id"] == "ctxpack.49bfeb7ce6c7"
    assert prov["source_revision"] == 1
    assert prov["card_id"] == proposal.card_id
    # gate 状态落库
    row = store.get_proposal(proposal.proposal_id)
    assert row["gate_status"] == "adopted"
    assert row["gate_decision"] == "ADOPT"
    assert row["gate_decision_source"] == "user"
    assert row["gate_resolved_at"] is not None


# ---------- idempotence / conflict ----------

def test_same_decision_twice_idempotent(tmp_path):
    store, gate, proposal, _first = make_gate(tmp_path, "ADOPT")
    second = gate.resolve(proposal.proposal_id, "ADOPT",
                          resolved_by="user")
    assert second.gate_status == "adopted"
    assert second.already_resolved is True
    # 没有重复写认知
    assert len(store.list_approved_cognition()) == 1


def test_different_decision_raises_conflict(tmp_path):
    _store, gate, proposal, _ = make_gate(tmp_path, "ADOPT")
    with pytest.raises(GateConflictError):
        gate.resolve(proposal.proposal_id, "REJECT", resolved_by="user")


# ---------- 其余四决策 ----------

def test_experiment_creates_stub_no_confirmed_cognition(tmp_path):
    store, _gate, proposal, resolution = make_gate(tmp_path, "EXPERIMENT")
    assert resolution.gate_status == "experimented"
    stub = (tmp_path / "insight" / "experiments" /
            f"experiment-{proposal.proposal_id}.md")
    assert stub.is_file()
    assert "两级筛选架构" in stub.read_text(encoding="utf-8")
    assert store.list_approved_cognition() == []


def test_watch_records_signal_no_cognition(tmp_path):
    store, _gate, proposal, resolution = make_gate(tmp_path, "WATCH")
    assert resolution.gate_status == "watched"
    signals = store.list_watch_signals_for_source(
        proposal.insight_source_id)
    assert len(signals) == 1
    assert store.list_approved_cognition() == []


@pytest.mark.parametrize("decision", ["ARCHIVE", "REJECT"])
def test_archive_reject_close_without_mutation(tmp_path, decision):
    store, _gate, proposal, _resolution = make_gate(tmp_path, decision)
    row = store.get_proposal(proposal.proposal_id)
    assert row["gate_status"] == {"ARCHIVE": "archived", "REJECT": "rejected"}[decision]
    assert store.list_approved_cognition() == []
    assert not (tmp_path / "insight" / "knowledge").exists()


# ---------- 入口校验 ----------

def test_resolve_unknown_proposal_raises(tmp_path):
    store = InsightStore(tmp_path / "insight" / "state.db")
    gate = InsightHumanGate(store, insight_root=tmp_path / "insight")
    with pytest.raises(ValueError):
        gate.resolve("cp.nope", "ADOPT", resolved_by="user")


def test_resolved_by_must_be_recorded(tmp_path):
    store, gate, proposal, _ = make_gate(tmp_path, "ADOPT")
    row = store.get_proposal(proposal.proposal_id)
    assert row["gate_decision_source"] == "user"
    resolution = gate.resolve(proposal.proposal_id, "ADOPT",
                              resolved_by="user")
    assert resolution.already_resolved is True
    assert resolution.resolved_by == "user"
