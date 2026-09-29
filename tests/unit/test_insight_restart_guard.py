"""V3 M10-R2: 昂贵阶段 restart guard + Thinking 产物持久化（P0 次生）。

M10 Batch-2 暴露：evidence 有 `_stage_done` 守卫与产物持久化，
thinking/critic 没有——card write 崩溃后重启会重新支付 thinking+critic
（生产实测 runs run2+run7 同条目两次，104–226s/次）。

冻结语义（M10-R R2）：已成功完成且 input/source_revision 未变化的
昂贵 stage，重启后不得再次调用模型；card write 崩溃 → 重启必须
从已持久化产物直接续写卡片。
"""

import json

from knowledge_ingest.insight.critic import outcome_from_json, outcome_to_json
from knowledge_ingest.insight.models import CandidateDecision, InsightSourceView, PersonalContextRef
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
    "own_version": "三级结构", "cognition_delta": "ADD",
    "actions": [], "human_gate_recommendation": "EXPERIMENT",
    "final_verdict": "CARD",
}
EVIDENCE = {"claims": ["c"], "evidence": ["e"], "inferences": [],
            "unknowns": [], "verification_flags": []}
CRITIC_PASS = {"source_understanding": "pass", "critical_reasoning": "pass",
               "personal_connection": "pass", "cognition_delta": "pass",
               "own_version": "pass", "actionability": "pass",
               "business_rigor": "pass", "traceability": "pass",
               "mechanism_salvage": "pass", "genericity_detected": False,
               "revision_required": False, "revision_instructions": []}
CRITIC_STRICT = dict(CRITIC_PASS, revision_required=True,
                     revision_instructions=["补 own_version 锚点"])


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


class CountingModel:
    def __init__(self, critic=CRITIC_PASS):
        self.critic_script = dict(critic)
        self.evidence_calls = 0
        self.thinking_calls = 0
        self.critic_calls = 0

    def run(self, stage, payload):
        if stage == "evidence_extract":
            self.evidence_calls += 1
            return dict(EVIDENCE)
        if stage == "thinking":
            self.thinking_calls += 1
            return dict(GOOD_THOUGHT)
        if stage == "critic":
            self.critic_calls += 1
            return dict(self.critic_script)
        raise AssertionError(stage)


class _Filter:
    def evaluate(self, view):
        return CandidateDecision(candidate=True, confidence=0.7)


class _Gate:
    def evaluate(self, view, candidate):
        from knowledge_ingest.insight.models import DeepValueDecision
        return DeepValueDecision("DEEP_READ", reason="r")


class _Retriever:
    def __init__(self):
        self.calls = 0

    def retrieve(self, view, content):
        self.calls += 1
        return (PersonalContextRef(
            record_id="PC-001", relation_reason="r", state="CONFIRMED",
            relation="CONDITIONING"),)


class _Resolver:
    def resolve(self, view):
        from knowledge_ingest.insight.content_resolver import ContentPart, ResolvedContent
        return ResolvedContent(
            parts=(ContentPart(part_id="main", text="正文",
                               source_ref="/x"),),
            verification_status=view.verification_status)


def make_service(tmp_path, model, *, card_writer=None):
    from knowledge_ingest.insight.candidate_filter import HighRecallCandidateFilter
    from knowledge_ingest.insight.cards import DeepInsightCardWriter
    from knowledge_ingest.insight.critic import QualityCritic
    from knowledge_ingest.insight.value_gate import DeepValueGate
    store = InsightStore(tmp_path / "insight" / "state.db")
    retriever = _Retriever()
    service = InsightService(
        store, resolver=_Resolver(), retriever=retriever,
        candidate_filter=HighRecallCandidateFilter(model, taste_profile="t"),
        value_gate=DeepValueGate(model), model_port=model,
        critic=QualityCritic(model),
        card_writer=card_writer or DeepInsightCardWriter(
            tmp_path / "insight"),
        now=lambda: NOW)
    service.candidate_filter = _Filter()
    service.value_gate = _Gate()
    return store, service, retriever


