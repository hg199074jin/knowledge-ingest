from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.next_action import (
    gate_enter,
    gate_resolve,
    next_action,
    target_checkpoint,
    target_complete,
    target_resume,
    target_start,
)
from knowledge_ingest.state_machine import InvalidTransition, transition_to


def make_manifest(targets=("cangjie", "personal"), status="CORPUS_READY") -> JobManifest:
    now = datetime.now(UTC)
    return JobManifest(
        job_id="20260906-120000-local-course",
        created_at=now,
        updated_at=now,
        status=status,
        request=JobRequest(raw_prompt="x", provider="local",
                           source="/tmp/x", targets=list(targets)),
    )


def test_corpus_ready_invokes_cangjie_first():
    action = next_action(make_manifest())
    assert action["next_action"] == "invoke_cangjie"
    assert action["targets_remaining"] == ["cangjie", "personal"]


def test_corpus_ready_only_personal_target():
    action = next_action(make_manifest(targets=("personal",)))
    assert action["next_action"] == "invoke_personal_distiller"


def test_next_returns_ask_user_when_waiting():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage0_overview")
    action = next_action(manifest)
    assert action["next_action"] == "ask_user"
    assert action["target"] == "cangjie"
    assert action["gate"] == "stage0_overview"
    assert manifest.status == "WAITING_USER"


def test_gate_enter_requires_target_running_state():
    manifest = make_manifest(status="ROUTING")
    with pytest.raises(InvalidTransition):
        gate_enter(manifest, "cangjie", "stage0_overview")


def test_gate_enter_rejects_target_not_in_request():
    manifest = make_manifest(targets=("cangjie",), status="CORPUS_READY")
    target_start(manifest, "cangjie")
    with pytest.raises(ValueError):
        gate_enter(manifest, "personal", "inventory_reviewed")


def test_gate_resolve_confirmed_resumes():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "confirmed")
    assert manifest.status == "TARGET_RUNNING"
    assert manifest.targets["cangjie"].waiting_for is None
    assert manifest.targets["cangjie"].status == "RUNNING"
    assert manifest.active_target == "cangjie"
    assert manifest.gate_history[-1]["decision"] == "confirmed"


def test_gate_resolve_rejected_single_target_completes():
    manifest = make_manifest(targets=("cangjie",), status="CORPUS_READY")
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "rejected")
    assert manifest.status == "COMPLETED"
    assert manifest.targets["cangjie"].status == "SKIPPED"
    assert manifest.targets["cangjie"].reason == "user_rejected"
    assert any(e.get("reason") == "target_rejected" for e in manifest.errors)


def test_gate_rejected_chains_to_remaining_target():
    """双 target 时拒绝 cangjie 不得静默吞掉 personal。"""
    manifest = make_manifest(targets=("cangjie", "personal"),
                             status="CORPUS_READY")
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "rejected")
    assert manifest.targets["cangjie"].status == "SKIPPED"
    assert manifest.status == "TARGET_RUNNING"
    # 剩余 target 继续被拒后才 COMPLETED
    target_start(manifest, "personal")
    gate_enter(manifest, "personal", "inventory_reviewed")
    gate_resolve(manifest, "personal", "inventory_reviewed", "rejected")
    assert manifest.status == "COMPLETED"
    assert manifest.targets["personal"].status == "SKIPPED"


def test_target_complete_records_pipeline_state():
    manifest = make_manifest(targets=("cangjie",), status="CORPUS_READY")
    target_start(manifest, "cangjie")
    state_file = Path("/tmp/books/x/PIPELINE_STATE.md")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"),
                    pipeline_state=state_file)
    assert manifest.targets["cangjie"].pipeline_state == state_file
    assert manifest.targets["cangjie"].status == "COMPLETED"
    assert manifest.targets["cangjie"].completed_at is not None


def test_gate_resolve_wrong_gate_rejected():
    manifest = make_manifest(targets=("cangjie",), status="CORPUS_READY")
    target_start(manifest, "cangjie")
    gate_enter(manifest, "cangjie", "stage0_overview")
    with pytest.raises(ValueError):
        gate_resolve(manifest, "cangjie", "other_gate", "confirmed")


def test_target_complete_cangjie_moves_to_personal():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"))
    assert manifest.status == "TARGET_RUNNING"
    assert manifest.active_target == "personal"  # 链式解锁下一个 READY
    assert manifest.targets["cangjie"].status == "COMPLETED"
    assert manifest.targets["cangjie"].output_path == Path("/tmp/out/cangjie")
    target_start(manifest, "personal")
    assert manifest.targets["personal"].status == "RUNNING"


def test_target_complete_last_target_completes_job():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"))
    target_start(manifest, "personal")
    target_complete(manifest, "personal", Path("/tmp/out/personal"))
    assert manifest.status == "COMPLETED"
    assert manifest.targets["personal"].status == "COMPLETED"
    assert manifest.active_target is None  # 终态不得为 active_target


def test_blocked_next_action_requires_human():
    manifest = make_manifest(status="ROUTING")
    manifest.errors.append({"reason": "unsupported_inputs"})
    transition_to(manifest, "BLOCKED")
    action = next_action(manifest)
    assert action["next_action"] == "resolve_blocked"
    assert action["reason"] == "unsupported_inputs"


