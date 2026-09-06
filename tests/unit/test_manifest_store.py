from datetime import datetime
from pathlib import Path

import pytest

from knowledge_ingest.manifest_store import ManifestStore, atomic_write_text
from knowledge_ingest.models import JobManifest, JobRequest


@pytest.fixture()
def store(tmp_path: Path) -> ManifestStore:
    return ManifestStore(jobs_root=tmp_path / "jobs")


@pytest.fixture()
def job_request() -> JobRequest:
    return JobRequest(
        raw_prompt="处理本地课程",
        provider="local",
        source="/tmp/课程",
        targets=["cangjie"],
    )


def test_create_writes_manifest_and_dirs(
    store: ManifestStore, job_request: JobRequest
):
    manifest = store.create(job_request)
    job_dir = store.job_dir(manifest.job_id)
    assert (job_dir / "job.yaml").is_file()
    assert (job_dir / "source").is_dir()
    assert (job_dir / "handoff" / "document-set").is_dir()
    assert (job_dir / "reports").is_dir()
    assert (job_dir / "logs").is_dir()
    assert manifest.status == "CREATED"
    assert manifest.request.provider == "local"


def test_save_and_reload_roundtrip(
    store: ManifestStore, job_request: JobRequest
):
    created = store.create(job_request)
    created.status = "ROUTING"
    store.save(created)
    loaded = store.load(created.job_id)
    assert loaded.status == "ROUTING"
    assert loaded.request.raw_prompt == job_request.raw_prompt


def test_save_is_atomic_on_failure(
    store: ManifestStore,
    job_request: JobRequest,
    monkeypatch: pytest.MonkeyPatch,
):
    created = store.create(job_request)
    path = store.job_dir(created.job_id) / "job.yaml"
    before = path.read_text(encoding="utf-8")

    import yaml

    def broken_dump(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(yaml, "safe_dump", broken_dump)
    with pytest.raises(RuntimeError):
        store.save(created)
    assert path.read_text(encoding="utf-8") == before
    assert not list(store.job_dir(created.job_id).glob(".job.yaml.tmp*"))


def test_atomic_write_text_preserves_old_content(tmp_path: Path):
    target = tmp_path / "data.txt"
    atomic_write_text(target, "v1")
    atomic_write_text(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"
    assert not list(tmp_path.glob(".data.txt.tmp"))


def test_load_missing_job_raises(store: ManifestStore):
    with pytest.raises(FileNotFoundError):
        store.load("20990101-000000-local-none")


def test_datetimes_and_paths_roundtrip(
    store: ManifestStore, job_request: JobRequest
):
    manifest = store.create(job_request)
    manifest.docchunk.corpus_path = Path("/tmp/corpus")
    manifest.cangjie.waiting_for = "stage0_overview"
    store.save(manifest)
    loaded = store.load(manifest.job_id)
    assert isinstance(loaded.created_at, datetime)
    assert loaded.docchunk.corpus_path == Path("/tmp/corpus")
    assert loaded.cangjie.waiting_for == "stage0_overview"
    assert isinstance(loaded, JobManifest)
