import json
from argparse import Namespace
from pathlib import Path

from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive", "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


def make_job(tmp_path: Path, provider: str) -> tuple[AppConfig, ManifestStore, str]:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider=provider, source="/x", targets=["cangjie"]))
    return config, store, manifest.job_id


def write_handoff(tmp_path: Path, provider: str, remote_path: str) -> Path:
    handoff = {
        "schema_version": 1, "provider": provider,
        "remote": {"id": None, "path": remote_path, "name": "x.pdf",
                   "size_bytes": 10, "mtime": None},
        "local_path": "/tmp/x.pdf", "download_completed": True,
        "source_notes": [],
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    return path


def test_baidu_out_of_scope_blocks(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "baidu")
    handoff = write_handoff(tmp_path, "baidu", "Downloads/课程.pdf")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "baidu_scope_limited"


def test_baidu_app_scope_ok(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "baidu")
    handoff = write_handoff(tmp_path, "baidu", "apps/bdpan/审计/课程.pdf")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 0
    assert store.load(job_id).status == "DOWNLOADED"


def test_quark_register_ok(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "quark", "/审计课程/融资担保")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 0
    assert store.load(job_id).status == "DOWNLOADED"
