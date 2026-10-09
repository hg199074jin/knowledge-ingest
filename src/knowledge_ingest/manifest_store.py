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

from knowledge_ingest.external_jobs import (
    EXTERNAL_LOCK_DIRNAME,
    ExternalIdConflictError,
    IncompleteIdempotentJobError,
    external_fingerprint,
    external_job_id,
    manifest_identity,
    marker_matches,
    request_identity,
    write_marker,
)
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


class LockedSession:
    """`ManifestStore.locked()` 的会话对象（Issue #37 §2）。

    显式 `commit()` 才触发写回 —— 这正是 ACK 重放"零写入"所需要的语义。
    不能用 `@contextmanager` 内的普通局部布尔量：调用方 `changed = True`
    只会改调用方自己的 frame，生成器帧永远看不到。
    """

    __slots__ = ("_job_id", "_store", "committed", "manifest")

    def __init__(self, *, manifest: JobManifest, store: ManifestStore,
                 job_id: str):
        self.manifest = manifest
        self._store = store
        self._job_id = job_id
        self.committed = False

    def commit(self) -> None:
        """声明本次需要写回（离开 with 块时执行 validate + atomic save）。"""
        self.committed = True

    @property
    def job_id(self) -> str:
        return self._job_id


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

    # ---------- 跨仓库 external-id 幂等（Issue #35） ----------

    def external_lock_path(self, external_id: str) -> Path:
        """跨进程锁：位于 jobs 内的 non-job 锁根（不占用任何 job 目录）。"""
        job_id = external_job_id(external_id)
        return self.jobs_root / EXTERNAL_LOCK_DIRNAME / f"{job_id}.lock"

    def create_idempotent(self, request: JobRequest, *,
                          _kill_after_manifest: bool = False) -> JobManifest:
        """按 external_id 原子创建/复用 Job（可重放、并发安全）。

        强默认：确定性 job_id + 跨进程 flock + 受控状态 marker。

        - 首次：创建目录树、写 marker（O_EXCL 证明归属）、落 manifest；
        - 重试（manifest 已存在且意图一致）：返回既有 Job，绝不新建；
        - 同键不同意图：ExternalIdConflictError（不隐式复用）；
        - 目录已存在但无法证明归属（无 marker / marker 不匹配 /
          manifest 不可解析）：IncompleteIdempotentJobError，
          **不覆盖、不删除**任何既有内容。

        `_kill_after_manifest` 仅供故障注入测试（SIGKILL 窗口）使用。
        """
        external_id = request.external_id
        if not external_id:
            raise ValueError(
                "create_idempotent requires JobRequest.external_id")
        job_id = external_job_id(external_id)
        with flock_ctx(self.external_lock_path(external_id),
                       timeout=MANIFEST_LOCK_TIMEOUT):
            manifest_path = self.manifest_path(job_id)
            if manifest_path.is_file():
                manifest = self._load_or_block(job_id, external_id)
                if request_identity(request) != manifest_identity(manifest):
                    raise ExternalIdConflictError(
                        f"{external_fingerprint(external_id)}: external-id "
                        f"already bound to a different request "
                        f"(job {job_id}); refusing to reuse")
                return manifest

            job_dir = self.job_dir(job_id)
            if job_dir.exists():
                # 受控状态？-> 有界恢复；否则 STOP，绝不覆盖未知内容
                if not marker_matches(job_dir, external_id):
                    raise IncompleteIdempotentJobError(
                        f"{external_fingerprint(external_id)}: job dir exists "
                        f"but is not provably owned by this external-id; "
                        f"refusing to overwrite (job {job_id})")
            else:
                job_dir.mkdir(parents=True, exist_ok=True)
                write_marker(job_dir, external_id)

            for sub in ("source", "handoff/document-set", "reports", "logs"):
                (job_dir / sub).mkdir(parents=True, exist_ok=True)
            manifest = JobManifest(
                job_id=job_id, created_at=_now(), updated_at=_now(),
                request=request)
            self.save(manifest)
            if _kill_after_manifest:
                # 故障注入：manifest 已落盘但调用方未收到响应即被杀
                os.kill(os.getpid(), 9)
            return manifest

    def _load_or_block(self, job_id: str, external_id: str) -> JobManifest:
        """读取 manifest；不可解析时 fail-closed（不静默重建）。"""
        try:
            return self.load(job_id)
        except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
            raise IncompleteIdempotentJobError(
                f"{external_fingerprint(external_id)}: existing manifest for "
                f"job {job_id} is unreadable ({type(exc).__name__}); refusing "
                f"to overwrite") from None

    def get_by_external_id(self, external_id: str) -> JobManifest | None:
        """只读查找：绝不 migration / 建 manifest / 改 mtime / 推进 Job。"""
        if not external_id:
            return None
        job_id = external_job_id(external_id)
        manifest_path = self.manifest_path(job_id)
        if not manifest_path.is_file():
            return None
        try:
            manifest = self.load(job_id)
        except (FileNotFoundError, ValueError, yaml.YAMLError):
            return None
        # 防冒名：目录归属必须与本次 external-id 一致
        if manifest.request.external_id != external_id:
            return None
        return manifest

    def load(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"job not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return JobManifest.model_validate(data)

    @contextmanager
    def locked(self, job_id: str) -> Iterator[LockedSession]:
        """在同一 per-job flock 内读取，并**按需**写回（Issue #37 §2）。

        与 `edit` 的关键区别：**不 commit 就没有任何写入**。ACK 丢失重放必须
        在同一锁内做最终判定，判定为"已注册"时**严格零写入**（mtime /
        bytes / events 全不变）—— 而 `edit` 会在 contextmanager 退出时无条件
        `save()`，不能用于重放路径。

        用法::

            with store.locked(job_id) as session:
                if already_registered(session.manifest, handoff):
                    return                 # 零写入
                session.manifest.source = handoff
                session.commit()           # 显式写回

        未调用 `commit()` 时离开 with 块不会落盘。
        """
        with flock_ctx(self._manifest_lock(job_id),
                       timeout=MANIFEST_LOCK_TIMEOUT):
            session = LockedSession(manifest=self.load(job_id), store=self,
                                    job_id=job_id)
            yield session
            if not session.committed:
                return
            manifest = session.manifest
            JobManifest.model_validate(manifest.model_dump(mode="json"))
            self.save(manifest)

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
