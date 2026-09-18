"""TG3: Telegram 认证与会话安全（冻结设计 §5.3/§5.5，人工环节 H1）。

session 按"账号完全访问凭据"处理：.gitignore 五条规则是登录前置
安全门；session 文件 600、目录 700；验证码/2FA 密码永不落盘。
"""

import os
from pathlib import Path

import pytest

from knowledge_ingest.telegram.auth import (
    GITIGNORE_REQUIRED_PATTERNS,
    TelegramAuthRequiredError,
    check_session_security,
    ensure_gitignore_gate,
    has_session,
    load_credentials,
    parse_credentials,
    run_auth,
)

CREDENTIALS_TEXT = "# comment\n\nAPI_ID=12345\nAPI_HASH=deadbeef\nPHONE=+8613800000000\n"


def write_gitignore(root: Path, patterns: list[str] | None = None):
    patterns = GITIGNORE_REQUIRED_PATTERNS if patterns is None else patterns
    (root / ".gitignore").write_text("\n".join(patterns) + "\n",
                                     encoding="utf-8")


def make_session(session_dir: Path, mode_file=0o600, mode_dir=0o700):
    session_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, mode_dir)
    session = session_dir / "session.session"
    session.write_bytes(b"fake-session")
    session.chmod(mode_file)
    return session


def test_parse_credentials():
    creds = parse_credentials(CREDENTIALS_TEXT)
    assert creds == {"API_ID": "12345", "API_HASH": "deadbeef",
                     "PHONE": "+8613800000000"}


def test_parse_credentials_missing_required_keys(tmp_path: Path):
    path = tmp_path / "credentials.env"
    path.write_text("API_ID=12345\n", encoding="utf-8")
    with pytest.raises(TelegramAuthRequiredError, match="API_HASH"):
        load_credentials(path)


def test_load_credentials_missing_file(tmp_path: Path):
    with pytest.raises(TelegramAuthRequiredError, match="credentials.env"):
        load_credentials(tmp_path / "nope.env")


def test_gitignore_gate_complete(tmp_path: Path):
    write_gitignore(tmp_path)
    assert ensure_gitignore_gate(tmp_path) == []


def test_gitignore_gate_lists_missing(tmp_path: Path):
    write_gitignore(tmp_path, ["*.session", "*.session-journal"])
    missing = ensure_gitignore_gate(tmp_path)
    assert "credentials.env" in missing
    assert "telegram-secrets*" in missing
    assert "wxpusher.env" in missing


def test_session_security_check(tmp_path: Path):
    session = make_session(tmp_path / "telegram")
    report = check_session_security(session)
    assert report["session_exists"] is True
    assert report["dir_mode_ok"] is True
    assert report["file_mode_ok"] is True


def test_session_security_flags_loose_perms(tmp_path: Path):
    session = make_session(tmp_path / "telegram", mode_file=0o644,
                           mode_dir=0o755)
    report = check_session_security(session)
    assert report["dir_mode_ok"] is False
    assert report["file_mode_ok"] is False


def test_has_session(tmp_path: Path):
    session_dir = tmp_path / "telegram"
    assert has_session(session_dir) is False
    make_session(session_dir)
    assert has_session(session_dir) is True


def test_run_auth_happy_path(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    write_gitignore(repo_root)
    session_dir = tmp_path / "home" / "telegram"
    creds_path = session_dir / "credentials.env"
    session_dir.mkdir(parents=True)
    creds_path.write_text(CREDENTIALS_TEXT, encoding="utf-8")

    def fake_signer(directory: Path, creds: dict) -> Path:
        assert creds["API_HASH"] == "deadbeef"
        return make_session(directory)

    result = run_auth(repo_root=repo_root, session_dir=session_dir,
                      signer=fake_signer)
    assert result["session_exists"] is True
    assert result["dir_mode_ok"] is True
    assert result["file_mode_ok"] is True


def test_run_auth_blocked_by_gitignore_gate(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    write_gitignore(repo_root, ["*.session"])  # 只有一条，不达标
    session_dir = tmp_path / "home" / "telegram"
    with pytest.raises(TelegramAuthRequiredError, match="gitignore"):
        run_auth(repo_root=repo_root, session_dir=session_dir,
                 signer=None)
