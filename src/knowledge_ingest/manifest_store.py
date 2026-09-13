"""Durable, atomically written Job Manifest store."""

from __future__ import annotations

import fcntl
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

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
    return datetime.now(UTC)


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
                     f"acquired_at={_now().isoformat()}".encode())
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


# 规格 15：job_id 追加 6 位 uuid hex（同秒同 provider 同 slug 不碰撞）
JOB_ID_SUFFIX_LEN = 6
V1_BACKUP_SUFFIX = ".bak-v1"


class V1BackupError(RuntimeError):
    """v1 备份已存在但与待备份原始字节不一致（fail-fast，绝不覆盖）。"""


def _read_raw_if_v1(path: Path) -> bytes | None:
    """磁盘文件是 v1 manifest 时返回原始 bytes，否则 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # v1：schema_version != 2（含缺失）；v2 文件恒为 2
    if data.get("schema_version") == 2:
        return None
    return raw


def backup_v1_manifest(path: Path, raw: bytes) -> None:
    """规格（V1→V2 迁移）：首次持久化前把原始 bytes 用 O_CREAT|O_EXCL 写
    job.yaml.bak-v1 并 fsync；EEXIST → 校验已有备份完整（可解析且与预期
    逐字节一致），不一致 fail-fast，绝不覆盖。"""
    backup = path.with_name(path.name + V1_BACKUP_SUFFIX)
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        existing = backup.read_bytes()
        try:
            parsed = yaml.safe_load(existing.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, dict) or existing != raw:
            raise V1BackupError(
                f"existing v1 backup is incomplete or divergent, "
                f"refusing to touch: {backup}") from None
        return
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


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
        job_id = (f"{now:%Y%m%d-%H%M%S}-{request.provider}"
                  f"-{_slugify(stem)}-{uuid.uuid4().hex[:JOB_ID_SUFFIX_LEN]}")
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
        path = self.manifest_path(manifest.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 规格（V1→V2 迁移）：首次 v1→v2 持久化前，原 job.yaml 原始 bytes
        # → job.yaml.bak-v1（O_CREAT|O_EXCL + fsync）；绝不覆盖。
        raw_v1 = _read_raw_if_v1(path)
        if raw_v1 is not None:
            backup_v1_manifest(path, raw_v1)
        manifest.updated_at = _now()
        payload = manifest.model_dump(mode="json")
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        atomic_write_text(path, text)

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
