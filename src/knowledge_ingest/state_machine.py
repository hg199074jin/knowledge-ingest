"""Explicit job state machine: illegal transitions are refused, never guessed."""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge_ingest.models import (
    TERMINAL_TARGET_STATUSES,
    JobManifest,
    OverallStatus,
)

TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"DISCOVERING", "DOWNLOADED", "ROUTING"},
    "DISCOVERING": {"DOWNLOADING", "BLOCKED", "FAILED"},
    "DOWNLOADING": {"DOWNLOADED", "FAILED"},
    "DOWNLOADED": {"ROUTING"},
    "ROUTING": {"TRANSCRIBING", "DOCCHUNKING", "BLOCKED"},
    "TRANSCRIBING": {"DOCCHUNKING", "BLOCKED", "FAILED"},
    # BLOCKED 也允许从 DOCCHUNKING 到达：split 失败（MinerU 崩溃等）可修复后重试
    "DOCCHUNKING": {"VERIFYING", "FAILED", "BLOCKED"},
    "VERIFYING": {"CORPUS_READY", "BLOCKED"},
    # 规格 2/8：DISTILLING_* 合并为 TARGET_RUNNING（active_target 区分谁在跑）
    "CORPUS_READY": {"TARGET_RUNNING", "COMPLETED"},
    # TARGET_RUNNING→TARGET_RUNNING 自转换受守卫（见 transition_to）
    "TARGET_RUNNING": {"WAITING_USER", "TARGET_RUNNING", "COMPLETED",
                       "PARTIAL", "FAILED", "BLOCKED"},
    "WAITING_USER": {"TARGET_RUNNING", "COMPLETED", "PARTIAL", "BLOCKED"},
    # BLOCKED 恢复出口：允许回到出错阶段前的可重入状态（recovery.md 定义具体策略）；
    # 规格 9 Blocker 1：target resume 使 BLOCKED 恢复为可调度态
    "BLOCKED": {"DISCOVERING", "DOWNLOADING", "ROUTING", "TRANSCRIBING",
                "DOCCHUNKING", "CORPUS_READY", "TARGET_RUNNING", "FAILED"},
    "FAILED": set(),
    "COMPLETED": set(),
    "PARTIAL": set(),
}


class InvalidTransition(Exception):
    pass


def transition_to(
    manifest: JobManifest,
    new_status: OverallStatus,
    new_active_target: str | None = None,
) -> JobManifest:
    allowed = TRANSITIONS.get(manifest.status)
    if allowed is None or new_status not in allowed:
        raise InvalidTransition(
            f"illegal transition {manifest.status} -> {new_status}")
    # 规格 8：TARGET_RUNNING→TARGET_RUNNING 自转换仅当
    # active_target 变更 + 新 target 已注册 + 依赖 COMPLETED + 自身未 COMPLETED
    if manifest.status == "TARGET_RUNNING" and new_status == "TARGET_RUNNING":
        if new_active_target is None \
                or new_active_target == manifest.active_target:
            raise InvalidTransition(
                "TARGET_RUNNING self-transition requires a changed "
                f"active_target (current: {manifest.active_target!r})")
        state = manifest.targets.get(new_active_target)
        if state is None:
            raise InvalidTransition(
                f"self-transition target not registered: "
                f"{new_active_target!r}")
        if state.status in TERMINAL_TARGET_STATUSES:
            raise InvalidTransition(
                f"self-transition target {new_active_target!r} already "
                f"terminal ({state.status})")
        unmet = [dep for dep in state.depends_on
                 if manifest.targets[dep].status != "COMPLETED"]
        if unmet:
            raise InvalidTransition(
                f"self-transition target {new_active_target!r} has "
                f"non-COMPLETED dependencies: {unmet}")
        manifest.active_target = new_active_target
    manifest.status = new_status
    manifest.updated_at = datetime.now(timezone.utc)
    return manifest


def transition(manifest: JobManifest, new_status: OverallStatus) -> JobManifest:
    return transition_to(manifest, new_status)
