"""V3 M8: Insight Doctor——只读体检（实施方案 Task 8 Step 6）。"""

from __future__ import annotations

import os
from pathlib import Path

from knowledge_ingest.config import AppConfig


def run_insight_doctor(config: AppConfig, store) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    root = Path(config.pipeline_root) / "insight"
    writable = root.is_dir() and os.access(root, os.W_OK)
    checks.append(("insight_root", "PASS" if writable else "WARN",
                   str(root)))
    if store is not None:
        version = store.user_version()
        checks.append(("schema_version",
                       "PASS" if version >= 1 else "FAIL",
                       f"user_version={version}"))
    else:
        checks.append(("schema_version", "FAIL", "store is None"))
    model_cmd = os.environ.get("KI_INSIGHT_MODEL_CMD", "").strip()
    checks.append(("model_cmd", "PASS" if model_cmd else "WARN",
                   model_cmd[:60] or "not configured"))
    retrieval_cmd = os.environ.get("KI_INSIGHT_RETRIEVAL_CMD", "").strip()
    checks.append(("retrieval_cmd", "PASS" if retrieval_cmd else "WARN",
                   retrieval_cmd[:60] or "not configured"))
    if store is not None:
        pending = store._conn.execute(
            "SELECT COUNT(*) FROM cognition_proposals "
            "WHERE gate_status = 'pending'").fetchone()[0]
        checks.append(("pending_gates", "INFO", str(pending)))
        blocked = store._conn.execute(
            "SELECT COUNT(*) FROM insight_runs "
            "WHERE status = 'blocked'").fetchone()[0]
        checks.append(("blocked_runs", "INFO", str(blocked)))
    return checks

