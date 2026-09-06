"""Resume semantics: reload from disk and continue at the exact breakpoint."""

from datetime import datetime, timezone
from pathlib import Path

from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobManifest, JobRequest
from knowledge_ingest.next_action import gate_enter, next_action


def test_resume_after_cangjie_waiting_user(tmp_path: Path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    now = datetime.now(timezone.utc)
    manifest = store.create(JobRequest(
        raw_prompt="混合课程", provider="local", source="/tmp/course",
        targets=["cangjie", "personal"]))

    # 模拟已完成的前置阶段（media success + docchunk verified）
    manifest.status = "CORPUS_READY"
    manifest.media.status = "success"
    manifest.docchunk.status = "verified"
    manifest.docchunk.corpus_path = tmp_path / "corpus"
    manifest.docchunk.verify = "PASS"
    store.save(manifest)

    # 蒸馏开始后进入 Cangjie 骨架确认门
    loaded = store.load(manifest.job_id)
    transition_through(loaded, "DISTILLING_CANGJIE")
    gate_enter(loaded, "cangjie", "stage0_overview")
    store.save(loaded)

    # 新会话恢复：从磁盘重载后 next 必须是 ask_user，绝不能重跑前置阶段
    resumed = store.load(manifest.job_id)
    action = next_action(resumed)
    assert action["next_action"] == "ask_user"
    assert action["target"] == "cangjie"
    assert action["gate"] == "stage0_overview"
    assert action["next_action"] not in {"transcribe", "docchunk", "preprocess"}
    assert resumed.media.status == "success"
    assert resumed.docchunk.status == "verified"


def test_resume_mid_preprocess_keeps_completed_stages(tmp_path: Path):
    store = ManifestStore(jobs_root=tmp_path / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source="/tmp/x",
        targets=["cangjie"]))
    manifest.status = "DOCCHUNKING"
    manifest.media.status = "success"
    store.save(manifest)

    resumed = store.load(manifest.job_id)
    action = next_action(resumed)
    assert action["next_action"] == "preprocess_resume"
    assert resumed.media.status == "success"


def transition_through(manifest: JobManifest, status: str) -> None:
    manifest.updated_at = datetime.now(timezone.utc)
    manifest.status = status
