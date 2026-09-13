"""Durable, atomically written Job Manifest store."""

from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

from knowledge_ingest.models import JobManifest, JobRequest

# v0.3 冻结规格 3/6：manifest 属数据一致性锁 —— EXCLUSIVE + blocking（有界等待）。
# 锁文件永不 unlink/replace（flock 绑定 inode，atomic write 会换 inode，
# 所以 manifest 锁必须落在独立的 .manifest.lock 上，而非 job.yaml 本身）。
MANIFEST_LOCK_NAME = ".manifest.lock"
MANIFEST_LOCK_TIMEOUT = 30.0


class LockHeld(RuntimeError):
    """NON-BLOCKING 尝试时锁已被他人持有。"""


class LockWaitTimeout(RuntimeError):
    """blocking 有界等待超时。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def flock_ctx(
    path: Path,
    *,
    blocking: bool = True,
    timeout: float = MANIFEST_LOCK_TIMEOUT,
) -> Iterator[int]:
    """EXCLUSIVE flock on path.

    - blocking=False：EXCLUSIVE + NON-BLOCKING，拿不到立即抛 LockHeld（业务互斥）。
    - blocking=True：有界等待，超时抛 LockWaitTimeout（数据一致性锁）。
    锁文件按需创建且释放后保留；持有期间写入 "pid=... acquired_at=..." 仅供诊断展示，
    判活一律以 flock 探测为准。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if blocking:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockWaitTimeout(
                            f"timed out after {timeout}s waiting for "
                            f"lock: {path}") from None
                    time.sleep(0.05)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockHeld(f"lock held by another process: {path}") from exc
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} "
                     f"acquired_at={_now().isoformat()}".encode("utf-8"))
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32] or "job"


class ManifestStore:
    def __init__(self, jobs_root: Path) -> None:
        self.jobs_root = Path(jobs_root)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    @property
    def manifest_path_pattern(self) -> str:
        return "job.yaml"

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.yaml"

    def create(self, request: JobRequest) -> JobManifest:
        now = _now()
        stem = Path(request.source.replace("\\", "/")).name or "source"
        job_id = f"{now:%Y%m%d-%H%M%S}-{request.provider}-{_slugify(stem)}"
        manifest = JobManifest(
            job_id=job_id, created_at=now, updated_at=now, request=request
        )
        job_dir = self.job_dir(job_id)
        (job_dir / "source").mkdir(parents=True, exist_ok=True)
        (job_dir / "handoff" / "document-set").mkdir(parents=True, exist_ok=True)
        (job_dir / "reports").mkdir(parents=True, exist_ok=True)
        (job_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.save(manifest)
        return manifest

    def load(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"job not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return JobManifest.model_validate(data)

    def save(self, manifest: JobManifest) -> None:
        manifest.updated_at = _now()
        payload = manifest.model_dump(mode="json")
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        atomic_write_text(self.manifest_path(manifest.job_id), text)

    def _manifest_lock(self, job_id: str) -> Path:
        return self.job_dir(job_id) / MANIFEST_LOCK_NAME

    @contextmanager
    def edit(self, job_id: str) -> Iterator[JobManifest]:
        """事务式 mutate（v0.3 冻结规格 6）：

        acquire per-job manifest flock → reload latest（read 必须在锁内）
        → yield 内存副本给调用方修改 → validate（pydantic 校验）→ atomic write
        → release。read-modify-write 整体在同一锁临界区；
        体内抛异常则不落盘。
        """
        with flock_ctx(self._manifest_lock(job_id),
                       timeout=MANIFEST_LOCK_TIMEOUT):
            manifest = self.load(job_id)
            yield manifest
            # validate：以 schema 全量回验后再落盘（status Literal、字段类型等）
            JobManifest.model_validate(manifest.model_dump(mode="json"))
            self.save(manifest)

    def save_section(self, manifest: JobManifest, sections: list[str]) -> None:
        """长跑 preprocess 的每步落盘：锁内 reload latest，把内存副本中指定的
        section（media/docchunk/routing/status/errors/...）覆盖到 latest 再原子写。

        废除"内存副本整体 dump"造成的覆盖问题：未指定的 section 保留磁盘上的
        最新值。等价于 update_manifest(job_id, mutator) 的事务语义。
        """
        with flock_ctx(self._manifest_lock(manifest.job_id),
                       timeout=MANIFEST_LOCK_TIMEOUT):
            latest = self.load(manifest.job_id)
            for name in sections:
                if not hasattr(latest, name):
                    raise ValueError(f"unknown manifest section: {name}")
                setattr(latest, name, getattr(manifest, name))
            self.save(latest)
            # save() 刷新的是 latest 的 updated_at；回填给调用方保持可见性一致
            manifest.updated_at = latest.updated_at

    def list_jobs(self) -> list[str]:
        if not self.jobs_root.is_dir():
            return []
        return sorted(
            p.parent.name
            for p in self.jobs_root.glob("*/job.yaml")
        )
