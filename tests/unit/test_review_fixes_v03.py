"""v0.3 Review fixes: C1 (gate_enter mutual exclusion), C2 (budget acquire
CORPUS_READY -> BLOCKED), C3 (transition_to ACTIVE target guard),
I3 (target_resume calls settle)."""

import pytest

from knowledge_ingest import next_action as na
from knowledge_ingest.models import (
    ACTIVE_TARGET_STATUSES, JobManifest, JobRequest, TargetState,
)
from knowledge_ingest.state_machine import (
    InvalidTransition, transition_to,
)


def make_manifest(targets, status="TARGET_RUNNING", active=None):
    data = {
        "schema_version": 2,
        "job_id": "r/test",
        "created_at": "2026-09-13T00:00:00+00:00",
        "updated_at": "2026-09-13T00:00:00+00:00",
        "status": status,
        "request": {"raw_prompt": "x", "provider": "local",
                    "source": "src",
                    "targets": list(targets)},
        "targets": {n: s.model_dump() for n, s in targets.items()},
        "active_target": active,
    }
    return JobManifest.model_validate(data)


def test_c2_budget_deny_in_corpus_ready_blocks_overall(tmp_path, monkeypatch):
    """C2: budget acquire in CORPUS_READY denied -> overall becomes BLOCKED."""
    from knowledge_ingest.config import AppConfig
    from knowledge_ingest.manifest_store import ManifestStore
    from knowledge_ingest import cli, budget as budget_mod

    config = AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "mp"),
        "docchunk_project": str(tmp_path / "dp"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "cor"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "baidu-drive", "quark": "quarkclouddrive",
                   "cangjie": "cangjie-skill",
                   "personal_distiller": "personal-capability-distiller",
                   "family_router": "family-router-builder"},
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    # 直接构造 CORPUS_READY manifest（v0.3 评审 C2 的核心路径）
    raw = {
        "schema_version": 2,
        "job_id": "r/test",
        "created_at": "2026-09-13T00:00:00+00:00",
        "updated_at": "2026-09-13T00:00:00+00:00",
        "status": "CORPUS_READY",
        "request": {"raw_prompt": "x", "provider": "local",
                    "source": "src",
                    "targets": ["family_router"]},
        "targets": {
            "family_router": {
                "status": "READY", "depends_on": ["cangjie"],
            },
        },
        "active_target": None,
    }
    from knowledge_ingest.models import JobManifest as JM
    m = JM.model_validate(raw)
    store.save(m)
    monkeypatch.setattr(budget_mod, "acquire",
                        lambda *a, **k: {"allowed": False,
                                          "reason": "budget_exhausted",
                                          "replay": False})
    from argparse import Namespace
    cli._cmd_budget(config, Namespace(
        job_id=m.job_id, budget_command="acquire",
        target="family_router", host="codex",
        case_id="r1", request_id="req1"))
    loaded = store.load(m.job_id)
    assert loaded.targets["family_router"].status == "BLOCKED"
    assert loaded.status == "BLOCKED", (
        "评审 C2: CORPUS_READY 下 budget acquire denied 必须让 overall "
        "同步进入 BLOCKED")
    assert loaded.active_target is None
    assert any(e.get("reason") == "budget_exhausted" for e in loaded.errors)


def test_c1_gate_enter_blocked_when_other_target_running():
    """C1: gate_enter refuses when active_target != this target."""
    manifest = make_manifest({
        "cangjie": TargetState(status="RUNNING", depends_on=[]),
        "family_router": TargetState(
            status="READY", depends_on=["cangjie"]),
    }, active="cangjie")
    with pytest.raises((InvalidTransition, ValueError)):
        na.gate_enter(manifest, "family_router", "cost_budget_confirmed")


def test_c3_transition_self_blocks_when_other_target_active():
    """C3: transition_to self-transition refuses when another target
    is already ACTIVE (RUNNING/WAITING_USER)."""
    # 陈述：两个 RUNNING target 在 invariants 下禁止同时存在。
    # 直接对 transition_to 调用，构造能通过 invariants 校验的最坏情形：
    # 两个 target 同时 RUNNING 的状态（模型_validate 会拒绝）—— 测模型
    # invariants 自身已经把这些违反作为非法拒绝（spec 8 已含该不变量）。
    # 然后测 transition_to 在 ACTIVE_TARGET_STATUSES 中有第二个 RUNNING 时
    # 主动拒绝。
    from knowledge_ingest.models import TargetState
    a = TargetState(status="RUNNING", depends_on=[])
    b = TargetState(status="RUNNING", depends_on=[])
    # 用对象组合做最直接的 transition_to 调用
    class _M:
        def __init__(self):
            self.status = "TARGET_RUNNING"
            self.active_target = "cangjie"
            self.targets = {"cangjie": a, "personal": b}
            self.updated_at = None
    with pytest.raises(InvalidTransition):
        transition_to(_M(), "TARGET_RUNNING",
                      new_active_target="personal")


def test_i3_target_resume_calls_settle():
    """I3: target_resume invokes settle after state update."""
    calls: list[str] = []
    import knowledge_ingest.next_action as na_mod
    orig = na_mod.settle
    na_mod.settle = lambda m: calls.append("settle") or orig(m)
    try:
        manifest = make_manifest({
            "cangjie": TargetState(
                status="COMPLETED", depends_on=[]),
            "family_router": TargetState(
                status="BLOCKED", depends_on=["cangjie"],
                reason="budget_exhausted"),
        }, status="BLOCKED", active=None)
        manifest.errors.append({"reason": "budget_exhausted",
                                "target": "family_router"})
        result = na.target_resume(manifest, "family_router")
        assert result == "READY"
        assert "settle" in calls, "I3: target_resume must call settle"
    finally:
        na_mod.settle = orig


def test_active_target_statuses_includes_running_and_waiting():
    assert "RUNNING" in ACTIVE_TARGET_STATUSES
    assert "WAITING_USER" in ACTIVE_TARGET_STATUSES
