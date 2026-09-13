"""External-call budget guard（冻结规格 9）：两阶段 + 幂等。

- acquire（request_id 必填）：重放返回既有 permit 不重复计数；
  校验 quota（target 级）/ breaker（target+host 级）/ case-retry
  （target+case 级，attempts ≤ max_retries_per_case + 1）后原子 +1。
- outcome：幂等（同 permit 同结果重放 no-op；冲突结果拒绝）。
- breaker：success→双清零；empty→empty+1、rate 清零；rate_limit→rate+1、
  empty 清零。
- 预算上限读 handoff/target-<target>.yaml 的 budget 段；缺省用默认值。
- BLOCKED 不在这里自动恢复（规格 9 Blocker 1：显式 target resume）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

DEFAULT_BUDGET: dict = {
    "mode": "balanced",
    "max_external_calls": 20,
    "max_retries_per_case": 1,
    "breaker": {"consecutive_empty": 3, "consecutive_rate_limit": 2},
}

OUTCOMES = ("success", "empty", "rate_limit")
DENY_REASONS = ("budget_exhausted", "breaker_open", "case_retry_exceeded")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def handoff_budget_path(job_dir: Path, target: str) -> Path:
    return Path(job_dir) / "handoff" / f"target-{target}.yaml"


def read_budget_config(job_dir: Path, target: str) -> dict:
    """读 handoff 的 budget 段；文件缺失或无 budget 段 → 默认。"""
    cfg = {
        "mode": DEFAULT_BUDGET["mode"],
        "max_external_calls": DEFAULT_BUDGET["max_external_calls"],
        "max_retries_per_case": DEFAULT_BUDGET["max_retries_per_case"],
        "breaker": dict(DEFAULT_BUDGET["breaker"]),
    }
    path = handoff_budget_path(job_dir, target)
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        budget = data.get("budget") if isinstance(data, dict) else None
        if isinstance(budget, dict):
            for key in ("mode", "max_external_calls", "max_retries_per_case"):
                if budget.get(key) is not None:
                    cfg[key] = budget[key]
            breaker = budget.get("breaker")
            if isinstance(breaker, dict):
                for key in ("consecutive_empty", "consecutive_rate_limit"):
                    if breaker.get(key) is not None:
                        cfg["breaker"][key] = breaker[key]
    return cfg


def _target_entry(budget_state: dict, target: str) -> dict:
    targets = budget_state.setdefault("targets", {})
    entry = targets.setdefault(target, {})
    entry.setdefault("calls", 0)
    entry.setdefault("cases", {})
    entry.setdefault("hosts", {})
    entry.setdefault("permits", {})
    return entry


def acquire(
    manifest, job_dir: Path, *, target: str, host: str, case_id: str,
    request_id: str,
) -> dict:
    """阶段一：申请外部调用许可。返回
    {allowed: true, permit_id, call_no, replay?} 或 {allowed: false, reason}。
    """
    entry = _target_entry(manifest.budget_state, target)
    # 幂等：同 request_id 重放 → 返回既有 permit，不重复计数
    for permit_id, permit in entry["permits"].items():
        if permit.get("request_id") == request_id:
            return {
                "allowed": True,
                "permit_id": permit_id,
                "call_no": permit.get("call_no"),
                "replay": True,
            }
    cfg = read_budget_config(job_dir, target)
    # quota（target 级 calls < max_external_calls）
    if entry["calls"] >= int(cfg["max_external_calls"]):
        return {"allowed": False, "reason": "budget_exhausted"}
    # breaker（target+host 级）
    host_state = entry["hosts"].setdefault(
        host, {"consecutive_empty": 0, "consecutive_rate_limit": 0})
    if (host_state["consecutive_empty"]
            >= int(cfg["breaker"]["consecutive_empty"])
            or host_state["consecutive_rate_limit"]
            >= int(cfg["breaker"]["consecutive_rate_limit"])):
        return {"allowed": False, "reason": "breaker_open"}
    # case-retry（target+case 级：首次 1 次 + N 次 retry = 上限 N+1）
    case_state = entry["cases"].setdefault(case_id, {"attempts": 0})
    if case_state["attempts"] >= int(cfg["max_retries_per_case"]) + 1:
        return {"allowed": False, "reason": "case_retry_exceeded"}
    entry["calls"] += 1
    case_state["attempts"] += 1
    permit_id = f"bp_{uuid4().hex}"
    entry["permits"][permit_id] = {
        "target": target,
        "host": host,
        "case_id": case_id,
        "request_id": request_id,
        "acquired_at": _now_iso(),
        "outcome": None,
        "call_no": entry["calls"],
    }
    return {
        "allowed": True,
        "permit_id": permit_id,
        "call_no": entry["calls"],
        "replay": False,
    }


def find_permit(manifest, permit_id: str) -> tuple[dict | None, dict | None]:
    for entry in manifest.budget_state.get("targets", {}).values():
        permit = entry.get("permits", {}).get(permit_id)
        if permit is not None:
            return permit, entry
    return None, None


def outcome(manifest, *, permit_id: str, result: str) -> dict:
    """阶段二：回报结果（幂等）。冲突结果抛 ValueError。"""
    if result not in OUTCOMES:
        raise ValueError(
            f"result must be one of {OUTCOMES}, got {result!r}")
    permit, entry = find_permit(manifest, permit_id)
    if permit is None or entry is None:
        raise ValueError(f"unknown permit: {permit_id}")
    if permit.get("outcome") == result:
        return {"permit_id": permit_id, "result": result, "noop": True}
    if permit.get("outcome") is not None:
        raise ValueError(
            f"conflicting outcome for permit {permit_id}: "
            f"{permit['outcome']!r} != {result!r}")
    host_state = entry["hosts"].setdefault(
        permit["host"], {"consecutive_empty": 0, "consecutive_rate_limit": 0})
    if result == "success":
        host_state["consecutive_empty"] = 0
        host_state["consecutive_rate_limit"] = 0
    elif result == "empty":
        host_state["consecutive_empty"] += 1
        host_state["consecutive_rate_limit"] = 0
    else:  # rate_limit
        host_state["consecutive_rate_limit"] += 1
        host_state["consecutive_empty"] = 0
    permit["outcome"] = result
    return {"permit_id": permit_id, "result": result, "noop": False}


def amend(
    job_dir: Path, target: str, *,
    max_external_calls: int | None = None,
    max_retries_per_case: int | None = None,
) -> dict:
    """只改 handoff 里的 budget 字段，绝不恢复 target 状态（规格 9）。"""
    path = handoff_budget_path(job_dir, target)
    if not path.is_file():
        raise ValueError(
            f"target handoff not found: {path} (run distill prepare first)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    budget = data.setdefault("budget", {})
    if max_external_calls is not None:
        budget["max_external_calls"] = int(max_external_calls)
    if max_retries_per_case is not None:
        budget["max_retries_per_case"] = int(max_retries_per_case)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return budget
