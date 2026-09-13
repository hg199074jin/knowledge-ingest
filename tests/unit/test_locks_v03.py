"""v0.3 A3：真互斥锁 + 事务式 manifest mutate + 锁语义测试。

冻结规格 v0.3.0 FROZEN v7 条文 3/6：
- `.preprocess.lock`（业务互斥）：EXCLUSIVE + NON-BLOCKING，拿不到 → 打印
  "another preprocess holds this job: ..." 并退出码 3（不重试不等待）
- manifest / transcript-cache-index（数据一致性）：EXCLUSIVE + blocking（有界等待
  默认 30 秒）；产生更新依据的 read 必须在锁内（read-modify-write 同一临界区）
- 锁文件内容写 PID+时间戳，仅作诊断展示；判活用 flock 探测（能拿到=无活进程）
- edit(job_id)：flock → reload latest → yield → validate → atomic write
- save_section(manifest, sections)：锁内 reload latest，仅覆盖指定 section
- 禁止 manifest 与 cache 两把锁嵌套获取（cache put 在 manifest 锁外）
"""

import fcntl
import os
import threading
from argparse import Namespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from knowledge_ingest.cli import _cmd_preprocess, _lock_alive
from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

from .test_preprocess_cli import make_config


@pytest.fixture()
def store(tmp_path: Path) -> ManifestStore:
    return ManifestStore(jobs_root=tmp_path / "jobs")


@pytest.fixture()
def job_id(store: ManifestStore) -> str:
    return store.create(JobRequest(
        raw_prompt="x", provider="local", source="/tmp/x",
        targets=["cangjie"])).job_id


def hold_flock(path: Path):
    """在测试进程内持有 path 的 EX flock，返回 (fd, release)。"""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


# ---------- flock_ctx / edit()：事务式 mutate ----------

def test_flock_ctx_conflicts_between_independent_opens(tmp_path: Path):
    from knowledge_ingest.manifest_store import LockHeld, flock_ctx

    lock = tmp_path / "some.lock"
    fd = hold_flock(lock)
    try:
        with pytest.raises(LockHeld):
            with flock_ctx(lock, blocking=False):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # 释放后可立即获取
    with flock_ctx(lock, blocking=False):
        pass


def test_flock_ctx_writes_pid_timestamp_diagnostics(tmp_path: Path):
    from knowledge_ingest.manifest_store import flock_ctx

    lock = tmp_path / "diag.lock"
    with flock_ctx(lock, blocking=False):
        content = lock.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in content
    assert "acquired_at=" in content


def test_flock_ctx_bounded_wait_times_out(tmp_path: Path):
    from knowledge_ingest.manifest_store import LockWaitTimeout, flock_ctx

    lock = tmp_path / "slow.lock"
    fd = hold_flock(lock)
    try:
        with pytest.raises(LockWaitTimeout):
            with flock_ctx(lock, blocking=True, timeout=0.2):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_edit_reloads_latest_inside_lock(
    store: ManifestStore, job_id: str
):
    # 另一写者先落盘一次修改（模拟并发 amend）
    external = store.load(job_id)
    external.request.targets.append("personal")
    store.save(external)

    with store.edit(job_id) as manifest:
        manifest.media.status = "running"

    loaded = store.load(job_id)
    assert loaded.media.status == "running"
    # reload 发生在锁内：外部写者的修改未被内存旧副本覆盖
    assert "personal" in loaded.request.targets


def test_edit_does_not_write_when_body_raises(
    store: ManifestStore, job_id: str
):
    with pytest.raises(RuntimeError):
        with store.edit(job_id) as manifest:
            manifest.media.status = "running"
            raise RuntimeError("boom")

    loaded = store.load(job_id)
    assert loaded.media.status == "pending"  # 未落盘


def test_edit_validates_before_write(store: ManifestStore, job_id: str):
    with pytest.raises(ValidationError):
        with store.edit(job_id) as manifest:
            manifest.status = "NOT_A_STATUS"

    assert store.load(job_id).status == "CREATED"


def test_edit_releases_lock_after_exit(store: ManifestStore, job_id: str):
    with store.edit(job_id) as manifest:
        manifest.media.status = "running"
    # 锁已释放：能立即再次进入
    with store.edit(job_id) as manifest:
        manifest.media.status = "success"
    assert store.load(job_id).media.status == "success"


# ---------- save_section：锁内 reload latest，仅覆盖指定 section ----------

