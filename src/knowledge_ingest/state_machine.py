"""Explicit job state machine: illegal transitions are refused, never guessed."""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge_ingest.models import JobManifest, OverallStatus

TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"DISCOVERING", "DOWNLOADED", "ROUTING"},
    "DISCOVERING": {"DOWNLOADING", "BLOCKED", "FAILED"},
    "DOWNLOADING": {"DOWNLOADED", "FAILED"},
    "DOWNLOADED": {"ROUTING"},
    "ROUTING": {"TRANSCRIBING", "DOCCHUNKING", "BLOCKED"},
    "TRANSCRIBING": {"DOCCHUNKING", "BLOCKED", "FAILED"},
    "DOCCHUNKING": {"VERIFYING", "FAILED"},
    "VERIFYING": {"CORPUS_READY", "BLOCKED"},
    "CORPUS_READY": {"DISTILLING_CANGJIE", "DISTILLING_PERSONAL", "COMPLETED"},
    "DISTILLING_CANGJIE": {"WAITING_USER", "DISTILLING_PERSONAL", "COMPLETED", "FAILED"},
    "DISTILLING_PERSONAL": {"WAITING_USER", "COMPLETED", "FAILED"},
    "WAITING_USER": {"DISTILLING_CANGJIE", "DISTILLING_PERSONAL", "COMPLETED", "BLOCKED"},
    # BLOCKED 恢复出口：允许回到出错阶段前的可重入状态（recovery.md 定义具体策略）
    "BLOCKED": {"DISCOVERING", "DOWNLOADING", "ROUTING", "TRANSCRIBING", "DOCCHUNKING", "FAILED"},
    "FAILED": set(),
    "COMPLETED": set(),
}


class InvalidTransition(Exception):
    pass


def transition_to(manifest: JobManifest, new_status: OverallStatus) -> JobManifest:
    allowed = TRANSITIONS.get(manifest.status)
    if allowed is None or new_status not in allowed:
        raise InvalidTransition(
            f"illegal transition {manifest.status} -> {new_status}"
        )
    manifest.status = new_status
    manifest.updated_at = datetime.now(timezone.utc)
    return manifest


def transition(manifest: JobManifest, new_status: OverallStatus) -> JobManifest:
    return transition_to(manifest, new_status)
