"""CLI-level preprocess tests: the orchestration layer with fake adapters.

Covers the branches acceptance-v1.md claimed: media_failed, corpus_verify_failed,
split failure, corpus reuse, stem conflict, provenance relative paths.
"""

from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import knowledge_ingest.adapters.docchunk as docchunk_module
import knowledge_ingest.adapters.media as media_module
import knowledge_ingest.runner as runner_module
from knowledge_ingest.adapters.media import TranscriptResult
from knowledge_ingest.cache import build_transcript_cache_key
from knowledge_ingest.cli import _cmd_preprocess, _cmd_route, _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.fingerprint import fingerprint_file
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest
from knowledge_ingest.runner import CommandResult


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus-root"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive", "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


class FakeDocchunk:
    def __init__(self, project: Path, split_fail: bool = False,
                 verify_ok: bool = True) -> None:
        self.project = project
        self.split_fail = split_fail
        self.verify_ok = verify_ok
        self.split_calls = 0
        self._corpus: Path | None = None

    def head(self) -> str:
        return "fakehead"

    def split_task(self, input_path: Path, corpus_root: Path | None = None,
                   log_path: Path | None = None):
        import uuid
        self.split_calls += 1
        if corpus_root is None:
            corpus_root = self.project / "corpus"
        corpus = Path(corpus_root) / f"corpus-{uuid.uuid4().hex[:12]}"
        task = runner_module.RunningTask(
            argv=("docchunk", "split"), cwd=self.project,
            log_path=Path(log_path or "/tmp/fake.log"),
            popen=None,  # type: ignore[arg-type]
            started_at=datetime.now(timezone.utc))
        task._fake_corpus = corpus  # type: ignore[attr-defined]
        return task

    def verify(self, corpus: Path, timeout: int = 300) -> bool:
        return self.verify_ok


def fake_poll_factory(docchunk: FakeDocchunk, fail: bool):
    def fake_poll(task):
        corpus: Path = task._fake_corpus  # type: ignore[attr-defined]
        corpus.mkdir(parents=True, exist_ok=True)
        (corpus / "manifest.json").write_text("{}", encoding="utf-8")
        (corpus / "index.jsonl").write_text("", encoding="utf-8")
        docchunk._corpus = corpus
        return CommandResult(
            argv=task.argv, returncode=1 if fail else 0,
            stdout=str(corpus), stderr="",
            started_at=task.started_at,
            ended_at=datetime.now(timezone.utc))
    return fake_poll


class FakeMedia:
    def __init__(self, project: Path, output_root: Path,
                 fail: bool = False) -> None:
        self.project = project
        self.output_root = Path(output_root)
        self.fail = fail
        self.transcribe_calls = 0

    def head(self) -> str:
        return "fakehead"

    def transcribe(self, path: Path, device: str = "auto",
                   timestamp: str = "10m", glossary=None, hotwords=None,
                   config_file=None, timeout=None) -> TranscriptResult:
        self.transcribe_calls += 1
        if self.fail:
            raise RuntimeError("asr boom")
        source = Path(path).resolve()
        md = self.output_root / source.stem / f"{source.stem}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(f"# transcript {source.stem}", encoding="utf-8")
        metadata = self.output_root / source.stem / "metadata.yaml"
        metadata.write_text("source: x\n", encoding="utf-8")
        source_sha = fingerprint_file(source)
        return TranscriptResult(
            source_path=source, markdown_path=md, metadata_path=metadata,
            source_sha256=source_sha,
            transcript_sha256=fingerprint_file(md),
            cache_key=build_transcript_cache_key(
                source_sha256=source_sha, mt_head="fakehead", config_sha=None,
                device=device, timestamp=timestamp, glossary=None,
                hotwords=None))


def make_media_job(tmp_path: Path, media_layout: dict[str, bytes]) -> tuple:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    source = tmp_path / "source"
    for rel, content in media_layout.items():
        p = source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(source),
        targets=["cangjie"]))
    handoff = tmp_path / "source.json"
    import json
    handoff.write_text(json.dumps({
        "schema_version": 1, "provider": "local", "remote": None,
        "local_path": str(source), "download_completed": True,
        "source_notes": []}), encoding="utf-8")
    _cmd_source_register(config, Namespace(job_id=manifest.job_id,
                                           handoff=str(handoff)))
    _cmd_route(config, Namespace(job_id=manifest.job_id, excludes=[]))
    return config, store, manifest.job_id


def run_preprocess(config, job_id, monkeypatch, fake_media, fake_docchunk,
                   fail_split=False):
    monkeypatch.setattr(media_module, "MediaAdapter", lambda project, output_root:
                        fake_media)
    monkeypatch.setattr(docchunk_module, "DocchunkAdapter", lambda project:
                        fake_docchunk)
    monkeypatch.setattr(runner_module, "poll",
                        fake_poll_factory(fake_docchunk, fail_split))
    return _cmd_preprocess(config, Namespace(job_id=job_id))


def test_media_failure_blocks(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    fake_media = FakeMedia(config.media_project, config.media_output_root,
                           fail=True)
    rc = run_preprocess(config, job_id, monkeypatch, fake_media,
                        FakeDocchunk(config.docchunk_project))
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "media_failed"
    assert loaded.media.status == "failed"


def test_verify_fail_blocks(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project, verify_ok=False))
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "corpus_verify_failed"
    assert loaded.docchunk.verify == "FAIL"


def test_split_failure_blocks(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project), fail_split=True)
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "docchunk_split_failed"


def test_corpus_cache_reused_on_second_run(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    fake_media = FakeMedia(config.media_project, config.media_output_root)
    fake_docchunk = FakeDocchunk(config.docchunk_project)
    rc1 = run_preprocess(config, job_id, monkeypatch, fake_media, fake_docchunk)
    assert rc1 == 0
    first_calls = fake_media.transcribe_calls
    # 模拟中断于 DOCCHUNKING（split 已入缓存、verify 未完成）后的重入
    resumed = store.load(job_id)
    resumed.status = "DOCCHUNKING"
    store.save(resumed)
    rc2 = run_preprocess(config, job_id, monkeypatch, fake_media, fake_docchunk)
    assert rc2 == 0
    loaded = store.load(job_id)
    assert loaded.status == "CORPUS_READY"
    assert loaded.docchunk.reused is True
    assert fake_media.transcribe_calls == first_calls  # 二次运行零 ASR
    assert fake_docchunk.split_calls == 1              # 零重复 split


def test_media_stem_conflict_blocks(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(
        tmp_path, {"A/01.mp4": b"1", "B/01.mp4": b"2"})
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project))
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "media_stem_conflict"


def test_media_output_keeps_relative_provenance(tmp_path: Path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"sub/01.mp4": b"v"})
    rc = run_preprocess(config, job_id, monkeypatch,
                        FakeMedia(config.media_project, config.media_output_root),
                        FakeDocchunk(config.docchunk_project))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.media.outputs[0].source_relative_path == "sub/01.mp4"
