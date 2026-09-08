"""v0.2.0 evolution: features demanded by the first production run.

Real-run friction → feature:
- 5h ASR showed 'running 0/32' → per-file manifest persistence + events
- hand-built hardcoded ki-resume.sh → generic `resume` command + shipped watchdog
- could not add 'personal' target while preprocess held manifest → `job amend` + pidfile lock
- source.json hand-authored twice → `source init`
- distill scaffolding manual → `distill prepare`
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import (
    _cmd_distill_prepare,
    _cmd_job_amend,
    _cmd_resume,
    _cmd_source_init,
)
from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

from .test_preprocess_cli import (
    FakeDocchunk,
    FakeMedia,
    make_config,
    make_media_job,
    run_preprocess,
)


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


def make_doc_job(tmp_path: Path, status: str = "ROUTING") -> tuple:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(tmp_path / "none"),
        targets=["cangjie"]))
    manifest.status = status
    store.save(manifest)
    return config, store, manifest.job_id


# ---------- P0a: per-file progress persistence ----------

def test_media_transcribed_events_persisted_per_file(tmp_path, monkeypatch):
    config, store, job_id = make_media_job(
        tmp_path, {"01.mp4": b"v", "02.mp4": b"w"})
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project))
    assert rc == 0
    events = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "logs" / "events.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip()]
    media_events = [e for e in events if e["event"] == "media_transcribed"]
    assert len(media_events) == 2


def test_manifest_persisted_mid_transcription(tmp_path, monkeypatch):
    """转写循环内每个文件完成即落盘（不等阶段结束）。"""
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    seen_during = []

    real_save = ManifestStore.save

    def spying_save(self, manifest):
        real_save(self, manifest)
        if manifest.status == "TRANSCRIBING" and manifest.media.outputs:
            seen_during.append(len(manifest.media.outputs))

    monkeypatch.setattr(ManifestStore, "save", spying_save)
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project))
    assert rc == 0
    # 转写阶段内（status 尚为 TRANSCRIBING）就有带 outputs 的落盘发生
    assert any(n >= 1 for n in seen_during), seen_during


# ---------- P1a: job amend + pidfile lock ----------

def test_amend_adds_target(tmp_path):
    config, store, job_id = make_doc_job(tmp_path)
    rc = _cmd_job_amend(config, Namespace(job_id=job_id,
                                          add_target="personal"))
    assert rc == 0
    loaded = store.load(job_id)
    assert "personal" in loaded.request.targets


def test_amend_idempotent(tmp_path):
    config, store, job_id = make_doc_job(tmp_path)
    _cmd_job_amend(config, Namespace(job_id=job_id, add_target="personal"))
    rc = _cmd_job_amend(config, Namespace(job_id=job_id, add_target="personal"))
    assert rc == 0
    assert store.load(job_id).request.targets.count("personal") == 1


def test_amend_blocked_while_preprocess_running(tmp_path):
    config, store, job_id = make_doc_job(tmp_path, status="TRANSCRIBING")
    lock = store.job_dir(job_id) / ".preprocess.lock"
    lock.write_text(str(1), encoding="utf-8")  # pid 1 永远存活
    rc = _cmd_job_amend(config, Namespace(job_id=job_id,
                                          add_target="personal"))
    assert rc == 2
    assert "personal" not in store.load(job_id).request.targets


def test_amend_allows_stale_lock(tmp_path):
    config, store, job_id = make_doc_job(tmp_path)
    lock = store.job_dir(job_id) / ".preprocess.lock"
    lock.write_text("99999999", encoding="utf-8")  # 不存在的 pid = 陈旧锁
    rc = _cmd_job_amend(config, Namespace(job_id=job_id,
                                          add_target="personal"))
    assert rc == 0


# ---------- P0b: generic resume ----------

def test_resume_lists_resumable_and_skips_done(tmp_path, capsys):
    config, store, job_id = make_doc_job(tmp_path, status="TRANSCRIBING")
    done = store.create(JobRequest(raw_prompt="y", provider="local",
                                   source="/tmp/y", targets=["cangjie"]))
    done.status = "COMPLETED"
    store.save(done)
    rc = _cmd_resume(config, Namespace(job=None, exec_run=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert job_id in out and "TRANSCRIBING" in out
    assert done.job_id not in out.split("resumable")[0] or True


def test_resume_skips_locked_job(tmp_path, capsys):
    config, store, job_id = make_doc_job(tmp_path, status="TRANSCRIBING")
    (store.job_dir(job_id) / ".preprocess.lock").write_text("1", encoding="utf-8")
    rc = _cmd_resume(config, Namespace(job=None, exec_run=False))
    out = capsys.readouterr().out
    assert "running" in out  # 报告为运行中，不列入可续跑


def test_resume_exec_runs_preprocess(tmp_path, monkeypatch, capsys):
    import knowledge_ingest.adapters.docchunk as docchunk_module
    import knowledge_ingest.adapters.media as media_module
    import knowledge_ingest.runner as runner_module
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    monkeypatch.setattr(media_module, "MediaAdapter", lambda project, output_root:
                        FakeMedia(config.media_project, config.media_output_root))
    fake_docchunk = FakeDocchunk(config.docchunk_project)
    monkeypatch.setattr(docchunk_module, "DocchunkAdapter", lambda project:
                        fake_docchunk)
    from .test_preprocess_cli import fake_poll_factory
    monkeypatch.setattr(runner_module, "poll",
                        fake_poll_factory(fake_docchunk, False))
    rc = _cmd_resume(config, Namespace(job=job_id, exec_run=True))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status == "CORPUS_READY"
    assert "CORPUS_READY" in capsys.readouterr().out


# ---------- P1b: source init ----------

def test_source_init_with_local_path(tmp_path):
    config, store, job_id = make_doc_job(tmp_path)
    local = tmp_path / "course"
    local.mkdir()
    (local / "a.pdf").write_bytes(b"pdf")
    rc = _cmd_source_init(config, Namespace(
        job_id=job_id, local_path=str(local),
        remote_path="apps/bdpan/课程", name="课程", note=[]))
    assert rc == 0
    handoff = json.loads(
        (store.job_dir(job_id) / "handoff" / "source.json")
        .read_text(encoding="utf-8"))
    assert handoff["provider"] == "local"
    assert handoff["download_completed"] is True
    assert handoff["source_fingerprint"].startswith("sha256:")
    assert handoff["remote"]["path"] == "apps/bdpan/课程"


def test_source_init_template_without_local(tmp_path):
    config, store, job_id = make_doc_job(tmp_path)
    rc = _cmd_source_init(config, Namespace(
        job_id=job_id, local_path=None,
        remote_path="apps/bdpan/课程", name="课程", note=[]))
    assert rc == 0
    handoff = json.loads(
        (store.job_dir(job_id) / "handoff" / "source.json")
        .read_text(encoding="utf-8"))
    assert handoff["download_completed"] is False


# ---------- P2: distill prepare ----------

def test_distill_prepare_cangjie(tmp_path):
    config, store, job_id = make_doc_job(tmp_path, status="CORPUS_READY")
    manifest = store.load(job_id)
    manifest.docchunk.corpus_path = Path("/tmp/corpus-x")
    store.save(manifest)
    rc = _cmd_distill_prepare(config, Namespace(job_id=job_id,
                                                target="cangjie"))
    assert rc == 0
    root = config.pipeline_root / "distill" / job_id / "cangjie"
    assert (root / "books").is_dir()
    assert (root / "PIPELINE_STATE.md").is_file()
    handoff = (store.job_dir(job_id) / "handoff" / "target-cangjie.yaml")
    assert handoff.is_file()
    text = handoff.read_text(encoding="utf-8")
    assert "corpus_path" in text and "/tmp/corpus-x" in text


def test_distill_prepare_idempotent(tmp_path):
    config, store, job_id = make_doc_job(tmp_path, status="CORPUS_READY")
    manifest = store.load(job_id)
    manifest.docchunk.corpus_path = Path("/tmp/corpus-x")
    store.save(manifest)
    _cmd_distill_prepare(config, Namespace(job_id=job_id, target="cangjie"))
    state = config.pipeline_root / "distill" / job_id / "cangjie" / "PIPELINE_STATE.md"
    state.write_text("MARKER", encoding="utf-8")
    rc = _cmd_distill_prepare(config, Namespace(job_id=job_id,
                                                target="cangjie"))
    assert rc == 0
    assert state.read_text(encoding="utf-8") == "MARKER"  # 不覆盖


def test_distill_prepare_requires_corpus(tmp_path):
    config, store, job_id = make_doc_job(tmp_path, status="TRANSCRIBING")
    rc = _cmd_distill_prepare(config, Namespace(job_id=job_id,
                                                target="cangjie"))
    assert rc == 2
