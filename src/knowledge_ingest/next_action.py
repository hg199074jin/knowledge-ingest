"""Next-action protocol, gates, and the target runtime operations.

`next` returns the action an agent should take; it never fabricates user
confirmation. Gates are recorded in the manifest before questions are shown.

v0.3 Part C（冻结规格 2/3/8/10/12/13）：
- 依赖决定能不能运行（depends_on 全 COMPLETED），声明顺序决定先运行谁
- target 终态不隐式复活；rejected → SKIPPED(user_rejected) 并依赖传播
- complete/start 后做依赖传播 + 聚合判定（COMPLETED/PARTIAL/FAILED/BLOCKED）
- gate 预授权两阶段：preauthorize 发 grant，resolve 引用 grant（knowledge gate
  拒绝预授权，白名单在 targets.REGISTRY.preauthorizable_gates）
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from knowledge_ingest.models import (
    ACTIVE_TARGET_STATUSES,
    TERMINAL_TARGET_STATUSES,
    JobManifest,
    TargetState,
)
from knowledge_ingest.state_machine import InvalidTransition, transition_to
from knowledge_ingest.targets import get as get_target


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def target_state(manifest: JobManifest, target: str) -> TargetState:
    state = manifest.targets.get(target)
    if state is None:
        raise ValueError(f"unknown target for this job: {target}")
    return state


# 兼容旧名
_target_state = target_state


def dependencies_satisfied(manifest: JobManifest, target: str) -> bool:
    """依赖满足 ⇔ dep.status == COMPLETED（唯一词）。"""
    state = target_state(manifest, target)
    return all(manifest.targets[d].status == "COMPLETED"
               for d in state.depends_on)


def _pending_targets(manifest: JobManifest) -> list[str]:
    """可调度 target：非终态、非 BLOCKED/RUNNING/WAITING_USER，
    且 depends_on 全 COMPLETED —— 按 manifest.targets 声明顺序。"""
    pending = []
    for name, state in manifest.targets.items():
        if state.status in TERMINAL_TARGET_STATUSES:
            continue
        if state.status in {"RUNNING", "WAITING_USER", "BLOCKED"}:
            continue
        if dependencies_satisfied(manifest, name):
            pending.append(name)
    return pending


def _waiting_target(manifest: JobManifest) -> tuple[str | None, str | None]:
    """WAITING_USER 反查：遍历 targets 找 waiting_for 非空且 WAITING_USER 的。"""
    for name, state in manifest.targets.items():
        if state.status == "WAITING_USER" and state.waiting_for:
            return name, state.waiting_for
    return None, None


def propagate_dependencies(manifest: JobManifest) -> None:
    """依赖传播（规格 2，对依赖它且尚未启动的 target，声明序迭代至不动点）：

    - dep SKIPPED → 自动 SKIPPED(reason=dependency_skipped)
    - dep FAILED  → 自动 SKIPPED(reason=dependency_failed)
    - dep BLOCKED → 保持 PENDING（可恢复，不传播终局）
    - 依赖全 COMPLETED 且尚未启动 → READY
    """
    changed = True
    while changed:
        changed = False
        for state in manifest.targets.values():
            if state.status not in {"PENDING", "READY"}:
                continue
            dep_statuses = [manifest.targets[d].status
                            for d in state.depends_on]
            if "SKIPPED" in dep_statuses:
                state.status = "SKIPPED"
                state.reason = "dependency_skipped"
                changed = True
            elif "FAILED" in dep_statuses:
                state.status = "SKIPPED"
                state.reason = "dependency_failed"
                changed = True
            elif "BLOCKED" in dep_statuses:
                continue  # 保持 PENDING，等依赖恢复
            elif state.depends_on and \
                    all(s == "COMPLETED" for s in dep_statuses) \
                    and state.status == "PENDING":
                state.status = "READY"
                changed = True


def aggregate_overall(manifest: JobManifest) -> str | None:
    """规格 12/13 聚合判定（返回新 OverallStatus 或 None=维持现状）。

    - 任一 BLOCKED 未解决 → BLOCKED（不限 requested，立即可判定）
    - 全部 requested target 已终态时：
      全 ∈ {COMPLETED, SKIPPED} → COMPLETED
      ≥1 COMPLETED 且 ≥1 FAILED → PARTIAL
      无 COMPLETED 且有 FAILED → FAILED
    - 仍有 requested target 未终态 → None（继续链式调度）
    """
    blocked = [name for name, state in manifest.targets.items()
               if state.status == "BLOCKED"]
    if blocked:
        return "BLOCKED"
    requested = [t for t in dict.fromkeys(manifest.request.targets)
                 if t in manifest.targets]
    if not requested:
        return None
    states = [manifest.targets[t] for t in requested]
    if not all(s.status in TERMINAL_TARGET_STATUSES for s in states):
        return None
    if all(s.status in {"COMPLETED", "SKIPPED"} for s in states):
        return "COMPLETED"
    completed = sum(1 for s in states if s.status == "COMPLETED")
    failed = sum(1 for s in states if s.status == "FAILED")
    if completed and failed:
        return "PARTIAL"
    if not completed and failed:
        return "FAILED"
    return None


def _finalize(manifest: JobManifest) -> bool:
    """聚合终局判定；发生 overall 跃迁返回 True。"""
    aggregate = aggregate_overall(manifest)
    if aggregate is None or aggregate == manifest.status:
        return False
    transition_to(manifest, aggregate)
    return True


def _chain_next(manifest: JobManifest) -> None:
    """链式解锁下一个 READY target（TARGET_RUNNING 自转换，守卫见
    state_machine）；没有可调度 target 则落入 TARGET_RUNNING 等待。"""
    nxt = next(iter(_pending_targets(manifest)), None)
    if manifest.status == "WAITING_USER":
        transition_to(manifest, "TARGET_RUNNING")
    if manifest.status == "TARGET_RUNNING" and nxt is not None \
            and manifest.active_target != nxt:
        transition_to(manifest, "TARGET_RUNNING", new_active_target=nxt)


def settle(manifest: JobManifest) -> None:
    """公共收敛入口：依赖传播 → 聚合判定（可能终局）→ 链式解锁下一个 READY。

    target_complete / gate_resolve(rejected) 内部走这里；
    程序化置 FAILED（无 CLI 命令）后也应调用它完成传播与聚合。
    """
    active = manifest.active_target
    if active is not None and manifest.targets[active].status \
            in TERMINAL_TARGET_STATUSES:
        manifest.active_target = None  # 终态不得为 active_target
    propagate_dependencies(manifest)
    if not _finalize(manifest):
        _chain_next(manifest)


def next_action(manifest: JobManifest) -> dict:
    status = manifest.status
    action: dict = {
        "job_id": manifest.job_id,
        "status": status,
    }

    if status == "CREATED":
        action["next_action"] = "await_source"
        action["detail"] = ("register source handoff after the cloud skill "
                            "finishes downloading (knowledge-ingest source register)")
    elif status in {"DISCOVERING", "DOWNLOADING"}:
        action["next_action"] = "wait_download"
    elif status == "DOWNLOADED":
        action["next_action"] = "route"
    elif status == "ROUTING":
        action["next_action"] = "preprocess"
    elif status in {"TRANSCRIBING", "DOCCHUNKING", "VERIFYING"}:
        action["next_action"] = "preprocess_resume"
    elif status == "CORPUS_READY":
        pending = _pending_targets(manifest)
        action["targets_remaining"] = pending
        action["corpus_path"] = (str(manifest.docchunk.corpus_path)
                                 if manifest.docchunk.corpus_path else None)
        # 依赖决定能不能运行，声明顺序决定先运行谁：多 READY 取第一个
        action["next_action"] = (get_target(pending[0]).invoke_key
                                 if pending else "complete_job")
    elif status == "TARGET_RUNNING":
        action["corpus_path"] = (str(manifest.docchunk.corpus_path)
                                 if manifest.docchunk.corpus_path else None)
        active = manifest.active_target
        if active:
            action["target"] = active
            action["next_action"] = get_target(active).invoke_key
        else:
            pending = _pending_targets(manifest)
            action["targets_remaining"] = pending
            action["next_action"] = (get_target(pending[0]).invoke_key
                                     if pending else "complete_job")
    elif status == "WAITING_USER":
        target, gate = _waiting_target(manifest)
        action["next_action"] = "ask_user"
        action["target"] = target
        action["gate"] = gate
    elif status == "BLOCKED":
        action["next_action"] = "resolve_blocked"
        action["reason"] = (manifest.errors[-1].get("reason")
                            if manifest.errors else "unknown")
        action["blocked_targets"] = [name for name, state in
                                     manifest.targets.items()
                                     if state.status == "BLOCKED"]
    elif status == "FAILED":
        action["next_action"] = "inspect_failure"
    elif status == "COMPLETED":
        action["next_action"] = "report"
        outputs = {name: str(state.output_path)
                   for name, state in manifest.targets.items()
                   if state.output_path}
        action["target_outputs"] = outputs
        # v0.2 兼容键
        action["cangjie_output"] = outputs.get("cangjie")
        action["personal_output"] = outputs.get("personal")
    return action


# ---------- gates（live） ----------


def gate_enter(manifest: JobManifest, target: str, name: str) -> None:
    state = target_state(manifest, target)
    if manifest.status != "TARGET_RUNNING":
        raise InvalidTransition(
            f"gate enter requires TARGET_RUNNING, got {manifest.status}")
    if state.status in TERMINAL_TARGET_STATUSES:
        raise ValueError(
            f"terminal target cannot enter a gate: {target} ({state.status})")
    if manifest.active_target not in (None, target):
        raise InvalidTransition(
            f"another target is active: {manifest.active_target}")
    state.status = "WAITING_USER"
    state.waiting_for = name
    manifest.active_target = target
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "gate": name, "action": "enter",
    })
    transition_to(manifest, "WAITING_USER")


def _find_grant(manifest: JobManifest, grant_id: str) -> dict | None:
    for grant in manifest.preauthorizations:
        if grant.get("grant_id") == grant_id:
            return grant
    return None


def gate_preauthorize(
    manifest: JobManifest, target: str, name: str, value,
) -> dict:
    """规格 10 第一阶段：发放预授权 grant（不改 target 状态）。"""
    runtime = get_target(target)
    if name not in runtime.preauthorizable_gates:
        raise ValueError(
            f"gate {name!r} on target {target!r} does not accept "
            f"preauthorization (live confirmation required)")
    grant = {
        "grant_id": f"pa_{uuid4().hex}",
        "target": target,
        "gate": name,
        "value": value,
        "granted_at": _now_iso(),
    }
    manifest.preauthorizations.append(grant)
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "gate": name,
        "action": "preauthorize", "grant_id": grant["grant_id"],
    })
    return grant


def gate_resolve(
    manifest: JobManifest, target: str, name: str, decision: str,
    preauthorization: str | None = None,
) -> None:
    if decision not in {"confirmed", "rejected"}:
        raise ValueError(f"decision must be confirmed|rejected, got {decision}")
    state = target_state(manifest, target)
    if manifest.status != "WAITING_USER" or state.status != "WAITING_USER" \
            or state.waiting_for != name:
        raise ValueError(
            f"no open gate {name!r} for target {target!r} "
            f"(status={manifest.status}, waiting_for={state.waiting_for!r})")
    entry = {
        "ts": _now_iso(), "target": target, "gate": name,
        "action": "resolve", "decision": decision,
    }
    if preauthorization is not None:
        # 规格 10 第二阶段：引用 grant；不得再传 value（grant 已携带）
        if decision != "confirmed":
            raise ValueError(
                "preauthorization resolve requires decision=confirmed")
        grant = _find_grant(manifest, preauthorization)
        if grant is None:
            raise ValueError(
                f"unknown preauthorization grant: {preauthorization}")
        if grant["target"] != target or grant["gate"] != name:
            raise ValueError(
                f"grant {preauthorization} does not match "
                f"target/gate ({target}/{name})")
        if grant.get("used_at"):
            raise ValueError(
                f"preauthorization grant already used: {preauthorization}")
        grant["used_at"] = _now_iso()
        entry["approval_mode"] = "preauthorized"
        entry["grant_id"] = preauthorization
    manifest.gate_history.append(entry)
    state.waiting_for = None
    if decision == "confirmed":
        state.status = "RUNNING"
        manifest.active_target = target
        transition_to(manifest, "TARGET_RUNNING")
    else:
        state.status = "SKIPPED"
        state.reason = "user_rejected"
        manifest.errors.append({
            "reason": "target_rejected", "target": target, "gate": name,
        })
        if manifest.active_target == target:
            manifest.active_target = None
        # 依赖传播（family_router ← cangjie SKIPPED）+ 聚合判定/链式进下一个
        settle(manifest)


# ---------- target runtime 操作 ----------


def target_start(manifest: JobManifest, target: str) -> None:
    """Agent begins a distillation target（依赖满足才允许启动）。"""
    state = target_state(manifest, target)
    unmet = [dep for dep in state.depends_on
             if manifest.targets[dep].status != "COMPLETED"]
    if unmet:
        raise ValueError(
            f"dependencies not COMPLETED for {target}: {unmet}")
    if state.status == "RUNNING":
        if manifest.active_target != target:
            raise ValueError(
                f"target {target} already RUNNING but active_target="
                f"{manifest.active_target!r}")
        return  # 幂等重启
    if state.status not in {"PENDING", "READY"}:
        raise ValueError(
            f"target {target} not startable (status={state.status})")
    if manifest.status == "CORPUS_READY":
        transition_to(manifest, "TARGET_RUNNING")
        manifest.active_target = target
    elif manifest.status == "TARGET_RUNNING":
        if manifest.active_target != target:
            # TARGET_RUNNING 自转换（active_target 变更，守卫在状态机层）
            transition_to(manifest, "TARGET_RUNNING",
                          new_active_target=target)
            manifest.active_target = target
    else:
        raise InvalidTransition(
            f"cannot start target {target} from {manifest.status}")
    state.status = "RUNNING"
    if state.started_at is None:
        state.started_at = _now()
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "action": "start",
    })


def target_complete(
    manifest: JobManifest, target: str, output_path: Path,
    pipeline_state: Path | None = None,
    output_manifest: Path | None = None,
) -> None:
    """规格 3：由 CLI 的 edit() 事务包裹；此处只做状态推进。"""
    state = target_state(manifest, target)
    if state.status != "RUNNING":
        raise ValueError(
            f"target {target} not completable (status={state.status})")
    state.status = "COMPLETED"
    state.completed_at = _now()
    state.output_path = Path(output_path)
    if pipeline_state is not None:
        state.pipeline_state = Path(pipeline_state)
    if output_manifest is not None:
        state.output_manifest = Path(output_manifest)
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "action": "complete",
    })
    if manifest.active_target == target:
        manifest.active_target = None  # 终态不得为 active_target
    settle(manifest)


def target_checkpoint(
    manifest: JobManifest, target: str, phase: str,
    checkpoint_path: Path | None = None,
    evidence_dir: Path | None = None,
) -> dict:
    """规格 15：事务式记录断点（不做 heartbeat）。"""
    state = target_state(manifest, target)
    if state.status in TERMINAL_TARGET_STATUSES:
        raise ValueError(
            f"terminal target cannot checkpoint: {target} ({state.status})")
    if checkpoint_path is not None:
        state.checkpoint_path = Path(checkpoint_path)
    if evidence_dir is not None:
        state.evidence_dir = Path(evidence_dir)
    state.checkpoint_at = _now()
    state.attempt = (state.attempt or 0) + 1
    record = {
        "target": target,
        "phase": phase,
        "checkpoint_path": (str(state.checkpoint_path)
                            if state.checkpoint_path else None),
        "evidence_dir": (str(state.evidence_dir)
                         if state.evidence_dir else None),
        "checkpoint_at": state.checkpoint_at.isoformat(),
        "attempt": state.attempt,
    }
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "action": "checkpoint",
        "phase": phase, "attempt": state.attempt,
    })
    return record


def target_resume(manifest: JobManifest, target: str) -> str:
    """规格 9 Blocker 1：BLOCKED 恢复出口（显式，预算修改不自动恢复）。

    target BLOCKED → READY（依赖已全 COMPLETED 才 READY，否则 PENDING）；
    overall 从 BLOCKED 恢复为可调度态：有其他 RUNNING target → TARGET_RUNNING
    （active_target 归还给它），否则 CORPUS_READY（active_target=null，
    由 next_action 再选）。
    """
    state = target_state(manifest, target)
    if state.status != "BLOCKED":
        raise ValueError(
            f"target {target} is not BLOCKED (status={state.status})")
    state.status = "READY" if dependencies_satisfied(manifest, target) \
        else "PENDING"
    state.reason = None
    running = [name for name, other in manifest.targets.items()
               if other.status in ACTIVE_TARGET_STATUSES]
    if manifest.status == "BLOCKED":
        if running:
            transition_to(manifest, "TARGET_RUNNING")
            manifest.active_target = running[0]
        else:
            transition_to(manifest, "CORPUS_READY")
            manifest.active_target = None
    manifest.gate_history.append({
        "ts": _now_iso(), "target": target, "action": "resume",
        "target_status": state.status,
    })
    # 评审 I3：resume 后 propagate + 聚合判定，让阻塞传播到 Job 整体
    settle(manifest)
    return state.status
