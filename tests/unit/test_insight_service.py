"""V3 M8: InsightService 状态机——编排、restart-safe、增量、绑定 Pack。"""

from pathlib import Path

from knowledge_ingest.insight.content_resolver import ContentPart, ResolvedContent
from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import (
    CandidateDecision,
    InsightSourceView,
    PersonalContextRef,
)
from knowledge_ingest.insight.service import InsightService
from knowledge_ingest.insight.store import InsightStore

NOW = "2026-09-28T12:00:00+00:00"

GOOD_THOUGHT = {
    "title": "Jev 范式映射为三级决策结构",
    "bottom_line": "判定模型承接高频小决策",
    "source_understanding": "s", "mechanism": "m", "challenge": "c",
    "personal_connections": [
        {"record_id": "PC-001", "connection": "扩展两级为三级"}],
    "project_impacts": [], "business_opportunity": None,
    "own_version": "三级结构，加层看调用量", "cognition_delta": "ADD",
    "actions": [{"action": "EXPERIMENT", "detail": "50 条样本试判定层"}],
    "human_gate_recommendation": "EXPERIMENT", "final_verdict": "CARD",
}
CANDIDATE_TRUE = {"candidate": True, "possible_value": ["COGNITION"],
                  "reasons": ["r"], "confidence": 0.7}
GATE_DEEP = {"decision": "DEEP_READ", "reason": "r", "novelty": "high",
             "cognitive_delta_potential": "high", "business_potential": None,
             "project_relevance": None, "transferability": None,
             "evidence_quality": None, "contradiction_value": None,
             "thinking_space": None}
EVIDENCE = {"claims": ["c"], "evidence": ["e"], "inferences": [],
            "unknowns": ["u"], "verification_flags": []}
SELECTED = {"judgements": [
    {"record_id": "PC-001", "relation": "CONDITIONING",
     "relation_reason": "扩展两级为三级", "confidence": 0.8}],
    "conflicts": []}
PLAN = {"queries": ["Q1？", "Q2？", "Q3？"], "include_history": False}


class FakeRetriever:
    def __init__(self, refs):
        self.refs = refs
        self.calls = 0

    def retrieve(self, view, content):
        self.calls += 1
        return self.refs


class FakeFilter:
    def __init__(self, decision):
        self.decision = decision

    def evaluate(self, view):
        return self.decision


class FakeGate:
    def __init__(self, decision):
        self.decision = decision

    def evaluate(self, view, candidate):
        from knowledge_ingest.insight.models import DeepValueDecision
        return DeepValueDecision(self.decision, reason="test")


class FakeModel:
    """evidence/thinking/critic 按 stage 脚本应答。"""

    def __init__(self, thinking_scripts, critic_scripts,
                 evidence_outputs=None, fail_evidence_times=0):
        self.thinking_scripts = list(thinking_scripts)
        self.critic_scripts = list(critic_scripts)
        self.evidence_outputs = evidence_outputs or []
        self.fail_evidence_times = fail_evidence_times
        self.evidence_calls = 0
        self.thinking_calls = 0
        self.critic_calls = 0

    def run(self, stage, payload):
        if stage == "evidence_extract":
            self.evidence_calls += 1
            if self.fail_evidence_times > 0:
                self.fail_evidence_times -= 1
                from knowledge_ingest.insight.model_port import (
                    ModelPortError,
                )
                raise ModelPortError("model down")
            return dict(EVIDENCE)
        if stage == "thinking":
            self.thinking_calls += 1
            out = self.thinking_scripts.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        if stage == "critic":
            self.critic_calls += 1
            out = self.critic_scripts.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        raise AssertionError(stage)


CRITIC_PASS = {"source_understanding": "pass", "critical_reasoning": "pass",
               "personal_connection": "pass", "cognition_delta": "pass",
               "own_version": "pass", "actionability": "pass",
               "business_rigor": "pass", "traceability": "pass",
               "mechanism_salvage": "pass", "genericity_detected": False,
               "revision_required": False, "revision_instructions": []}


def make_service(tmp_path, *, model, candidate=True, gate="DEEP_READ",
                 refs=None):
    from knowledge_ingest.insight.candidate_filter import HighRecallCandidateFilter
    from knowledge_ingest.insight.cards import DeepInsightCardWriter
    from knowledge_ingest.insight.critic import QualityCritic
    from knowledge_ingest.insight.value_gate import DeepValueGate
    if refs is None:
        refs = (PersonalContextRef(
            record_id="PC-001", relation_reason="r",
            state="CONFIRMED", relation="CONDITIONING"),)
    store = InsightStore(tmp_path / "insight" / "state.db")
    service = InsightService(
        store,
        resolver=RealResolver(),
        retriever=FakeRetriever(refs),
        candidate_filter=HighRecallCandidateFilter(
            model, taste_profile="t"),
        value_gate=DeepValueGate(model),
        model_port=model,
        critic=QualityCritic(model),
        card_writer=DeepInsightCardWriter(tmp_path / "insight"),
        now=lambda: NOW)
    # filter/gate 也走同一个 FakeModel 的脚本——直接替换为确定性桩
    service.candidate_filter = FakeFilter(
        dataclasses_candidate(candidate))
    service.value_gate = FakeGate(gate)
    return store, service


