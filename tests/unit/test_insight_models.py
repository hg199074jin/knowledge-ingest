"""V3 M1: insight core contracts — frozen vocabularies, immutable records.

Unknown decision/status values are rejected at construction (fail-fast,
no silent coercion). Timestamps that are part of a record must be
timezone-aware ISO-8601; naive timestamps are rejected.
"""

import pytest

from knowledge_ingest.insight.models import (
    CANDIDATE_VALUE_TYPES,
    COGNITION_DELTA_TYPES,
    DEEP_VALUE_STATES,
    HUMAN_GATE_DECISIONS,
    PERSONAL_KNOWLEDGE_STATES,
    THINKING_ACTION_TYPES,
    CandidateDecision,
    CriticResult,
    DeepValueDecision,
    EvidencePack,
    InsightSourceView,
    PersonalContextRef,
    PersonalKnowledgeRecord,
)

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


# ---------- frozen vocabularies ----------

def test_frozen_vocabularies_exact():
    assert CANDIDATE_VALUE_TYPES == (
        "COGNITION", "METHOD", "BUSINESS_OPPORTUNITY",
        "PROJECT_IMPACT", "CONTRARIAN", "WEAK_SIGNAL")
    assert DEEP_VALUE_STATES == ("DEEP_READ", "WATCH", "ARCHIVE_ONLY", "REJECT")
    assert PERSONAL_KNOWLEDGE_STATES == (
        "CONFIRMED", "TENTATIVE", "SUPERSEDED", "REJECTED",
        "EXPERIMENTING", "ARCHIVED")
    assert HUMAN_GATE_DECISIONS == (
        "ADOPT", "EXPERIMENT", "WATCH", "ARCHIVE", "REJECT")
    assert COGNITION_DELTA_TYPES == (
        "ADD", "REINFORCE", "REVISE", "OVERTURN", "NONE")
    assert THINKING_ACTION_TYPES == ("IMMEDIATE", "EXPERIMENT", "WATCH", "NONE")


# ---------- CandidateDecision ----------

def test_candidate_decision_accepts_known_value_types():
    d = CandidateDecision(candidate=True, possible_value=("COGNITION",),
                          reasons=("new mechanism",), confidence=0.6)
    assert d.candidate is True
    assert d.possible_value == ("COGNITION",)


def test_candidate_decision_rejects_unknown_value_type():
    with pytest.raises(ValueError):
        CandidateDecision(candidate=True, possible_value=("MAGIC",),
                          confidence=0.5)


def test_candidate_decision_rejects_confidence_out_of_range():
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            CandidateDecision(candidate=True, confidence=bad)


# ---------- DeepValueDecision ----------

def test_deep_value_decision_freezes_exact_states():
    for state in DEEP_VALUE_STATES:
        assert DeepValueDecision(state, reason="r").decision == state


def test_deep_value_decision_rejects_unknown_state():
    with pytest.raises(ValueError):
        DeepValueDecision("MAYBE", reason="r")


def test_deep_value_decision_allows_unknown_contradiction_but_not_unknown_novelty_wording():
    ok = DeepValueDecision("DEEP_READ", contradiction_value="unknown")
    assert ok.contradiction_value == "unknown"
    with pytest.raises(ValueError):
        DeepValueDecision("DEEP_READ", novelty="extreme")


# ---------- InsightSourceView ----------

def test_source_view_defaults_and_identity_fields():
    view = make_view()
    assert view.source_revision == 1
    assert view.source_deleted is False
    assert view.full_text_available is True


def test_source_view_rejects_unknown_kind_and_status():
    with pytest.raises(ValueError):
        make_view(content_kind="tweet")
    with pytest.raises(ValueError):
        make_view(verification_status="verified_by_vibes")


def test_source_view_rejects_naive_timestamp_and_empty_fingerprint():
    with pytest.raises(ValueError):
        make_view(captured_at="2026-09-28T12:00:00")     # naive
    with pytest.raises(ValueError):
        make_view(content_fingerprint="")


def test_source_view_rejects_revision_below_one():
    with pytest.raises(ValueError):
        make_view(source_revision=0)


# ---------- PersonalKnowledgeRecord / PersonalContextRef ----------

def test_personal_knowledge_record_state_and_tz():
    rec = PersonalKnowledgeRecord(
        record_id="PC-001", kind="cognition", state="CONFIRMED",
        text="Agent 效率=模型×上下文×流程×反馈",
        source_ref="obsidian://x", updated_at=NOW)
    assert rec.state == "CONFIRMED"
    with pytest.raises(ValueError):
        PersonalKnowledgeRecord(record_id="PC-2", kind="cognition",
                                state="TRUE", text="t", source_ref=None,
                                updated_at=NOW)
    with pytest.raises(ValueError):
        PersonalKnowledgeRecord(record_id="PC-3", kind="cognition",
                                state="CONFIRMED", text="t", source_ref=None,
                                updated_at="2026-09-28T12:00:00")   # naive


def test_context_ref_requires_relation_reason_no_hard_links():
    ref = PersonalContextRef(record_id="PC-001",
                             relation_reason="changes decision-layer boundary",
                             state="CONFIRMED", source_ref="s")
    assert ref.relation_reason
    with pytest.raises(ValueError):
        PersonalContextRef(record_id="PC-001", relation_reason="",
                           state="CONFIRMED")
    with pytest.raises(ValueError):
        PersonalContextRef(record_id="PC-001", relation_reason="r",
                           state="GONE")


# ---------- EvidencePack / CriticResult ----------

def test_evidence_pack_separates_categories():
    pack = EvidencePack(
        source_claims=("claim",),
        source_evidence=("evidence",),
        source_inferences=("inference",),
        unknown_variables=("var",),
        personal_context=(PersonalContextRef(
            record_id="PC-001", relation_reason="r", state="CONFIRMED"),),
        verification_flags=("VERIFY_REQUIRED:market_size",))
    assert pack.source_claims == ("claim",)
    assert pack.personal_context[0].record_id == "PC-001"
    assert pack.verification_flags == ("VERIFY_REQUIRED:market_size",)


def test_critic_result_defaults_and_revision_fields():
    critic = CriticResult()
    assert critic.revision_required is False
    assert critic.revision_instructions == ()
    critic2 = CriticResult(revision_required=True,
                           revision_instructions=("add cognition delta",))
    assert critic2.revision_instructions == ("add cognition delta",)
