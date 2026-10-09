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
    return hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()[:EXTERNAL_FINGERPRINT_LEN]


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
        "lookup_json_fields": ["found", "external_id", "job_id", "status", "contract_version"],
    }


# ---------- request identity（等价判断；prompt 先脱敏） ----------


def request_identity(request: JobRequest) -> tuple:
    """用于「同一 external-id 是否同一意图」的等价判断。

    prompt 在 CLI 侧已经过 `redact_text`（落盘前脱敏），这里比较的是
    **脱敏后**的字符串，避免用明文 prompt 参与判断而泄漏。
    """
    return (request.provider, request.source, tuple(request.targets), request.raw_prompt)


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
    fd = os.open(marker_path(job_dir), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
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
    return (
        isinstance(payload, dict)
        and payload.get("external_id_sha256") == external_fingerprint(external_id)
        and payload.get("protocol_version") == EXTERNAL_PROTOCOL_VERSION
    )


# ---------- source register 幂等（§2.5） ----------

#: `source` 中由**下游派生**的键：注册时本地 handoff 还没有它们，
#: 因此必须排除在等价判断之外，否则同一次注册永远"不相等"。
DERIVED_SOURCE_KEYS = frozenset({"source_fingerprint"})

#: 注册成功凭据在 `manifest.source` 里的键（Issue #39 §1）。
#: 只在**验证通过**的成功路径写入；失败的注册**绝不**伪造它。
REGISTRATION_RECEIPT_KEY = "_registration_receipt"
#: 凭据协议版本（与 external-job contract 解耦，单独演进）
REGISTRATION_RECEIPT_VERSION = 1

#: 参与等价判断的 handoff 稳定字段（Issue #37 §4）。
#: 刻意**不**包含 `local_path`：它随临时目录变化，不是稳定身份。
HANDOFF_IDENTITY_FIELDS = (
    ("schema_version",),
    ("provider",),
    ("remote", "id"),
    ("remote", "name"),
    ("remote", "size_bytes"),
    ("remote", "mtime"),
    ("remote", "path"),
    ("local_path_present",),
    ("download_completed",),
    ("provenance", "source_id"),
    ("provenance", "chat_id"),
    ("provenance", "message_ids"),
    ("provenance", "sender_id"),
    ("provenance", "first_message_at"),
    ("provenance", "last_message_at"),
    ("provenance", "source_deleted"),
)


def _dig(mapping, path: tuple):
    node = mapping
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _normalise(value):
    """bool 与 int 不可混同；容器递归规范化，使键序差异不影响等价性。"""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, str) or value is None:
        return ("scalar", value)
    if isinstance(value, list):
        return ("list", tuple(_normalise(v) for v in value))
    if isinstance(value, dict):
        return ("dict", tuple(sorted((str(k), _normalise(v)) for k, v in value.items())))
    return ("other", repr(value))


def handoff_identity(handoff: dict) -> tuple:
    """handoff 的等价判断键（Issue #37 §4 加固）。

    旧实现只取 `(local_path, remote.id, message_ids)`，导致：

    - **同路径内容更新** -> 误判为同一 payload（local_path 没变）；
    - `size_bytes` / `mtime` / `source_deleted` / `message_ids` / `chat_id`
      等变化 -> 一律被忽略。

    现在取 handoff 的**稳定 canonical 字段**（不含任何正文），并：

    - 排除下游派生键 `source_fingerprint`（注册时本地还没有）；
    - 用 `local_path_present` 代替 `local_path` 绝对路径本身
      （路径随环境变化，不该参与身份）；
    - 递归规范化，使 producer 的键序/序列化差异不影响等价性。

    同路径内容变化只要伴随 size/mtime 变化即可被发现；若两者都不变，
    该 handoff 本身就没有携带任何可区分信息 —— 见
    `source_path_content_changed` 的兜底说明。
    """
    if not isinstance(handoff, dict):
        return ("<non-mapping>",)
    parts = []
    for field in HANDOFF_IDENTITY_FIELDS:
        if field == ("local_path_present",):
            value = bool(handoff.get("local_path"))
        else:
            value = _dig(handoff, field)
        parts.append((field, _normalise(value)))
    return tuple(parts)


def source_without_derived(handoff: dict) -> dict:
    """剥离下游派生键后的 handoff 视图（用于与 manifest.source 比对）。"""
    if not isinstance(handoff, dict):
        return {}
    return {k: v for k, v in handoff.items() if k not in DERIVED_SOURCE_KEYS}


def source_without_receipt(source) -> dict:
    """剥离下游派生键**与**注册凭据后的 source 视图。

    凭据本身不能参与自己的完整性校验，否则会自指。
    """
    stripped = source_without_derived(source)
    stripped.pop(REGISTRATION_RECEIPT_KEY, None)
    return stripped


def source_digest(source) -> str:
    """source 的稳定摘要（凭据用它绑定「这正是被接受过的那份 handoff」）。"""
    import hashlib

    canonical = json.dumps(
        source_without_receipt(source),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def registration_receipt(manifest: JobManifest) -> dict | None:
    """读取 manifest 上的注册成功凭据；不存在或格式不符返回 None。"""
    source = manifest.source
    if not isinstance(source, dict):
        return None
    receipt = source.get(REGISTRATION_RECEIPT_KEY)
    if not isinstance(receipt, dict):
        return None
    if receipt.get("validated") is not True:
        return None
    if receipt.get("protocol_version") != REGISTRATION_RECEIPT_VERSION:
        return None
    return receipt


def receipt_matches_source(manifest: JobManifest) -> bool:
    """凭据是否确实绑定在当前这份 source 上（防篡改 / 防错配）。"""
    receipt = registration_receipt(manifest)
    if receipt is None:
        return False
    return receipt.get("handoff_sha256") == source_digest(manifest.source)


def build_registration_receipt(manifest: JobManifest) -> dict:
    """构造（但不写入）当前 source 的成功注册凭据。"""
    return {
        "validated": True,
        "protocol_version": REGISTRATION_RECEIPT_VERSION,
        "handoff_sha256": source_digest(manifest.source),
    }


def already_registered(manifest: JobManifest, handoff: dict, *, proven: bool = False) -> bool:
    """该 Job 是否**可证明**曾成功注册过完全相同的 handoff（Issue #39 §1）。

    两个条件缺一不可：

    1. `manifest.source` 与传入 handoff 的身份三元组等价（内容/范围一致）；
    2. `proven=True` —— 调用方已确认存在**成功注册凭据**（receipt 或旧
       manifest 的 `source_registered` 事件）。

    只有 (1) 时**不能**判定已注册：`manifest.source` 在验证失败时也会被写入
    （随后 BLOCKED），那时重放必须返回非 0，而不是伪装 `ALREADY_REGISTERED`。
    """
    if not proven:
        return False
    existing = manifest.source
    if not isinstance(existing, dict) or not existing:
        return False
    return handoff_identity(existing) == handoff_identity(handoff)
