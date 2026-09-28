"""V3 M8: Shadow Agent——LaunchAgent 增量扫描调度（A6 参数 pin）。"""

from __future__ import annotations

import plistlib

LABEL = "com.sandro.ki-insight-shadow"
START_INTERVAL_SECONDS = 300


def insight_label() -> str:
    return LABEL


def generate_insight_plist_bytes(*, ki_exe: str, config_path: str) -> bytes:
    cfg = {
        "Label": LABEL,
        "ProgramArguments": [
            ki_exe, "insight", "scan", "--provider", "telegram",
            "--config", config_path,
        ],
        "StartInterval": START_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "StandardOutPath": "/Users/sandro/Library/Logs/knowledge-ingest/"
                           "insight-shadow.log",
        "StandardErrorPath": "/Users/sandro/Library/Logs/knowledge-ingest/"
                             "insight-shadow.log",
    }
    return plistlib.dumps(cfg, fmt=plistlib.FMT_XML)
