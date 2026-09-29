"""V3 M1: independent Insight Store — schema, idempotence, revision binding.

`insight/state.db` keeps Personal Insight Facts only; it never touches
`telegram/state.db` (Source Facts stay in the Telegram store, 设计 §4.2/§19).
Source identity = (provider, source_item_id); a content_fingerprint change
bumps source_revision, and every decision/run binds the revision it was
made against — so a Telegram EDIT can never silently overwrite an earlier
judgment.
"""

import sqlite3
from pathlib import Path

import pytest

from knowledge_ingest.config import AppConfig
from knowledge_ingest.insight.models import (
    CandidateDecision,
    DeepValueDecision,
    InsightSourceView,
    PersonalContextRef,
)
from knowledge_ingest.insight.store import (
    INSIGHT_SCHEMA_VERSION,
    InsightStore,
    UnsupportedInsightSchemaError,
    insight_root,
)

NOW = "2026-09-28T12:00:00+00:00"

REQUIRED_TABLES = (
    "insight_sources", "insight_candidates", "insight_runs",
    "insight_context_refs", "insight_context_packs", "insight_cards",
    "cognition_proposals", "watch_signals", "trend_clusters",
    "approved_cognition", "insight_ai_budget", "insight_digest_state")


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "m"),
        "docchunk_project": str(tmp_path / "d"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "dc"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "b", "quark": "q", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })


def make_store(tmp_path: Path) -> InsightStore:
    return InsightStore(tmp_path / "insight" / "state.db")


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


# ---------- schema / pragmas ----------

def test_schema_version_and_required_tables(tmp_path):
    store = make_store(tmp_path)
    # M10-R1 起版本 2（insight_context_packs）；只升不降
    assert store.user_version() == INSIGHT_SCHEMA_VERSION >= 2
    names = {row["name"] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(REQUIRED_TABLES) <= names
    assert "insight_context_packs" in names


def test_wal_and_foreign_keys(tmp_path):
    store = make_store(tmp_path)
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_higher_schema_version_fails_fast(tmp_path):
    db = tmp_path / "insight" / "state.db"
    InsightStore(db).close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(UnsupportedInsightSchemaError):
        InsightStore(db)


def test_insight_root_under_pipeline_root(tmp_path):
    config = make_config(tmp_path)
    assert insight_root(config) == tmp_path / "kp" / "insight"


# ---------- source registration / revision ----------

def test_register_source_view_idempotent_same_identity(tmp_path):
    store = make_store(tmp_path)
    sid1 = store.register_source_view(make_view())
    sid2 = store.register_source_view(make_view())
    assert sid1 == sid2
    assert store.count_sources() == 1


def test_fingerprint_change_bumps_revision_same_logical_identity(tmp_path):
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view(content_fingerprint="fp-1"))
    assert store.get_source(sid)["source_revision"] == 1

    sid2 = store.register_source_view(make_view(
        content_fingerprint="fp-2", visible_text="edited body"))
    assert sid2 == sid                       # same logical source identity
    assert store.get_source(sid)["source_revision"] == 2
    assert store.get_source(sid)["content_fingerprint"] == "fp-2"

    sid3 = store.register_source_view(make_view(content_fingerprint="fp-2"))
    assert sid3 == sid
    assert store.get_source(sid)["source_revision"] == 2   # unchanged → no bump


def test_distinct_sources_get_distinct_ids(tmp_path):
    store = make_store(tmp_path)
    a = store.register_source_view(make_view(source_item_id="tg_a:1"))
    b = store.register_source_view(make_view(source_item_id="tg_a:2"))
    assert a != b
    assert store.count_sources() == 2


# ---------- decisions bind revision / idempotent ----------

def test_context_pack_id_stable_and_content_addressed(tmp_path):
    """M6 复核①：pack_id 内容寻址、同 refs 稳定、refs 变则 id 变、
    get_context_pack 返回可追溯三元组。"""
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    ref_a = PersonalContextRef(record_id="PC-001",
                               relation_reason="改变判定层边界",
                               state="CONFIRMED", relation="CONDITIONING")
    ref_b = PersonalContextRef(record_id="PC-002",
                               relation_reason="r2", state="TENTATIVE")
    pack_id_1 = store.replace_context_refs(sid, (ref_a, ref_b))
    assert pack_id_1.startswith("ctxpack.")
    assert store.replace_context_refs(sid, (ref_a, ref_b)) == pack_id_1
    pack_id_2 = store.replace_context_refs(sid, (ref_b,))
    assert pack_id_2 != pack_id_1
    pack = store.get_context_pack(sid)
    assert pack.pack_id == pack_id_2
    assert pack.source_revision == 1
    assert [r.record_id for r in pack.refs] == ["PC-002"]


