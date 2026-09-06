from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.next_action import (
    gate_enter,
    gate_resolve,
    next_action,
    target_complete,
)
from knowledge_ingest.state_machine import InvalidTransition, transition_to


def make_manifest(targets=("cangjie", "personal"), status="CORPUS_READY") -> JobManifest:
    now = datetime.now(timezone.utc)
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
    manifest = make_manifest(status="DISTILLING_CANGJIE")
    gate_enter(manifest, "cangjie", "stage0_overview")
    action = next_action(manifest)
    assert action["next_action"] == "ask_user"
    assert action["target"] == "cangjie"
    assert action["gate"] == "stage0_overview"
    assert manifest.status == "WAITING_USER"


def test_gate_enter_requires_distilling_state():
    manifest = make_manifest(status="ROUTING")
    with pytest.raises(InvalidTransition):
        gate_enter(manifest, "cangjie", "stage0_overview")


def test_gate_enter_rejects_target_not_in_request():
    manifest = make_manifest(targets=("cangjie",), status="DISTILLING_CANGJIE")
    with pytest.raises(ValueError):
        gate_enter(manifest, "personal", "inventory_reviewed")


def test_gate_resolve_confirmed_resumes():
    manifest = make_manifest(status="DISTILLING_CANGJIE")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "confirmed")
    assert manifest.status == "DISTILLING_CANGJIE"
    assert manifest.cangjie.waiting_for is None
    assert manifest.cangjie.status == "running"
    assert manifest.gate_history[-1]["decision"] == "confirmed"


def test_gate_resolve_rejected_single_target_completes():
    manifest = make_manifest(targets=("cangjie",),
                             status="DISTILLING_CANGJIE")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "rejected")
    assert manifest.status == "COMPLETED"
    assert any(e.get("reason") == "target_rejected" for e in manifest.errors)


def test_gate_rejected_chains_to_remaining_target():
    """双 target 时拒绝 cangjie 不得静默吞掉 personal。"""
    manifest = make_manifest(targets=("cangjie", "personal"),
                             status="DISTILLING_CANGJIE")
    gate_enter(manifest, "cangjie", "stage0_overview")
    gate_resolve(manifest, "cangjie", "stage0_overview", "rejected")
    assert manifest.cangjie.status == "skipped"
    assert manifest.status == "DISTILLING_PERSONAL"
    # 剩余 target 继续被拒后才 COMPLETED
    gate_enter(manifest, "personal", "inventory_reviewed")
    gate_resolve(manifest, "personal", "inventory_reviewed", "rejected")
    assert manifest.status == "COMPLETED"
    assert manifest.personal.status == "skipped"


def test_target_complete_records_pipeline_state():
    manifest = make_manifest(status="DISTILLING_CANGJIE")
    state_file = Path("/tmp/books/x/PIPELINE_STATE.md")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"),
                    pipeline_state=state_file)
    assert manifest.cangjie.pipeline_state == state_file


def test_gate_resolve_wrong_gate_rejected():
    manifest = make_manifest(status="DISTILLING_CANGJIE")
    gate_enter(manifest, "cangjie", "stage0_overview")
    with pytest.raises(ValueError):
        gate_resolve(manifest, "cangjie", "other_gate", "confirmed")


def test_target_complete_cangjie_moves_to_personal():
    manifest = make_manifest(status="DISTILLING_CANGJIE")
    target_complete(manifest, "cangjie", Path("/tmp/out/cangjie"))
    assert manifest.status == "DISTILLING_PERSONAL"
    assert manifest.cangjie.status == "success"
    assert manifest.cangjie.output_path == Path("/tmp/out/cangjie")


def test_target_complete_last_target_completes_job():
    manifest = make_manifest(status="DISTILLING_PERSONAL")
    target_complete(manifest, "personal", Path("/tmp/out/personal"))
    assert manifest.status == "COMPLETED"
    assert manifest.personal.status == "success"


def test_blocked_next_action_requires_human():
    manifest = make_manifest(status="ROUTING")
    manifest.errors.append({"reason": "unsupported_inputs"})
    transition_to(manifest, "BLOCKED")
    action = next_action(manifest)
    assert action["next_action"] == "resolve_blocked"
    assert action["reason"] == "unsupported_inputs"


def test_completed_next_action_is_report():
    action = next_action(make_manifest(status="COMPLETED"))
    assert action["next_action"] == "report"


def test_next_never_offers_distill_before_corpus_ready():
    for status in ("CREATED", "DOWNLOADED", "ROUTING", "TRANSCRIBING",
                   "DOCCHUNKING", "VERIFYING"):
        action = next_action(make_manifest(status=status))
        assert action["next_action"] not in {
            "invoke_cangjie", "invoke_personal_distiller"}
