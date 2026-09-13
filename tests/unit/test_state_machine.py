from datetime import datetime, timezone

import pytest

from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.state_machine import InvalidTransition, transition_to


def make_manifest(status: str = "ROUTING") -> JobManifest:
    now = datetime.now(timezone.utc)
    return JobManifest(
        job_id="20260906-000000-local-test",
        created_at=now,
        updated_at=now,
        status=status,
        request=JobRequest(
            raw_prompt="x",
            provider="local",
            source="/tmp/x",
            targets=["cangjie"],
        ),
    )


def test_legal_transition_updates_status_and_timestamp():
    manifest = make_manifest("DOWNLOADED")
    before = manifest.updated_at
    moved = transition_to(manifest, "ROUTING")
    assert moved.status == "ROUTING"
    assert moved.updated_at >= before


def test_cannot_distill_before_corpus_ready():
    manifest = make_manifest("ROUTING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "TARGET_RUNNING")


def test_cannot_skip_verifying():
    manifest = make_manifest("DOCCHUNKING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "CORPUS_READY")


def test_waiting_user_can_resume_distillation():
    manifest = make_manifest("WAITING_USER")
    assert transition_to(manifest, "TARGET_RUNNING").status == "TARGET_RUNNING"
    assert transition_to(make_manifest("WAITING_USER"), "COMPLETED").status == "COMPLETED"


def test_unknown_status_rejected():
    manifest = make_manifest("ROUTING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "SOMETHING_ELSE")


def test_blocked_can_recover_to_rework():
    manifest = make_manifest("BLOCKED")
    assert transition_to(manifest, "ROUTING").status == "ROUTING"


def test_blocked_can_recover_to_schedulable():
    """规格 9 Blocker 1：BLOCKED→CORPUS_READY / TARGET_RUNNING 恢复出口。"""
    assert transition_to(make_manifest("BLOCKED"), "CORPUS_READY").status \
        == "CORPUS_READY"
    assert transition_to(make_manifest("BLOCKED"), "TARGET_RUNNING").status \
        == "TARGET_RUNNING"


def test_full_happy_path_chain():
    manifest = make_manifest("CREATED")
    for step in (
        "DOWNLOADED",
        "ROUTING",
        "TRANSCRIBING",
        "DOCCHUNKING",
        "VERIFYING",
        "CORPUS_READY",
        "TARGET_RUNNING",
        "WAITING_USER",
        "TARGET_RUNNING",
        "COMPLETED",
    ):
        manifest = transition_to(manifest, step)
    assert manifest.status == "COMPLETED"


def test_target_running_self_transition_requires_changed_active_target():
    """规格 8：TARGET_RUNNING→TARGET_RUNNING 自转换仅当 active_target 变更。"""
    manifest = make_manifest("TARGET_RUNNING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "TARGET_RUNNING")


def test_target_running_self_transition_guard_rails():
    manifest = make_manifest("TARGET_RUNNING")
    manifest.targets["personal"] = type(manifest.targets["cangjie"])()
    # 新 target 未注册 → 拒绝
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "TARGET_RUNNING", new_active_target="nope")
    # 自身已 COMPLETED → 拒绝
    manifest.targets["personal"].status = "COMPLETED"
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "TARGET_RUNNING", new_active_target="personal")
    # 合法：active_target 变更 + 已注册 + 未 COMPLETED
    manifest.targets["personal"].status = "READY"
    moved = transition_to(manifest, "TARGET_RUNNING",
                          new_active_target="personal")
    assert moved.active_target == "personal"


def test_target_running_self_transition_requires_completed_deps():
    """规格 8：自转换要求新 target 依赖全 COMPLETED。"""
    from knowledge_ingest.targets import REGISTRY

    manifest = make_manifest("TARGET_RUNNING")
    manifest.request.targets.append("family_router")
    manifest.targets["family_router"] = type(manifest.targets["cangjie"])(
        depends_on=list(REGISTRY["family_router"].depends_on))
    # cangjie 未 COMPLETED → 拒绝
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "TARGET_RUNNING",
                      new_active_target="family_router")
    manifest.targets["cangjie"].status = "COMPLETED"
    moved = transition_to(manifest, "TARGET_RUNNING",
                          new_active_target="family_router")
    assert moved.active_target == "family_router"