def test_save_section_overwrites_only_named_sections(
    store: ManifestStore, job_id: str
):
    from knowledge_ingest.models import MediaOutput

    base = store.load(job_id)
    base.status = "ROUTING"
    base.media.outputs = [MediaOutput(
        source_relative_path="01.mp4", source_sha256="sha256:a",
        transcript=Path("/t/01.md"), transcript_sha256="sha256:md",
        metadata=Path("/t/01.yaml"))]
    store.save(base)

    memory = store.load(job_id)
    # 另一写者推进了 status/errors（模拟锁内并发写）
    external = store.load(job_id)
    external.status = "BLOCKED"
    external.errors.append({"reason": "external_change"})
    store.save(external)

    # 内存副本只推进 media（旧 status 仍是 ROUTING——这正是要废除的覆盖问题）
    memory.media.outputs.append(MediaOutput(
        source_relative_path="02.mp4", source_sha256="sha256:b",
        transcript=Path("/t/02.md"), transcript_sha256="sha256:md2",
        metadata=Path("/t/02.yaml")))
    store.save_section(memory, ["media"])

    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"            # 未被内存副本的旧 status 覆盖
    assert loaded.errors[-1]["reason"] == "external_change"
    assert len(loaded.media.outputs) == 2        # 指定 section 已覆盖
    assert loaded.media.outputs[1].source_relative_path == "02.mp4"


def test_save_section_rejects_unknown_section(
    store: ManifestStore, job_id: str
):
    manifest = store.load(job_id)
    with pytest.raises(ValueError):
        store.save_section(manifest, ["bogus_section"])


# ---------- _lock_alive：flock 探测判活（PID/时间戳仅诊断展示） ----------

def test_lock_alive_false_when_file_missing(tmp_path: Path):
    assert _lock_alive(tmp_path / "nope" / ".preprocess.lock") is False


def test_lock_alive_false_when_no_holder(tmp_path: Path):
    lock = tmp_path / ".preprocess.lock"
    lock.write_text("99999999", encoding="utf-8")  # 陈旧内容无持有者
    assert _lock_alive(lock) is False


def test_lock_alive_true_when_flock_held(tmp_path: Path):
    lock = tmp_path / ".preprocess.lock"
    lock.write_text("1", encoding="utf-8")  # 内容不再决定判活
    fd = hold_flock(lock)
    try:
        assert _lock_alive(lock) is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert _lock_alive(lock) is False  # 释放后 = 无活进程


def test_lock_alive_true_with_garbage_content_when_held(tmp_path: Path):
    lock = tmp_path / ".preprocess.lock"
    lock.write_text("garbage-not-a-pid", encoding="utf-8")
    fd = hold_flock(lock)
    try:
        assert _lock_alive(lock) is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------- preprocess 业务锁：EXCLUSIVE + NON-BLOCKING → exit 3 ----------

def _make_doc_job(tmp_path: Path) -> tuple[AppConfig, ManifestStore, str]:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source="/tmp/none",
        targets=["cangjie"]))
    manifest.status = "ROUTING"
    store.save(manifest)
    return config, store, manifest.job_id