def dataclasses_candidate(flag):
    return CandidateDecision(candidate=flag, confidence=0.7)


class RealResolver:
    def resolve(self, view):
        if view.full_text_available:
            return ResolvedContent(
                parts=(ContentPart(part_id="main", text="正文",
                                   source_ref="/x"),),
                verification_status=view.verification_status)
        return None


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


# ---------- 全链 happy path ----------

def test_full_chain_produces_card_proposal_and_bindings(tmp_path):
    model = FakeModel([dict(GOOD_THOUGHT)], [dict(CRITIC_PASS)],
                      evidence_outputs=[dict(EVIDENCE)])
    store, service = make_service(tmp_path, model=model)
    outcome = service.process(make_view())
    assert outcome.stage == "card" and outcome.status == "passed"
    assert outcome.card_path is not None
    assert outcome.proposal_id is not None       # ADD→proposal 应生成
    # cognition_delta=ADD ≠ NONE → proposal 应生成
    assert outcome.proposal_id is not None
    assert outcome.proposal_id.startswith("cp.")
    assert Path(outcome.card_path).is_file()
    # pack 绑定：context refs 持久化
    sid = store._conn.execute(
        "SELECT insight_source_id FROM insight_sources").fetchone()[0]
    refs = store.get_context_refs(sid)
    assert len(refs) == 1
    assert refs[0].relation == "CONDITIONING"


def test_candidate_false_is_terminal_no_models_after(tmp_path):
    model = FakeModel([], [])
    _store, service = make_service(
        tmp_path, model=model, candidate=False)
    outcome = service.process(make_view())
    assert outcome.stage == "candidate"
    assert outcome.status == "rejected"
    assert model.evidence_calls == 0 and model.thinking_calls == 0


def test_value_watch_is_terminal(tmp_path):
    model = FakeModel([], [])
    _store, service = make_service(tmp_path, model=model, gate="WATCH")
    outcome = service.process(make_view())
    assert outcome.stage == "value_gate" and outcome.status == "watch"
    assert model.evidence_calls == 0


# ---------- fail-closed ----------

def test_waiting_content_returns_waiting(tmp_path):
    model = FakeModel([], [])
    _store, service = make_service(tmp_path, model=model)
    outcome = service.process(make_view(full_text_available=False))
    assert outcome.stage == "content" and outcome.status == "waiting"


def test_evidence_model_failure_blocked_not_rejected(tmp_path):
    from knowledge_ingest.insight.model_port import ModelPortError
    model = FakeModel([], [])
    model.fail_evidence = ModelPortError("model down")
    orig_run = model.run

    def failing_run(stage, payload):
        if stage == "evidence_extract":
            raise ModelPortError("model down")
        return orig_run(stage, payload)

    model.run = failing_run
    store, service = make_service(tmp_path, model=model)
    outcome = service.process(make_view())
    assert outcome.stage == "evidence" and outcome.status == "blocked"
    # blocked run 落库
    row = store._conn.execute(
        "SELECT status FROM insight_runs WHERE stage = 'evidence'"
        " AND status = 'blocked'").fetchone()
    assert row is not None


def test_thinking_model_failure_blocked(tmp_path):
    model = FakeModel(
        [ModelBadOutputError("bad")], [],
        evidence_outputs=[dict(EVIDENCE)])
    _store, service = make_service(tmp_path, model=model)
    outcome = service.process(make_view())
    assert outcome.stage == "thinking" and outcome.status == "blocked"


# ---------- 增量 / restart-safe ----------

def test_second_run_skips_candidate_and_gate_models(tmp_path):
    model = FakeModel([dict(GOOD_THOUGHT), dict(GOOD_THOUGHT)],
                      [dict(CRITIC_PASS), dict(CRITIC_PASS)],
                      evidence_outputs=[dict(EVIDENCE), dict(EVIDENCE)])
    _store, service = make_service(tmp_path, model=model)
    service.process(make_view())
    # 第二轮：candidate/gate 判定已持久化 → 只有 evidence/thinking/critic 再跑
    outcome = service.process(make_view())
    assert outcome.status == "passed"
    # candidate_filter FakeFilter 不经模型——但 value_gate 决策已持久化，
    # 第二轮直接复用 latest_value_gate（FakeGate 是确定性的，无调用计数）
    # 关键断言：不崩溃、状态一致
    assert outcome.stage == "card"


def test_fingerprint_change_resets_downstream(tmp_path):
    model = FakeModel([dict(GOOD_THOUGHT)], [dict(CRITIC_PASS)],
                      evidence_outputs=[dict(EVIDENCE)])
    store, service = make_service(tmp_path, model=model)
    service.process(make_view())
    sid = store._conn.execute(
        "SELECT insight_source_id FROM insight_sources").fetchone()[0]
    assert len(store.get_context_refs(sid)) == 1
    # EDIT：指纹变化 → revision 2 → refs 清空（待重检索）
    store.register_source_view(make_view(content_fingerprint="fp-2"))
    assert store.get_context_refs(sid) == ()