def test_completed_next_action_outputs_target_outputs():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"))
    target_start(manifest, "personal")
    target_complete(manifest, "personal", Path("/tmp/out/personal"))
    action = next_action(manifest)
    assert action["next_action"] == "report"
    assert action["target_outputs"] == {
        "cangjie": "/tmp/out/cangjie", "personal": "/tmp/out/personal"}
    # v0.2 兼容键
    assert action["cangjie_output"] == "/tmp/out/cangjie"
    assert action["personal_output"] == "/tmp/out/personal"


def test_target_blocked_next_action_lists_blocked_targets():
    manifest = make_manifest(status="CORPUS_READY")
    target_start(manifest, "cangjie")
    manifest.targets["cangjie"].status = "BLOCKED"
    manifest.targets["cangjie"].reason = "breaker_open"
    manifest.active_target = None
    transition_to(manifest, "BLOCKED")
    action = next_action(manifest)
    assert action["next_action"] == "resolve_blocked"
    assert action["blocked_targets"] == ["cangjie"]


def test_next_never_offers_distill_before_corpus_ready():
    for status in ("CREATED", "DOWNLOADED", "ROUTING", "TRANSCRIBING",
                   "DOCCHUNKING", "VERIFYING"):
        action = next_action(make_manifest(status=status))
        assert action["next_action"] not in {
            "invoke_cangjie", "invoke_personal_distiller"}


def test_target_start_requires_dependencies_completed():
    """规格 2：依赖决定能不能运行（dep.status == COMPLETED 唯一词）。"""
    manifest = make_manifest(targets=("cangjie", "family_router"))
    with pytest.raises(ValueError):
        target_start(manifest, "family_router")
    manifest.targets["cangjie"].status = "COMPLETED"
    target_start(manifest, "family_router")
    assert manifest.targets["family_router"].status == "RUNNING"


def test_target_checkpoint_records_transactional_state():
    manifest = make_manifest(targets=("cangjie",), status="CORPUS_READY")
    target_start(manifest, "cangjie")
    record = target_checkpoint(manifest, "cangjie", "stage3",
                               checkpoint_path=Path("/tmp/ckpt.md"),
                               evidence_dir=Path("/tmp/evidence"))
    assert record["attempt"] == 1
    state = manifest.targets["cangjie"]
    assert state.checkpoint_path == Path("/tmp/ckpt.md")
    assert state.evidence_dir == Path("/tmp/evidence")
    assert state.checkpoint_at is not None
    assert state.attempt == 1
    again = target_checkpoint(manifest, "cangjie", "stage4")
    assert again["attempt"] == 2
    assert any(g.get("action") == "checkpoint"
               for g in manifest.gate_history)


def test_target_resume_restores_blocked_target():
    """规格 9 Blocker 1：BLOCKED→READY（依赖 COMPLETED）；overall 恢复可调度。"""
    manifest = make_manifest(targets=("cangjie", "family_router"),
                             status="CORPUS_READY")
    target_start(manifest, "cangjie")
    # 预算拒绝：target=BLOCKED、overall=BLOCKED、active_target=null
    manifest.targets["cangjie"].status = "BLOCKED"
    manifest.targets["cangjie"].reason = "breaker_open"
    manifest.active_target = None
    transition_to(manifest, "BLOCKED")
    # cangjie 无依赖 → 恢复 READY；overall 恢复 CORPUS_READY（无其他 RUNNING）
    assert target_resume(manifest, "cangjie") == "READY"
    assert manifest.status == "CORPUS_READY"
    assert manifest.active_target is None


def test_target_resume_blocked_target_with_completed_deps_ready():
    """cangjie 已 COMPLETED → BLOCKED 的 family_router 恢复 READY。"""
    manifest = make_manifest(targets=("cangjie", "family_router"))
    target_start(manifest, "cangjie")  # 声明顺序：cangjie 先跑
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"))
    target_start(manifest, "family_router")
    # 预算拒绝：target=BLOCKED、overall=BLOCKED、active_target=null
    manifest.targets["family_router"].status = "BLOCKED"
    manifest.targets["family_router"].reason = "budget_exhausted"
    manifest.active_target = None
    transition_to(manifest, "BLOCKED")
    # 依赖已全 COMPLETED → 恢复 READY；无其他 RUNNING → CORPUS_READY
    assert target_resume(manifest, "family_router") == "READY"
    assert manifest.status == "CORPUS_READY"
    assert manifest.active_target is None


def test_target_resume_pending_when_dependency_unmet():
    """依赖未全 COMPLETED → resume 后保持 PENDING（不 READY）。"""
    manifest = make_manifest(targets=("cangjie", "family_router"),
                             status="CORPUS_READY")
    target_start(manifest, "cangjie")
    # cangjie 仍在跑时它自己被 BLOCKED（如 breaker 打开）
    manifest.targets["cangjie"].status = "BLOCKED"
    manifest.targets["cangjie"].reason = "breaker_open"
    manifest.active_target = None
    transition_to(manifest, "BLOCKED")
    assert target_resume(manifest, "cangjie") == "READY"
    # family_router 依赖未满足，保持 PENDING
    assert manifest.targets["family_router"].status == "PENDING"


def test_target_resume_not_blocked_raises():
    manifest = make_manifest(status="CORPUS_READY")
    with pytest.raises(ValueError):
        target_resume(manifest, "cangjie")
