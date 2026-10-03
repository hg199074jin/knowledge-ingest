"""R1.5-A: production shadow scan 组合根——真实 wiring、增量、bounded、gate。

覆盖 Issue #27 §25 scan 项：
- scan 不再是 no-op（真正驱动 InsightService.process）
- limit=1 bounded；无 eligible source → 干净 no-op
- V2 adapter 只读；同 revision restart-safe（不重复消耗模型预算）
- model failure → blocked/retryable，不记成 reject；单项失败不破坏其它状态
- candidate=false / WATCH / ARCHIVE / REJECT 均为合法终态
- DEEP_READ 走到完整组合
- strict isolation 失败时**在模型调用之前** fail-closed
- production knowledge adapter 必须真实存在（无 fake-empty 回落）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.production import (
    InsightActivationError,
    assert_activation_ready,
    build_production_insight_service,
    build_production_knowledge_port,
    is_incremental_candidate,
    run_shadow_scan,
    select_incremental_views,
)
from knowledge_ingest.insight.store import InsightStore

NOW = "2026-10-02T00:00:00+00:00"


def make_view(item_id: str, *, fingerprint: str = "fp.aaa", text: str = "内容"
              ) -> InsightSourceView:
    return InsightSourceView(
        provider="telegram", source_item_id=item_id, content_kind="text",
        title=None, visible_text=text, materialized_path=None,
        full_text_available=False, verification_status="source_only",
        content_fingerprint=fingerprint, captured_at=NOW, author="u1",
        source_deleted=False, original_refs=({"message_id": 1},))


class FakeAdapter:
    """V2 只读投影器替身；记录被读取的 item 以证明只读遍历。"""

    def __init__(self, views):
        self.views = list(views)
        self.iterated: list[str] = []

    def iter_views(self):
        for view in self.views:
            self.iterated.append(view.source_item_id)
            yield view


class RecordingService:
    """只记录调用的 service 替身（组合根 wiring 测试，不测状态机）。"""

    def __init__(self, outcomes=None):
        self.seen: list[str] = []
        self.outcomes = outcomes or {}

    def process(self, view):
        from knowledge_ingest.insight.service import StageOutcome
        self.seen.append(view.source_item_id)
        outcome = self.outcomes.get(view.source_item_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or StageOutcome("candidate", "rejected",
                                       detail="candidate=false")


@pytest.fixture
def store(tmp_path) -> InsightStore:
    return InsightStore(tmp_path / "insight" / "state.db")


# ---------- incremental selection ----------

def test_new_source_is_incremental_candidate(store):
    assert is_incremental_candidate(store, make_view("item-1")) is True


def test_registered_same_revision_is_not_incremental(store):
    view = make_view("item-1")
    store.register_source_view(view)
    assert is_incremental_candidate(store, view) is False


def test_changed_fingerprint_is_incremental_candidate(store):
    store.register_source_view(make_view("item-1", fingerprint="fp.old"))
    assert is_incremental_candidate(
        store, make_view("item-1", fingerprint="fp.new")) is True


def test_selection_is_read_only_for_already_processed(store):
    view = make_view("item-1")
    store.register_source_view(view)
    adapter = FakeAdapter([view])
    assert select_incremental_views(store, adapter) == []
    assert adapter.iterated == ["item-1"]        # 仍只读遍历，不写
    assert store.count_sources() == 1            # 未新增/未改写来源行


def test_limit_bounds_selection(store):
    views = [make_view(f"item-{i}") for i in range(5)]
    adapter = FakeAdapter(views)
    selected = select_incremental_views(store, adapter, limit=1)
    assert [v.source_item_id for v in selected] == ["item-0"]
    assert adapter.iterated == ["item-0"]        # 早停，不全量遍历


def test_no_eligible_source_is_clean_noop(store):
    view = make_view("item-1")
    store.register_source_view(view)
    assert select_incremental_views(store, FakeAdapter([view])) == []


# ---------- activation gate（模型调用之前） ----------

def test_gate_fails_closed_without_isolation_env():
    with pytest.raises(InsightActivationError) as exc:
        assert_activation_ready({})
    assert "activation_ready=FAIL" in str(exc.value)


def test_service_construction_refuses_when_gate_fails(store, tmp_path):
    with pytest.raises(InsightActivationError):
        build_production_insight_service(
            store=store, insight_root=tmp_path / "insight", env={})


def test_scan_refuses_before_touching_service(store, tmp_path):
    """gate 失败 → 不选 source、不写 V3、不调用模型。"""
    adapter = FakeAdapter([make_view("item-1")])
    with pytest.raises(InsightActivationError):
        run_shadow_scan(store=store, adapter=adapter,
                        insight_root=tmp_path / "insight", env={}, limit=1)
    assert adapter.iterated == []               # 连遍历都没发生
    assert store.count_sources() == 0


# ---------- production knowledge adapter（不得 fake-empty 回落） ----------

def test_knowledge_port_is_real_approved_cognition_port(store):
    port = build_production_knowledge_port(store, env={})
    assert type(port).__name__ == "ApprovedCognitionKnowledgePort"
    # 真实 port 的语义：无 CONFIRMED 认知时合法返回空（不是异常/伪造）
    assert port.search(["q"], statuses=["CONFIRMED"], limit=5) == []


def test_knowledge_port_composites_command_port_when_configured(store):
    env = {"KI_INSIGHT_RETRIEVAL_CMD": "/usr/bin/true"}
    port = build_production_knowledge_port(store, env=env)
    assert type(port).__name__ == "CompositePersonalKnowledgePort"
    assert len(port.ports) == 2
    assert type(port.ports[1]).__name__ == "CommandPersonalKnowledgePort"


def test_approved_cognition_port_returns_records_after_adopt(store):
    store.upsert_approved_cognition(
        record_id="cog-1", proposal_id="p1", cognition="三级决策结构",
        domain=None, approved_at=NOW, approved_by="user")
    port = build_production_knowledge_port(store, env={})
    records = port.search(["决策"], statuses=["CONFIRMED"], limit=5)
    assert [r.record_id for r in records] == ["cog-1"]


# ---------- scan 驱动（真实 service 组合） ----------

def _patched_service(monkeypatch, service):
    monkeypatch.setattr(
        "knowledge_ingest.insight.production."
        "build_production_insight_service",
        lambda **kwargs: service)
    monkeypatch.setattr(
        "knowledge_ingest.insight.production.assert_activation_ready",
        lambda *a, **k: None)


def test_scan_is_not_noop_drives_service(monkeypatch, store, tmp_path):
    from knowledge_ingest.insight.service import StageOutcome
    service = RecordingService(
        {"item-1": StageOutcome("value_gate", "watch", detail="WATCH")})
    _patched_service(monkeypatch, service)
    report = run_shadow_scan(store=store, adapter=FakeAdapter([make_view("item-1")]),
                             insight_root=tmp_path / "insight", limit=1)
    assert service.seen == ["item-1"]            # 真正调用了 service.process
    assert report.scanned == 1
    assert report.outcomes == [("text", "value_gate", "watch")]


def test_scan_respects_limit(monkeypatch, store, tmp_path):
    service = RecordingService()
    _patched_service(monkeypatch, service)
    views = [make_view(f"item-{i}") for i in range(4)]
    run_shadow_scan(store=store, adapter=FakeAdapter(views),
                    insight_root=tmp_path / "insight", limit=2)
    assert len(service.seen) == 2


def test_scan_no_eligible_source_is_clean_noop(monkeypatch, store, tmp_path):
    view = make_view("item-1")
    store.register_source_view(view)
    service = RecordingService()
    _patched_service(monkeypatch, service)
    report = run_shadow_scan(store=store, adapter=FakeAdapter([view]),
                             insight_root=tmp_path / "insight")
    assert service.seen == []
    assert report.scanned == 0 and report.outcomes == []


def test_scan_restart_safe_same_revision_not_reprocessed(
        monkeypatch, store, tmp_path):
    view = make_view("item-1")
    store.register_source_view(view)
    service = RecordingService()
    _patched_service(monkeypatch, service)
    run_shadow_scan(store=store, adapter=FakeAdapter([view]),
                    insight_root=tmp_path / "insight")
    assert service.seen == []                    # 同 revision 不重复处理


def test_scan_isolates_single_item_failure(monkeypatch, store, tmp_path):
    """单项异常不拖垮整批，也不伪造成 reject。"""
    service = RecordingService({
        "item-1": RuntimeError("model exploded"),
        "item-2": RuntimeError("other"),
    })
    _patched_service(monkeypatch, service)
    views = [make_view("item-1"), make_view("item-2")]
    report = run_shadow_scan(store=store, adapter=FakeAdapter(views),
                             insight_root=tmp_path / "insight")
    assert service.seen == ["item-1", "item-2"]   # 两项都被尝试
    assert report.scanned == 0                    # 失败不计作判定
    assert report.errors == ["RuntimeError", "RuntimeError"]
    assert report.outcomes == []


def test_scan_report_never_leaks_content(monkeypatch, store, tmp_path):
    from knowledge_ingest.insight.service import StageOutcome
    secret = "TOPSECRET-SOURCE-BODY"
    service = RecordingService(
        {"item-1": StageOutcome("content", "waiting", detail=secret)})
    _patched_service(monkeypatch, service)
    report = run_shadow_scan(store=store,
                             adapter=FakeAdapter([make_view("item-1", text=secret)]),
                             insight_root=tmp_path / "insight", limit=1)
    blob = "\n".join(report.lines())
    assert secret not in blob
    assert "TOPSECRET" not in blob


# ---------- V2 只读保证 ----------

def test_v2_store_read_only_rejects_writes():
    from knowledge_ingest.telegram.event_store import TelegramEventStore
    db = Path("/Volumes/ORICO/KnowledgePipeline/telegram/state.db")
    if not db.is_file():                            # 本机无 V2 库 → 跳过
        pytest.skip("no local telegram state.db")
    import sqlite3
    store = TelegramEventStore.open_read_only(db)
    with pytest.raises(sqlite3.OperationalError):
        store._conn.execute("UPDATE source_items SET processing_status='x'")
    assert store.user_version() >= 1


def test_read_only_open_refuses_missing_db(tmp_path):
    from knowledge_ingest.telegram.event_store import TelegramEventStore
    with pytest.raises(FileNotFoundError):
        TelegramEventStore.open_read_only(tmp_path / "absent.db")


def test_read_only_open_does_not_create_db(tmp_path):
    from knowledge_ingest.telegram.event_store import TelegramEventStore
    target = tmp_path / "nested" / "state.db"
    with pytest.raises(FileNotFoundError):
        TelegramEventStore.open_read_only(target)
    assert not target.parent.exists()               # 绝不 mkdir


def test_store_identity_lookup_is_read_only(store):
    assert store.get_source_by_identity("telegram", "nope") is None
    view = make_view("item-1")
    sid = store.register_source_view(view)
    row = store.get_source_by_identity("telegram", "item-1")
    assert row["insight_source_id"] == sid

# ---------- §25 补齐：合法终态 / model failure 语义 / 完整组合 ----------

def test_model_failure_recorded_as_error_never_as_reject(
        monkeypatch, tmp_path, store):
    """§6/§25：model failure → blocked/retryable 语义，绝不记成 reject。"""
    from knowledge_ingest.insight.model_port import ModelCommandFailedError
    _patched_service(monkeypatch, RecordingService(outcomes={
        "i1": ModelCommandFailedError("model command exit 1")}))
    report = run_shadow_scan(store=store, adapter=FakeAdapter([make_view("i1")]),
                             insight_root=tmp_path / "insight")
    assert report.scanned == 0
    assert report.outcomes == []                    # 失败不是业务终态
    assert report.errors == ["ModelCommandFailedError"]
    rendered = " ".join(report.lines())
    assert "reject" not in rendered.lower()


@pytest.mark.parametrize("stage,status", [
    ("candidate", "rejected"),     # candidate=false：合法
    ("deep_value_gate", "passed"),
    ("human_gate", "watch"),      # WATCH：合法业务结果，不强行出 card
    ("human_gate", "archived"),
    ("human_gate", "rejected"),
])
def test_business_terminals_are_legal_and_not_errors(
        monkeypatch, tmp_path, store, stage, status):
    from knowledge_ingest.insight.service import StageOutcome
    _patched_service(monkeypatch, RecordingService(
        outcomes={"i1": StageOutcome(stage, status)}))
    report = run_shadow_scan(store=store, adapter=FakeAdapter([make_view("i1")]),
                             insight_root=tmp_path / "insight")
    assert report.errors == []
    assert report.outcomes == [("text", stage, status)]
    assert report.scanned == 1


def test_production_service_composes_all_existing_components(
        monkeypatch, tmp_path, store):
    """§3.1：必须复用现有七件套，不得重写第二套 pipeline。"""
    from knowledge_ingest.insight.candidate_filter import HighRecallCandidateFilter
    from knowledge_ingest.insight.cards import DeepInsightCardWriter
    from knowledge_ingest.insight.content_resolver import PartsContentResolver
    from knowledge_ingest.insight.critic import QualityCritic, ThinkingOrchestrator
    from knowledge_ingest.insight.retrieval import PersonalReasoningRetriever
    from knowledge_ingest.insight.value_gate import DeepValueGate

    env = {"KI_INSIGHT_REQUIRE_ISOLATION": "0",
           "KI_INSIGHT_MODEL_CMD": "/bin/true"}
    _patched_gate(monkeypatch)
    service = build_production_insight_service(
        store=store, insight_root=tmp_path / "insight", env=env,
        version_probe=lambda p: "")
    assert isinstance(service.resolver, PartsContentResolver)
    assert isinstance(service.retriever, PersonalReasoningRetriever)
    assert isinstance(service.candidate_filter, HighRecallCandidateFilter)
    assert isinstance(service.value_gate, DeepValueGate)
    assert isinstance(service.critic, QualityCritic)
    assert isinstance(service.card_writer, DeepInsightCardWriter)
    assert service.model_port is not None
    assert isinstance(service.orchestrator, ThinkingOrchestrator)


def test_model_port_comes_from_strict_factory_not_raw_constructor(
        monkeypatch, tmp_path, store):
    """§3.2：model port 必须来自 R1.1 factory。

    行为证明：strict env 下 factory 产出的 port 带 `isolation`（require_strict）；
    直接 new 原始 CommandInsightModelPort 不会有该属性。
    """
    from knowledge_ingest.insight.model_port import CommandInsightModelPort
    iso_home = tmp_path / "iso-home"
    iso_home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = {
        "KI_INSIGHT_REQUIRE_ISOLATION": "1",
        "KI_INSIGHT_ISOLATION_HOME": str(iso_home),
        "KI_INSIGHT_ISOLATION_CODEX_HOME": str(tmp_path / "codex-home"),
        "KI_INSIGHT_ISOLATION_WORKSPACE": str(workspace),
        "KI_INSIGHT_MODEL_CMD": "/bin/true",
    }
    _patched_gate(monkeypatch)
    service = build_production_insight_service(
        store=store, insight_root=tmp_path / "insight", env=env)
    port = service.model_port
    assert isinstance(port, CommandInsightModelPort)
    assert port.isolation is not None
    assert port.isolation.require_strict is True
    assert port.isolation.workspace_cwd == workspace


def _patched_gate(monkeypatch):
    """组合根 wiring 测试专用：把 activation gate 替换为 no-op。

    gate 本身由 `test_service_construction_refuses_when_gate_fails` /
    `test_scan_refuses_before_touching_service` 覆盖；这里只验证组件接线。
    """
    from knowledge_ingest.insight import production
    monkeypatch.setattr(production, "assert_activation_ready",
                        lambda *a, **k: None)
