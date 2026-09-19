"""TG3: Telegram 认证与会话安全（冻结设计 §5.3/§5.5，人工环节 H1）。

session 按"账号完全访问凭据"处理：
- .gitignore 五条规则是任何真实登录的前置安全门（E3）；
- session 文件 600、目录 700；
- 验证码 / 2FA 密码只存在于交互过程，永不写盘、永不进日志。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

GITIGNORE_REQUIRED_PATTERNS = (
    "*.session",
    "*.session-journal",
    "credentials.env",
    "telegram-secrets*",
    "wxpusher.env",
)

REQUIRED_CREDENTIAL_KEYS = ("API_ID", "API_HASH")

# 评审 M8：client_port 已有同名领域错误，此处直接复用为唯一权威类
from .client_port import TelegramAuthRequiredError


def parse_credentials(text: str) -> dict[str, str]:
    """KEY=VALUE 格式；'#' 注释与空行忽略；不引入 dotenv 依赖。"""
    creds: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        creds[key.strip()] = value.strip()
    return creds


def load_credentials(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise TelegramAuthRequiredError(
            f"missing credentials.env at {path}; create it with API_ID / "
            "API_HASH from my.telegram.org, then chmod 600")
    creds = parse_credentials(path.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_CREDENTIAL_KEYS
               if not creds.get(key)]
    if missing:
        raise TelegramAuthRequiredError(
            f"credentials.env missing keys: {', '.join(missing)}")
    return creds


def ensure_gitignore_gate(repo_root: str | Path) -> list[str]:
    """H1 前置安全门：返回缺失的 .gitignore 模式列表（空 = 通过）。

    逐行匹配；注释（#）与否定（!pattern）行不算有效规则——
    子串包含会被 "# *.session" 或 "!credentials.env" 骗过（评审 M7）。
    """
    gitignore = Path(repo_root) / ".gitignore"
    active: set[str] = set()
    if gitignore.is_file():
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!")):
                continue
            active.add(stripped)
    return [pattern for pattern in GITIGNORE_REQUIRED_PATTERNS
            if pattern not in active]


def has_session(session_dir: Path) -> bool:
    return any(session_dir.glob("*.session")) \
        if session_dir.is_dir() else False


def check_session_security(session_path: Path) -> dict:
    session_path = Path(session_path)
    directory = session_path.parent

    def private_mode(path: Path) -> bool:
        """组/其他位必须为零；比 600 更严（如 400）同样通过（评审 M6）。"""
        return path.exists() \
            and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0

    return {
        "session_exists": session_path.is_file(),
        "dir_mode_ok": private_mode(directory),
        "file_mode_ok": private_mode(session_path),
    }


def run_auth(*, repo_root: Path, session_dir: Path,
             signer: Callable[[Path, dict], Path]) -> dict:
    """执行一次 H1 认证流程并输出安全核对结果。

    signer(session_dir, credentials) -> session 文件路径；
    真实实现 = TelethonAdapter.interactive_sign_in（交互式登录，
    验证码 / 2FA 只经内存）。测试注入 fake signer。
    """
    missing = ensure_gitignore_gate(repo_root)
    if missing:
        raise TelegramAuthRequiredError(
            "gitignore gate failed; add these patterns to .gitignore "
            "before any Telegram login: " + ", ".join(missing))
    session_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, 0o700)
    credentials = load_credentials(session_dir / "credentials.env")
    session_path = Path(signer(session_dir, credentials))
    os.chmod(session_path, 0o600)
    report = check_session_security(session_path)
    report["session_path"] = str(session_path)
    return report
