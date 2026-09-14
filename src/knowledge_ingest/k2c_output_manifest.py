"""K2C output manifest (M2 Task 21/22).

Cangjie 专用的 output_manifest.py 保持不动；本模块是 k2c 专属 scanner。
K2C 侧在其 run 目录写 k2c-target-manifest.yaml（status + 产物清单），KI 在
target complete 时读取并决定性转存为 handoff/k2c-output-manifest.json。

状态映射（Task 22，冻结）：
    completed                  -> 允许 complete -> KI COMPLETED
    needs_review_nonblocking   -> 允许 complete -> KI COMPLETED + review ref
    needs_review_blocking      -> 拒绝 complete -> KI WAITING_USER（由 agent 走 gate）
    paused_budget              -> 拒绝 complete -> KI BLOCKED（K2C 侧恢复后重试）
    failed                     -> 拒绝 complete
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

MANIFEST_NAME = "k2c-target-manifest.yaml"
ALLOWED_COMPLETABLE = {"completed", "needs_review_nonblocking"}
KNOWN_STATUSES = ALLOWED_COMPLETABLE | {
    "needs_review_blocking",
    "paused_budget",
    "failed",
}


class K2CManifestError(Exception):
    pass


def load_k2c_target_manifest(run_dir: Path) -> dict:
    path = Path(run_dir) / MANIFEST_NAME
    if not path.is_file():
        raise K2CManifestError(f"missing {MANIFEST_NAME} under {run_dir}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    status = data.get("status")
    if status not in KNOWN_STATUSES:
        raise K2CManifestError(f"unknown k2c manifest status: {status!r}")
    return data


def build_k2c_output_manifest(run_dir: Path) -> dict:
    """决定性读取：同输入同输出；staged 资产必须真实存在。"""
    run_dir = Path(run_dir)
    manifest = load_k2c_target_manifest(run_dir)
    for rel in manifest.get("staged_assets") or []:
        if not (run_dir / rel).is_file():
            raise K2CManifestError(f"staged asset missing: {rel}")
    return {
        "schema_version": 1,
        "target": "k2c",
        "run_id": manifest.get("run_id"),
        "status": manifest["status"],
        "learning_units": manifest.get("learning_units", 0),
        "families": manifest.get("families", 0),
        "variants": manifest.get("variants", 0),
        "cross_source": manifest.get("cross_source", 0),
        "personal_candidates": manifest.get("personal_candidates", 0),
        "coverage_report": manifest.get("coverage_report") or {},
        "eval_report": manifest.get("eval_report") or {},
        "review_pack": manifest.get("review_pack"),
        "runtime_snapshot": manifest.get("runtime_snapshot") or {},
        "staged_assets": list(manifest.get("staged_assets") or []),
    }


def render_k2c_output_manifest(manifest: dict) -> str:
    """canonical JSON：sort_keys、固定缩进、结尾换行 —— byte-identical 重放。"""
    return json.dumps(
        manifest, sort_keys=True, ensure_ascii=False, indent=2
    ) + "\n"


def k2c_complete_blocked_reason(manifest: dict) -> str | None:
    """Task 22 状态映射：返回拒绝 complete 的原因；None = 允许。"""
    status = manifest["status"]
    if status in ALLOWED_COMPLETABLE:
        return None
    if status == "needs_review_blocking":
        return (
            "k2c run needs_review_blocking: resolve review via gate first "
            "(KI WAITING_USER)"
        )
    if status == "paused_budget":
        return "k2c run paused_budget: resume the K2C build, then retry"
    return f"k2c run status {status!r} is not completable"
