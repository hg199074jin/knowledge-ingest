"""跨仓库 external-id Job API（Issue #35 / `ki-external-job-v1`）。

## 为什么需要它

拆分前 Telegram handoff 与 KI 共享进程与文件系统，能靠「先写 job_id 再
注册」拿幂等。跨仓库后这条链断了：`job create` 已落盘、调用方还没拿到
`job_id` 就崩溃时，重试既无法定位孤儿 job，也会**重复创建**。

本模块提供**确定性**的 external-id -> job_id 映射，让「同一个来源意图」在
任何时刻都指向**同一个 job 目录**，从而让 create/lookup 变得可重放。

## 设计约束（§3）

- **纯函数派生**：job_id 由 SHA-256(external_id) 确定性生成，路径安全、
  不可逆；**绝不**把 user-controlled 串直接拼进路径。
- **先写后建**：在受控状态证明归属（marker 文件）后才创建 manifest；
  遇到无法证明归属的半成品目录 -> `INCOMPLETE_IDEMPOTENT_JOB`，
  **绝不覆盖或删除**未知内容。
- **无 DB migration**：沿用现有 `ManifestStore` 的 `atomic_write_text`
  与 `flock_ctx`，不引入新持久化依赖。
- **向后兼容**：未带 `external_id` 的 Job 行为、随机 job_id、旧 manifest
  读写**完全不变**（opt-in）。

隐私：external-id 不是 token，但可能关联 Telegram 来源，因此
**任何**日志/异常/路径只出现哈希短指纹，绝不回显完整来源串。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .models import JobManifest, JobRequest

# ---------- 协议常量（machine-readable，Bridge 依此探测） ----------

PROTOCOL = "ki-external-job-v1"
EXTERNAL_PROTOCOL_VERSION = 1

#: 确定性 job_id 前缀：自解释、可与随机旧 job_id 区分
EXTERNAL_JOB_ID_PREFIX = "ext-v1-"
#: 哈希截断长度：足够抗碰撞，又保持目录名短
EXTERNAL_JOB_ID_HEX_LEN = 32
#: 日志/异常里使用的 external-id 短指纹长度
EXTERNAL_FINGERPRINT_LEN = 16

#: 受控归属证明文件（写在 job 目录内，O_EXCL 原子创建）
EXTERNAL_MARKER_NAME = ".external-id.json"
#: 跨进程锁根目录（非 job，避免半成品 job 目录被当成锁载体）
EXTERNAL_LOCK_DIRNAME = ".external-locks"

# ---------- 稳定错误码（§2.7 / §5） ----------

EXTERNAL_ID_CONFLICT = "EXTERNAL_ID_CONFLICT"
NOT_FOUND = "NOT_FOUND"
INCOMPLETE_IDEMPOTENT_JOB = "INCOMPLETE_IDEMPOTENT_JOB"
HANDOFF_CONFLICT = "HANDOFF_CONFLICT"
ALREADY_REGISTERED = "ALREADY_REGISTERED"

#: 各错误码对应的进程 exit code（machine-readable，禁止散文替代）
EXIT_CODES = {
    EXTERNAL_ID_CONFLICT: 4,
    NOT_FOUND: 3,
    INCOMPLETE_IDEMPOTENT_JOB: 5,
    HANDOFF_CONFLICT: 6,
}

# 兼容别名（错误码字符串与 exit code 同名导出，方便 CLI 直接用）
EXTERNAL_ID_CONFLICT_CODE = EXIT_CODES[EXTERNAL_ID_CONFLICT]
NOT_FOUND_CODE = EXIT_CODES[NOT_FOUND]
INCOMPLETE_IDEMPOTENT_JOB_CODE = EXIT_CODES[INCOMPLETE_IDEMPOTENT_JOB]
HANDOFF_CONFLICT_CODE = EXIT_CODES[HANDOFF_CONFLICT]


class ExternalJobError(RuntimeError):
    """external-id 协议错误基类。消息只含哈希短指纹，绝不含来源原文。"""

    code = "EXTERNAL_JOB_ERROR"
    exit_code = 2


class ExternalIdConflictError(ExternalJobError):
    code = EXTERNAL_ID_CONFLICT
    exit_code = EXIT_CODES[EXTERNAL_ID_CONFLICT]


class IncompleteIdempotentJobError(ExternalJobError):
    code = INCOMPLETE_IDEMPOTENT_JOB
    exit_code = EXIT_CODES[INCOMPLETE_IDEMPOTENT_JOB]


class HandoffConflictError(ExternalJobError):
    code = HANDOFF_CONFLICT
    exit_code = EXIT_CODES[HANDOFF_CONFLICT]


# ---------- 纯函数派生 ----------

def external_fingerprint(external_id: str) -> str:
    """external-id 的短哈希指纹（日志/错误消息专用，不可逆）。"""
    return hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()[
        :EXTERNAL_FINGERPRINT_LEN]


def external_job_id(external_id: str) -> str:
    """external_id -> 确定性 job_id。

    路径安全（只含小写十六进制与连字符）、不可逆、定长；同一键永远
    定位同一目录。注意这**不是**用户可控路径的拼接。
    """
    digest = hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()
    return f"{EXTERNAL_JOB_ID_PREFIX}{digest[:EXTERNAL_JOB_ID_HEX_LEN]}"


# ---------- 能力自证（供 Bridge 可靠探测，而非 --help 文本匹配） ----------

def capabilities() -> dict:
    """machine-readable 能力声明。Bridge 应据此判断能否启用 apply。"""
    return {
        "protocol": PROTOCOL,
        "contract_version": EXTERNAL_PROTOCOL_VERSION,
        "idempotent_create": True,
        "external_lookup": True,
        "register_idempotent": True,
        "lookup_json_fields": ["found", "external_id", "job_id", "status",
                               "contract_version"],
    }


# ---------- request identity（等价判断；prompt 先脱敏） ----------

def request_identity(request: JobRequest) -> tuple:
    """用于「同一 external-id 是否同一意图」的等价判断。

    prompt 在 CLI 侧已经过 `redact_text`（落盘前脱敏），这里比较的是
    **脱敏后**的字符串，避免用明文 prompt 参与判断而泄漏。
    """
    return (request.provider, request.source,
            tuple(request.targets), request.raw_prompt)


def manifest_identity(manifest: JobManifest) -> tuple:
    return request_identity(manifest.request)


# ---------- 受控状态 marker ----------

def marker_path(job_dir: Path) -> Path:
    return job_dir / EXTERNAL_MARKER_NAME


def write_marker(job_dir: Path, external_id: str) -> None:
    """O_EXCL 原子写归属证明（同键并发下只有一个能成功）。"""
    payload = {
        "external_id_sha256": external_fingerprint(external_id),
        "protocol_version": EXTERNAL_PROTOCOL_VERSION,
    }
    fd = os.open(marker_path(job_dir), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 0o644)
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def marker_matches(job_dir: Path, external_id: str) -> bool:
    """受控状态判定：marker 存在且哈希与本次 external-id 一致。

    返回 False 表示「无法证明属于该 external-id」-> 调用方必须拒绝覆盖。
    """
    path = marker_path(job_dir)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(payload, dict)
            and payload.get("external_id_sha256")
            == external_fingerprint(external_id)
            and payload.get("protocol_version") == EXTERNAL_PROTOCOL_VERSION)

# ---------- source register 幂等（§2.5） ----------

def handoff_identity(handoff: dict) -> tuple:
    """handoff 的等价判断键：只取稳定身份字段，不含材料正文。

    选择 local_path + remote.id + message_ids 三元组：
    - 能区分「不同来源材料」（不同 local_path / remote.id / message 区间）；
    - 不依赖 `source_fingerprint`（它是注册时**派生**出来的，尚未落盘时没有）；
    - 不把正文写进任何比较日志。
    """
    if not isinstance(handoff, dict):
        return ("<non-mapping>",)
    remote = handoff.get("remote") or {}
    prov = handoff.get("provenance") or {}
    return (
        str(handoff.get("local_path") or ""),
        str(remote.get("id") or ""),
        tuple(prov.get("message_ids") or ()),
    )


def already_registered(manifest: JobManifest, handoff: dict) -> bool:
    """该 Job 是否已注册过**完全相同**的 handoff。

    只读判断：供 `source register` 在 ACK 丢失重放时走幂等早退，
    不改 status / 不追加 event / 不写盘。
    """
    existing = manifest.source
    if not isinstance(existing, dict) or not existing:
        return False
    return handoff_identity(existing) == handoff_identity(handoff)