class CrashingCardWriter:
    """首次 write 崩溃（模拟 M10 Batch-2 的卡片落盘前崩溃）。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def write(self, **kwargs):
        self.calls += 1
        raise RuntimeError("simulated card-write crash")


# ---------- thinking 产物持久化 ----------

def test_thinking_outcome_persisted_with_output_ref(tmp_path):
    model = CountingModel()
    store, service, _retriever = make_service(tmp_path, model)
    outcome = service.process(make_view())
    assert outcome.stage == "card"
    sid = store._conn.execute(
        "SELECT insight_source_id FROM insight_sources").fetchone()[0]
    run = store._conn.execute(
        "SELECT status, output_ref FROM insight_runs "
        "WHERE stage='thinking'").fetchone()
    assert run["status"] in ("passed", "needs_review")
    assert run["output_ref"]                    # 产物可寻址
    loaded = service._load_thought(sid, 1)
    assert loaded is not None
    assert loaded.draft["title"] == GOOD_THOUGHT["title"]
    assert loaded.status == run["status"]
    assert loaded.final_review is not None


def test_outcome_json_roundtrip_preserves_audit_chain(tmp_path):
    from knowledge_ingest.insight.critic import CriticResult, ThinkingOutcome
    review = CriticResult(
        source_understanding="pass", critical_reasoning="fail",
        personal_connection="pass", cognition_delta="partial",
        own_version="fail", actionability="pass", business_rigor="pass",
        traceability="partial", mechanism_salvage="pass",
        genericity_detected=True, revision_required=True,
        revision_instructions=("补锚点", "给判停条件"))
    outcome = ThinkingOutcome(
        draft=dict(GOOD_THOUGHT), final_review=review, status="needs_review",
        initial_draft=dict(GOOD_THOUGHT), initial_review=review,
        revision_instructions=("补锚点",))
    blob = outcome_to_json(outcome)
    loaded = outcome_from_json(blob)
    assert loaded == outcome


# ---------- THE 回归：卡崩 → 重启 → 零重复调用 → 卡落盘一次 ----------

def test_card_crash_restart_no_model_recall_card_written_once(tmp_path):
    model = CountingModel()
    _store, service, retriever = make_service(tmp_path, model)
    crashing = CrashingCardWriter(service.card_writer)
    service.card_writer = crashing
    try:
        service.process(make_view())
        raise AssertionError("expected card-write crash")
    except RuntimeError as exc:
        assert "card-write crash" in str(exc)
    first_counts = (model.evidence_calls, model.thinking_calls,
                    model.critic_calls, retriever.calls)
    assert first_counts == (1, 1, 1, 1)

    # 重启：新 store/service 实例（同 DB），完整产物恢复
    _store2, service2, retriever2 = make_service(tmp_path, model)
    outcome = service2.process(make_view())
    assert outcome.stage == "card"
    assert outcome.status in ("passed", "needs_review")
    assert (model.evidence_calls, model.thinking_calls,
            model.critic_calls) == (1, 1, 1)     # 零重复模型调用
    assert retriever2.calls == 0                 # 检索也不重复
    assert crashing.calls == 1                   # 崩溃 writer 未被再触发
    cards = list((tmp_path / "insight" / "cards").rglob("*.md"))
    assert len(cards) == 1                       # 卡片恰好落盘一张


# ---------- 幂等重扫：已产出卡片的条目零新增调用 ----------

def test_rescan_completed_card_no_new_calls(tmp_path):
    model = CountingModel()
    store, service, retriever = make_service(tmp_path, model)
    outcome = service.process(make_view())
    assert outcome.stage == "card"
    card_rows_before = store._conn.execute(
        "SELECT COUNT(*) FROM insight_cards").fetchone()[0]
    service.process(make_view())                 # 同 revision 重扫
    assert (model.evidence_calls, model.thinking_calls,
            model.critic_calls) == (1, 1, 1)
    assert retriever.calls == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM insight_cards").fetchone()[0] == \
        card_rows_before == 1


# ---------- card 行落库（digest/doctor 的数据源缺口补齐） ----------

def test_card_row_recorded_with_pack_provenance(tmp_path):
    model = CountingModel()
    store, service, _retriever = make_service(tmp_path, model)
    outcome = service.process(make_view())
    row = store._conn.execute(
        "SELECT quality_status, cognition_delta, meta_json "
        "FROM insight_cards").fetchone()
    assert row is not None
    assert row["quality_status"] == outcome.status
    assert row["cognition_delta"] == "ADD"
    meta = json.loads(row["meta_json"])
    assert meta["context_pack_id"].startswith("ctxpack.")


# ---------- M10-R4 复验发现：needs_review 产物也必须 resume ----------

def test_needs_review_thought_resumed_without_model_recall(tmp_path):
    """critic needs_review 是成功完成的昂贵 stage（重启恢复语义，
    不是可重跑草稿）：needs_review 卡片重启后不得重跑 thinking/critic。
    （M10-R4 复验实测：52258 thinking x2 = main + rescan，即此缺口。）"""
    model = CountingModel(critic=CRITIC_STRICT)
    _store, service, retriever = make_service(tmp_path, model)
    outcome = service.process(make_view())
    assert outcome.stage == "card" and outcome.status == "needs_review"
    baseline = (model.evidence_calls, model.thinking_calls,
                model.critic_calls)
    assert baseline[1] >= 1               # 含修订轮

    service.process(make_view())          # 同 revision 重扫
    assert (model.evidence_calls, model.thinking_calls,
            model.critic_calls) == baseline   # 零新增=从产物恢复
    assert retriever.calls == 1
