"""V3 M10-R1: Empty Context Pack 是一等持久化对象（P0 修复）。

生产冷启动（approved_cognition=0）下 NO_RELEVANT_PERSONAL_CONTEXT 是
合法结果，因此 `source_revision → context_pack_id → refs=[]` 必须：
可持久化、进程退出后可恢复、可被下游引用、可出现在卡面 provenance，
且绝不制造假 cognition ref 占位。

回归背景：M10 Batch-2 中 4/4 个过门条目在卡片写出前因
`get_context_pack(sid).pack_id` 空解引用崩溃（验收记录 §4 M10-BUG-1）。
"""

from pathlib import Path

from knowledge_ingest.insight.cards import DeepInsightCardWriter
from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.service import InsightService
from knowledge_ingest.insight.store import INSIGHT_SCHEMA_VERSION, InsightStore

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


def test_schema_contains_context_packs_table(tmp_path):
    store = InsightStore(tmp_path / "insight" / "state.db")
    tables = {r[0] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "insight_context_packs" in tables
    assert INSIGHT_SCHEMA_VERSION >= 2


def test_zero_refs_persists_first_class_pack(tmp_path):
    """零 refs → 合法 context_pack_id，pack 行真实存在（非 ref 行反推）。"""
    store = InsightStore(tmp_path / "insight" / "state.db")
    sid = store.register_source_view(make_view())
    pack_id = store.replace_context_refs(sid, ())
    assert pack_id.startswith("ctxpack.")
    row = store._conn.execute(
        "SELECT ref_count FROM insight_context_packs "
        "WHERE insight_source_id = ?", (sid,)).fetchone()
    assert row is not None and row["ref_count"] == 0
    pack = store.get_context_pack(sid)
    assert pack is not None
    assert pack.pack_id == pack_id
    assert pack.refs == ()


def test_empty_pack_survives_store_reopen(tmp_path):
    """close/reopen 后 `source_revision → pack_id → refs=[]` 仍成立。"""
    db = tmp_path / "insight" / "state.db"
    store = InsightStore(db)
    sid = store.register_source_view(make_view())
    pack_id = store.replace_context_refs(sid, ())
    store.close()

    reopened = InsightStore(db)
    pack = reopened.get_context_pack(sid)
    assert pack is not None
    assert pack.pack_id == pack_id
    assert pack.refs == ()
    assert pack.source_revision == 1


def test_pack_none_when_retrieval_never_ran(tmp_path):
    """未到 DEEP_READ 的条目没有 pack：None 语义保留（不是空 pack）。"""
    store = InsightStore(tmp_path / "insight" / "state.db")
    sid = store.register_source_view(make_view())
    assert store.get_context_pack(sid) is None


def test_new_revision_starts_without_pack(tmp_path):
    """EDIT → 新 revision 从空开始：旧 pack 不被继承。"""
    store = InsightStore(tmp_path / "insight" / "state.db")
    sid = store.register_source_view(make_view())
    store.replace_context_refs(sid, ())
    assert store.get_context_pack(sid) is not None
    store.register_source_view(make_view(content_fingerprint="fp-2"))
    assert store.get_context_pack(sid) is None


# ---------- service 全链：零 refs 不崩、卡面带 pack_id ----------

EMPTY_THOUGHT = {
    "title": "空个人语境下的独立判断",
    "bottom_line": "结论只来自材料本身",
    "source_understanding": "s", "mechanism": "m", "challenge": "c",
    "personal_connections": [],
    "project_impacts": [], "business_opportunity": None,
    "own_version": "仅材料内证据", "cognition_delta": "NONE",
    "actions": [], "human_gate_recommendation": "WATCH",
    "final_verdict": "CARD",
}
EMPTY_EVIDENCE = {"claims": ["c"], "evidence": ["e"], "inferences": [],
                  "unknowns": [], "verification_flags": []}


class _FakeModel:
    def __init__(self):
        self.thinking_calls = 0

    def run(self, stage, payload):
        if stage == "evidence_extract":
            return dict(EMPTY_EVIDENCE)
        if stage == "thinking":
            self.thinking_calls += 1
            return dict(EMPTY_THOUGHT)
        if stage == "critic":
            return {"source_understanding": "pass",
                    "critical_reasoning": "pass",
                    "personal_connection": "pass",
                    "cognition_delta": "pass", "own_version": "pass",
                    "actionability": "pass", "business_rigor": "pass",
                    "traceability": "pass", "mechanism_salvage": "pass",
                    "genericity_detected": False,
                    "revision_required": False,
                    "revision_instructions": []}
        raise AssertionError(stage)


class _FakeFilter:
    def evaluate(self, view):
        from knowledge_ingest.insight.models import CandidateDecision
        return CandidateDecision(candidate=True, confidence=0.7)


class _FakeGate:
    def evaluate(self, view, candidate):
        from knowledge_ingest.insight.models import DeepValueDecision
        return DeepValueDecision("DEEP_READ", reason="r")


class _EmptyRetriever:
    def __init__(self):
        self.calls = 0

    def retrieve(self, view, content):
        self.calls += 1
        return ()


class _Resolver:
    def resolve(self, view):
        from knowledge_ingest.insight.content_resolver import ContentPart, ResolvedContent
        return ResolvedContent(
            parts=(ContentPart(part_id="main", text="正文",
                               source_ref="/x"),),
            verification_status=view.verification_status)


def _make_service(tmp_path):
    from knowledge_ingest.insight.candidate_filter import HighRecallCandidateFilter
    from knowledge_ingest.insight.critic import QualityCritic
    from knowledge_ingest.insight.value_gate import DeepValueGate
    store = InsightStore(tmp_path / "insight" / "state.db")
    model = _FakeModel()
    retriever = _EmptyRetriever()
    service = InsightService(
        store, resolver=_Resolver(), retriever=retriever,
        candidate_filter=HighRecallCandidateFilter(model, taste_profile="t"),
        value_gate=DeepValueGate(model), model_port=model,
        critic=QualityCritic(model),
        card_writer=DeepInsightCardWriter(tmp_path / "insight"),
        now=lambda: NOW)
    service.candidate_filter = _FakeFilter()
    service.value_gate = _FakeGate()
    return store, service, model, retriever


def test_deep_read_zero_refs_full_chain_writes_card(tmp_path):
    """DEEP_READ 全链在零 refs 下不崩；卡面带 context_pack_id；
    Personal Connection 诚实为空；NONE delta 不产生 proposal。"""
    store, service, _model, retriever = _make_service(tmp_path)
    outcome = service.process(make_view())
    assert outcome.stage == "card"
    assert outcome.status in ("passed", "needs_review")
    assert outcome.proposal_id is None        # delta=NONE → 无 proposal
    markdown = Path(outcome.card_path).read_text(encoding="utf-8")
    sid = store._conn.execute(
        "SELECT insight_source_id FROM insight_sources").fetchone()[0]
    pack = store.get_context_pack(sid)
    assert pack is not None and pack.refs == ()
    assert pack.pack_id in markdown           # provenance 可追溯
    assert "ctxpack." in markdown
    # 空检索只允许发生一次（pack 行存在后不再重检索）
    assert retriever.calls == 1
