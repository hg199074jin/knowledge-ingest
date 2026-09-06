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


def write_handoff(tmp_path: Path, provider: str, remote_path: str,
                  local_name: str = "x.pdf",
                  download_completed: bool = True) -> Path:
    local_file = tmp_path / local_name
    local_file.write_bytes(b"pdf")
    handoff = {
        "schema_version": 1, "provider": provider,
        "remote": {"id": None, "path": remote_path, "name": "x.pdf",
                   "size_bytes": 10, "mtime": None},
        "local_path": str(local_file),
        "download_completed": download_completed,
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


def test_baidu_prefix_boundary_rejects_lookalike(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "baidu")
    handoff = write_handoff(tmp_path, "baidu", "apps/bdpan-evil/x.pdf")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    assert store.load(job_id).errors[-1]["reason"] == "baidu_scope_limited"


def test_baidu_bare_relative_path_rejected_full_path_norm(tmp_path: Path):
    """规范统一为全路径：应用目录内文件也必须写 /apps/bdpan/ 前缀。"""
    config, store, job_id = make_job(tmp_path, "baidu")
    handoff = write_handoff(tmp_path, "baidu", "审计/课程.pdf")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    assert store.load(job_id).errors[-1]["reason"] == "baidu_scope_limited"


def test_baidu_app_scope_ok(tmp_path: Path):
    for remote in ("apps/bdpan/审计/课程.pdf", "/apps/bdpan/审计/课程.pdf"):
        config, store, job_id = make_job(tmp_path, "baidu")
        handoff = write_handoff(tmp_path, "baidu", remote)
        rc = _cmd_source_register(config, Namespace(job_id=job_id,
                                                    handoff=str(handoff)))
        assert rc == 0, remote
        assert store.load(job_id).status == "DOWNLOADED"


def test_quark_register_ok(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "quark", "/审计课程/融资担保")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 0
    assert store.load(job_id).status == "DOWNLOADED"


def test_register_rejects_incomplete_download(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "quark", "/x", download_completed=False)
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    assert loaded.errors[-1]["reason"] == "source_incomplete"


def test_register_rejects_missing_local_file(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "quark", "/x")
    handoff_data = json.loads(handoff.read_text(encoding="utf-8"))
    handoff_data["local_path"] = "/tmp/definitely-missing-9f31.pdf"
    handoff.write_text(json.dumps(handoff_data), encoding="utf-8")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    assert store.load(job_id).errors[-1]["reason"] == "source_missing"


def test_register_rejects_provider_mismatch(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "baidu", "apps/bdpan/x.pdf")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    assert store.load(job_id).errors[-1]["reason"] == "provider_mismatch"


def test_register_rejects_bad_schema_version(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    handoff = write_handoff(tmp_path, "quark", "/x")
    handoff_data = json.loads(handoff.read_text(encoding="utf-8"))
    handoff_data["schema_version"] = 99
    handoff.write_text(json.dumps(handoff_data), encoding="utf-8")
    rc = _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    assert rc == 1
    assert store.load(job_id).errors[-1]["reason"] == "invalid_handoff_schema"


def test_job_create_redacts_prompt(tmp_path: Path):
    from argparse import Namespace
    from knowledge_ingest.cli import _cmd_job_create

    config, store, existing = make_job(tmp_path, "local")
    args = Namespace(provider="local", source="/tmp/课程.pdf",
                     targets=["cangjie"], prompt="处理 token=supersecret 这个")
    rc = _cmd_job_create(config, args)
    assert rc == 0
    new_jobs = [j for j in store.list_jobs() if j != existing]
    assert len(new_jobs) == 1
    manifest = store.load(new_jobs[0])
    assert "supersecret" not in manifest.request.raw_prompt
    assert "[REDACTED]" in manifest.request.raw_prompt
