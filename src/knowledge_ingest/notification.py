"""通用通知模块（A5 抽取，M8）：NotificationPort 与 WxPusher 实现。

从 telegram/notification.py 抽取的无状态通用件——任何子系统（Telegram
Source / Personal Insight）都可复用；幂等落库由各子系统的 store 自行
承担（本模块不感知任何具体 store）。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Protocol

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


class NotificationPort(Protocol):
    def send(self, uid: str, summary: str, content: str) -> bool: ...


def wxpusher_env_path(session_dir: Path | None = None) -> Path:
    return (session_dir or (Path.home() / ".config" / "knowledge-ingest"
                            / "telegram")) / "wxpusher.env"


def load_wxpusher_credentials(path: Path | None = None) -> dict[str, str]:
    """解析 wxpusher.env（KEY=VALUE）；缺失/不完整 → 空 dict（降级）。"""
    path = path or wxpusher_env_path()
    if not path.is_file():
        return {}
    creds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        creds[key.strip()] = value.strip()
    if not creds.get("WXPUSHER_APP_TOKEN") or not creds.get("WXPUSHER_UID"):
        return {}
    return creds


class WxPusherAdapter:
    """NotificationPort 的 WxPusher 实现（HTTP，urllib）。"""

    def __init__(self, app_token: str, uid: str, *,
                 api_url: str = WXPUSHER_API, timeout: int = 15):
        self.app_token = app_token
        self.uid = uid
        self.api_url = api_url
        self.timeout = timeout

    def send(self, uid: str, summary: str, content: str) -> bool:
        payload = json.dumps({
            "appToken": self.app_token, "content": content,
            "summary": summary[:99], "contentType": 1,
            "uids": [uid or self.uid],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.api_url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("code") == 1000)


def null_port(uid: str, summary: str, content: str) -> bool:
    """无凭据时的降级端口：本地打印，返回 False（未真正投递）。"""
    print(f"[notify:degraded] {summary}\n{content}")
    return False
