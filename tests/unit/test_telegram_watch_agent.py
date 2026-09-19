"""V2.1-1 P0：telegram watch LaunchAgent（watcher 持久化）。

纪律与 watchdog 相同：launchctl 只经 run_launchctl() 调用（测试
monkeypatch）；plist 生成是纯函数，直接断言内容。单元测试绝不触碰
真实 launchd。
"""

import plistlib
from pathlib import Path

import pytest

from knowledge_ingest.config import AppConfig
from knowledge_ingest.telegram import watch_agent


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "baidu", "quark": "quark", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })


def test_labels_and_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_agent, "_home", lambda: tmp_path)
    assert watch_agent.label_for() == f"com.{tmp_path.name}.ki-telegram-watch"
    assert watch_agent.plist_install_path() == (
        tmp_path / "Library" / "LaunchAgents"
        / f"com.{tmp_path.name}.ki-telegram-watch.plist")
    config = make_config(tmp_path)
    assert watch_agent.launchd_log_path(config) == (
        config.pipeline_root / "logs" / "launchd" / "telegram-watch.log")


def test_collect_env_picks_channel_vars_and_defaults_handoff(monkeypatch):
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin",
           "KI_TELEGRAM_LLM_CMD": "codex exec -",
           "KI_TELEGRAM_LLM_MAX_CALLS": "20",
           "KI_TELEGRAM_LLM_BREAKER_RATE_LIMIT": "3",
           "KI_TELEGRAM_LLM_CWD": "/tmp/x",
           "HOME": "/Users/x", "UNRELATED": "nope"}
    collected = watch_agent.collect_env(env)
    assert collected["KI_TELEGRAM_LLM_CMD"] == "codex exec -"
    assert collected["KI_TELEGRAM_LLM_MAX_CALLS"] == "20"
    assert collected["KI_TELEGRAM_LLM_BREAKER_RATE_LIMIT"] == "3"
    assert collected["KI_TELEGRAM_LLM_CWD"] == "/tmp/x"
    assert collected["KI_TELEGRAM_HANDOFF"] == "0"      # 显式落盘，可审计
    assert collected["PATH"] == env["PATH"]             # codex/uv 需要
    assert "UNRELATED" not in collected
    assert "KI_TELEGRAM_LLM_TIMEOUT" not in collected   # 未设置不落


def test_collect_env_preserves_explicit_handoff():
    collected = watch_agent.collect_env(
        {"PATH": "/bin", "KI_TELEGRAM_HANDOFF": "1"})
    assert collected["KI_TELEGRAM_HANDOFF"] == "1"


def test_generate_plist_bytes(tmp_path):
    config = make_config(tmp_path)
    env = watch_agent.collect_env({"PATH": "/bin", "HOME": "/Users/x"})
    data = watch_agent.generate_plist_bytes(
        config, "/repo/.venv/bin/python", env)
    cfg = plistlib.loads(data)
    assert cfg["Label"] == watch_agent.label_for()
    assert cfg["ProgramArguments"] == [
        "/repo/.venv/bin/python", "-m", "knowledge_ingest",
        "telegram", "watch"]
    assert cfg["RunAtLoad"] is True
    assert cfg["KeepAlive"] is True                     # 崩溃/被杀即复活
    assert cfg["WorkingDirectory"] == str(
        Path(config.pipeline_root).parent) or cfg["WorkingDirectory"]
    assert cfg["EnvironmentVariables"]["KI_TELEGRAM_HANDOFF"] == "0"
    assert cfg["StandardOutPath"] == str(watch_agent.launchd_log_path(config))
    assert cfg["StandardErrorPath"] == str(watch_agent.launchd_log_path(config))


@pytest.fixture()
def fake_launchd(monkeypatch, tmp_path):
    """带状态的假 launchd：load/unload 真实改变 is_loaded。"""
    monkeypatch.setattr(watch_agent, "_home", lambda: tmp_path)
    calls: list[list[str]] = []
    state = {"loaded": False}

    def fake_run_launchctl(argv):
        calls.append(argv)
        if len(argv) > 1 and argv[1] == "load":
            state["loaded"] = True
        if len(argv) > 1 and argv[1] == "unload":
            state["loaded"] = False
        return 0, "", ""

    monkeypatch.setattr(watch_agent, "run_launchctl", fake_run_launchctl)
    monkeypatch.setattr(watch_agent, "is_loaded", lambda: state["loaded"])
    return calls


def test_install_writes_and_loads(tmp_path, fake_launchd, capsys, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setenv("KI_TELEGRAM_LLM_CMD", "codex exec -")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert watch_agent.install(config, python_exe="/repo/.venv/bin/python") == 0
    plist = watch_agent.plist_install_path()
    assert plist.is_file()
    cfg = plistlib.loads(plist.read_bytes())
    assert cfg["EnvironmentVariables"]["KI_TELEGRAM_LLM_CMD"] == "codex exec -"
    assert any(argv[0] == "launchctl" and argv[1] == "load"
               for argv in fake_launchd)
    assert "installed" in capsys.readouterr().out


def test_install_idempotent(tmp_path, fake_launchd, capsys, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert watch_agent.install(config, python_exe="/repo/.venv/bin/python") == 0
    loads_before = len(fake_launchd)
    assert watch_agent.install(config, python_exe="/repo/.venv/bin/python") == 0
    assert "already up to date" in capsys.readouterr().out
    assert len(fake_launchd) == loads_before            # 不重复 load


def test_uninstall(tmp_path, fake_launchd, capsys):
    config = make_config(tmp_path)
    watch_agent.install(config, python_exe="/repo/.venv/bin/python")
    capsys.readouterr()
    assert watch_agent.uninstall(config) == 0
    assert not watch_agent.plist_install_path().exists()
    assert any(argv[0] == "launchctl" and argv[1] == "unload"
               for argv in fake_launchd)
    assert "uninstalled" in capsys.readouterr().out