def test_preprocess_lock_held_returns_3_without_waiting(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    config, store, job_id = _make_doc_job(tmp_path)
    lock = store.job_dir(job_id) / ".preprocess.lock"
    fd = hold_flock(lock)
    try:
        rc = _cmd_preprocess(config, Namespace(job_id=job_id))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert rc == 3
    err = capsys.readouterr().err
    assert "another preprocess holds this job" in err


def test_preprocess_lock_release_allows_rerun(tmp_path: Path, monkeypatch):
    import knowledge_ingest.adapters.docchunk as docchunk_module
    import knowledge_ingest.adapters.media as media_module
    import knowledge_ingest.runner as runner_module

    from .test_preprocess_cli import (
        FakeDocchunk,
        FakeMedia,
        fake_poll_factory,
    )

    config, store, job_id = _make_doc_job(tmp_path)
    # 空任务（无 media/document）会走到 nothing_to_preprocess，但锁先被拿再释放
    rc_first = _cmd_preprocess(config, Namespace(job_id=job_id))
    assert rc_first == 2  # 锁内正常执行（此处只是无东西可处理）
    assert _lock_alive(store.job_dir(job_id) / ".preprocess.lock") is False

    # 再跑一次完整 fake 流程（doc-only job 也能 CORPUS_READY）
    monkeypatch.setattr(media_module, "MediaAdapter",
                        lambda project, output_root:
                        FakeMedia(config.media_project,
                                  config.media_output_root))
    fake_docchunk = FakeDocchunk(config.docchunk_project)
    monkeypatch.setattr(docchunk_module, "DocchunkAdapter",
                        lambda project: fake_docchunk)
    monkeypatch.setattr(runner_module, "poll",
                        fake_poll_factory(fake_docchunk, False))
    manifest = store.load(job_id)
    manifest.status = "ROUTING"
    manifest.routing = {
        "collection": False,
        "discovered": {"documents": ["/tmp/none/a.md"], "media": [],
                       "unsupported": []},
        "excluded": {"documents": [], "media": [], "unsupported": []},
        "effective": {"documents": ["/tmp/none/a.md"], "media": [],
                      "unsupported": []},
        "excluded_raw": [],
    }
    store.save(manifest)
    (tmp_path / "none").mkdir()
    (tmp_path / "none" / "a.md").write_text("doc", encoding="utf-8")
    rc = _cmd_preprocess(config, Namespace(job_id=job_id))
    assert rc == 0


def test_preprocess_double_open_subprocess_exits_3(tmp_path: Path):
    """端到端：测试进程持锁后，子进程跑 preprocess 必须立即 exit 3。"""
    import json
    import subprocess

    config, store, job_id = _make_doc_job(tmp_path)
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(json.dumps({
        "pipeline_root": str(config.pipeline_root),
        "media_project": str(config.media_project),
        "docchunk_project": str(config.docchunk_project),
        "media_output_root": str(config.media_output_root),
        "docchunk_corpus_root": str(config.docchunk_corpus_root),
        "skill_roots": [str(config.skill_roots[0])],
        "skills": config.skills.model_dump(),
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    }), encoding="utf-8")

    lock = store.job_dir(job_id) / ".preprocess.lock"
    fd = hold_flock(lock)
    try:
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            ["uv", "run", "knowledge-ingest", "preprocess", job_id,
             "--config", str(config_yaml)],
            cwd=repo_root, capture_output=True, text=True, timeout=120)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert proc.returncode == 3, (
        f"stdout={proc.stdout}\nstderr={proc.stderr}")
    assert "another preprocess holds this job" in (proc.stdout + proc.stderr)


# ---------- edit() 并发：两线程同时 edit 不同 section，两次更新都存活 ----------

def test_edit_concurrent_no_lost_writes(store: ManifestStore, job_id: str):
    failures: list[Exception] = []

    def edit_section(section: str, value: str) -> None:
        try:
            for _ in range(10):
                with store.edit(job_id) as manifest:
                    getattr(manifest, section).status = value
        except Exception as exc:  # noqa: BLE001 - 透传给断言
            failures.append(exc)

    threads = [
        threading.Thread(target=edit_section, args=("media", "running")),
        threading.Thread(target=edit_section, args=("docchunk", "running")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive(), "edit() 并发死锁"

    assert not failures, failures
    loaded = store.load(job_id)
    assert loaded.media.status == "running"
    assert loaded.docchunk.status == "running"


# ---------- cache-index 锁外置 + 禁止嵌套由实现保证；此处测 put 并发不丢条目 ----------

def test_cache_concurrent_put_no_lost_entries(tmp_path: Path, monkeypatch):
    from knowledge_ingest.cache import TranscriptCache

    from .test_cache import make_entry

    index = tmp_path / "transcript-index.json"
    cache = TranscriptCache(index)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    entry_a = make_entry(tmp_path / "a", "k-a")
    entry_b = make_entry(tmp_path / "b", "k-b")

    # 注入受控竞态：两个线程都完成 read 后才允许任一 write（barrier 带超时，
    # 锁实现下第二线程被挡在 flock 处，barrier 超时破裂不致死锁）
    real_read = TranscriptCache._read
    barrier = threading.Barrier(2)

    def slow_read(self):
        data = real_read(self)
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return data

    monkeypatch.setattr(TranscriptCache, "_read", slow_read)

    errors: list[Exception] = []

    def put(entry) -> None:
        try:
            cache.put(entry)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=put, args=(entry_a,)),
               threading.Thread(target=put, args=(entry_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive(), "cache put 并发死锁"
    assert not errors, errors

    loaded = TranscriptCache(index)
    assert loaded.lookup("k-a") is not None
    assert loaded.lookup("k-b") is not None
