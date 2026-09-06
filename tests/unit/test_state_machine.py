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
        transition_to(manifest, "DISTILLING_CANGJIE")


def test_cannot_skip_verifying():
    manifest = make_manifest("DOCCHUNKING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "CORPUS_READY")


def test_waiting_user_can_resume_distillation():
    manifest = make_manifest("WAITING_USER")
    assert transition_to(manifest, "DISTILLING_CANGJIE").status == "DISTILLING_CANGJIE"
    assert transition_to(make_manifest("WAITING_USER"), "COMPLETED").status == "COMPLETED"


def test_unknown_status_rejected():
    manifest = make_manifest("ROUTING")
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "SOMETHING_ELSE")


def test_blocked_can_recover_to_rework():
    manifest = make_manifest("BLOCKED")
    assert transition_to(manifest, "ROUTING").status == "ROUTING"


def test_full_happy_path_chain():
    manifest = make_manifest("CREATED")
    for step in (
        "DOWNLOADED",
        "ROUTING",
        "TRANSCRIBING",
        "DOCCHUNKING",
        "VERIFYING",
        "CORPUS_READY",
        "DISTILLING_CANGJIE",
        "WAITING_USER",
        "DISTILLING_CANGJIE",
        "COMPLETED",
    ):
        manifest = transition_to(manifest, step)
    assert manifest.status == "COMPLETED"