def test_candidate_decision_idempotent_and_revision_bound(tmp_path):
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    decision = CandidateDecision(candidate=True,
                                 possible_value=("COGNITION",),
                                 reasons=("r",), confidence=0.6)
    store.record_candidate(sid, decision)
    store.record_candidate(sid, decision)    # same decision → idempotent
    assert store.count_candidates(sid) == 1
    row = store.latest_candidate(sid)
    assert row["source_revision"] == 1
    assert row["candidate"] == 1

    # EDIT → revision 2 → new decision binds revision 2 (old one preserved)
    store.register_source_view(make_view(content_fingerprint="fp-2"))
    store.record_candidate(sid, CandidateDecision(candidate=False,
                                                  confidence=0.1))
    assert store.count_candidates(sid) == 2
    latest = store.latest_candidate(sid)
    assert latest["source_revision"] == 2
    assert latest["candidate"] == 0


def test_value_gate_idempotent_latest_wins_within_revision(tmp_path):
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    store.record_value_gate(sid, DeepValueDecision("WATCH", reason="weak"))
    store.record_value_gate(sid, DeepValueDecision("WATCH", reason="weak"))
    assert store.count_value_gates(sid) == 1
    assert store.latest_value_gate(sid)["decision"] == "WATCH"

    store.record_value_gate(sid, DeepValueDecision("DEEP_READ", reason="strong"))
    assert store.count_value_gates(sid) == 1
    assert store.latest_value_gate(sid)["decision"] == "DEEP_READ"


def test_decisions_reject_unknown_source(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.record_candidate("isv.nope", CandidateDecision(candidate=True))
    with pytest.raises(ValueError):
        store.record_value_gate("isv.nope", DeepValueDecision("WATCH"))


# ---------- runs ----------

def test_context_refs_persist_and_revision_bound(tmp_path):
    """M6 修复①：某 revision 的 Context Pack 一旦确定即持久化；
    下游（Evidence/Thinking/Critic/Card）读同一份，不允许静默重检索。"""
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    ref = PersonalContextRef(record_id="PC-001",
                             relation_reason="改变判定层边界",
                             state="CONFIRMED", source_ref="src://PC-001",
                             kind="cognition", text="两级筛选架构",
                             relation="CONDITIONING")
    assert store.get_context_refs(sid) == ()      # 未确定前为空
    store.replace_context_refs(sid, (ref,))
    loaded = store.get_context_refs(sid)
    assert len(loaded) == 1
    assert loaded[0].record_id == "PC-001"
    assert loaded[0].relation == "CONDITIONING"
    assert loaded[0].relation_reason == "改变判定层边界"
    assert loaded[0].text == "两级筛选架构"
    # 重写同 revision：替换而非追加
    store.replace_context_refs(sid, (PersonalContextRef(
        record_id="PC-002", relation_reason="r2", state="TENTATIVE"),))
    assert [r.record_id for r in store.get_context_refs(sid)] == ["PC-002"]
    # EDIT → 新 revision：refs 从空开始（旧 revision 判定不被继承）
    store.register_source_view(make_view(content_fingerprint="fp-2"))
    assert store.get_context_refs(sid) == ()


def test_run_lifecycle(tmp_path):
    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    run_id = store.create_run(sid, "candidate", "running")
    assert isinstance(run_id, int)
    store.finish_run(run_id, "passed", output_ref="cand-1")
    row = store.get_run(run_id)
    assert row["status"] == "passed"
    assert row["output_ref"] == "cand-1"
    assert row["finished_at"] is not None
    with pytest.raises(ValueError):
        store.finish_run(999999, "passed")


# ---------- isolation from Telegram facts ----------

def test_no_telegram_db_mutation(tmp_path):
    from knowledge_ingest.telegram.event_store import TelegramEventStore
    tg = TelegramEventStore(tmp_path / "telegram" / "state.db")
    tg.add_source("tg_a", chat_id=-1001, start_at=None)
    before = tg._conn.execute(
        "SELECT COUNT(*) FROM source_items").fetchone()[0]

    store = make_store(tmp_path)
    sid = store.register_source_view(make_view())
    store.record_candidate(sid, CandidateDecision(candidate=True,
                                                  confidence=0.5))

    after = tg._conn.execute(
        "SELECT COUNT(*) FROM source_items").fetchone()[0]
    assert before == after == 0
    tg_tables = tg._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'insight%'"
    ).fetchone()[0]
    assert tg_tables == 0
