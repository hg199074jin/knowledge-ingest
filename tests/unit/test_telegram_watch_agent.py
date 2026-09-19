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
    # launchd 打不开外置盘上的日志文件（生产实测 EX_CONFIG/78）→ 固定内建盘
    config = make_config(tmp_path)
    assert watch_agent.launchd_log_path(config) == (
        Path.home() / "Library" / "Logs" / "knowledge-ingest"
        / "telegram-watch.log")


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
        config, "/repo/.venv/bin/knowledge-ingest", env,
        config_path="/repo/config.yaml")
    cfg = plistlib.loads(data)
    assert cfg["Label"] == watch_agent.label_for()
    # --config 必须钉死（launchd 的 cwd 不可控）
    assert cfg["ProgramArguments"] == [
        "/repo/.venv/bin/knowledge-ingest", "telegram", "watch",
        "--config", "/repo/config.yaml"]
    assert watch_agent.program_arguments(
        "/repo/.venv/bin/knowledge-ingest",
        "/repo/config.yaml") == cfg["ProgramArguments"]
    assert cfg["RunAtLoad"] is True
    assert cfg["KeepAlive"] is True                     # 崩溃/被杀即复活
    assert cfg["WorkingDirectory"] == str(watch_agent.repo_root())
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
    monkeypatch.setattr(watch_agent, "running_watcher_pids", list)
    return calls


def test_install_writes_and_loads(tmp_path, fake_launchd, capsys, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setenv("KI_TELEGRAM_LLM_CMD", "codex exec -")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                       config_path="/repo/config.yaml") == 0
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
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                       config_path="/repo/config.yaml") == 0
    loads_before = len(fake_launchd)
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                       config_path="/repo/config.yaml") == 0
    assert "already up to date" in capsys.readouterr().out
    assert len(fake_launchd) == loads_before            # 不重复 load


def test_uninstall(tmp_path, fake_launchd, capsys):
    config = make_config(tmp_path)
    watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                       config_path="/repo/config.yaml")
    capsys.readouterr()
    assert watch_agent.uninstall(config) == 0
    assert not watch_agent.plist_install_path().exists()
    assert any(argv[0] == "launchctl" and argv[1] == "unload"
               for argv in fake_launchd)
    assert "uninstalled" in capsys.readouterr().out


# ---------- 评审 Important 回归 ----------

def test_status_survives_minimal_or_corrupt_plist(tmp_path, fake_launchd,
                                                  capsys):
    """I7：手工精简/损坏的 plist 不能让 status 抛 traceback。"""
    import plistlib

    config = make_config(tmp_path)
    plist = watch_agent.plist_install_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as fh:
        plistlib.dump({"Label": "hand.edited"}, fh)   # 无 EnvironmentVariables
    assert watch_agent.status(config) == 0
    out = capsys.readouterr().out
    assert "channel cmd = none" in out and "KI_TELEGRAM_HANDOFF = unset" in out

    plist.write_bytes(b"not a plist")                        # 损坏
    assert watch_agent.status(config) == 1
    assert "corrupt" in capsys.readouterr().out


def test_install_env_change_triggers_reload(tmp_path, fake_launchd, capsys,
                                             monkeypatch):
    """I(覆盖空缺)：env 变化 → unload+重写+load（staleness 刷新分支）。"""
    config = make_config(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                               config_path="/repo/config.yaml") == 0
    loads = [a for a in fake_launchd if a[1] == "load"]
    unloads = [a for a in fake_launchd if a[1] == "unload"]
    assert len(loads) == 1 and not unloads
    capsys.readouterr()

    monkeypatch.setenv("KI_TELEGRAM_LLM_MAX_CALLS", "20")   # env 变了
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                               config_path="/repo/config.yaml") == 0
    assert "plist updated" in capsys.readouterr().out
    loads = [a for a in fake_launchd if a[1] == "load"]
    unloads = [a for a in fake_launchd if a[1] == "unload"]
    assert len(loads) == 2 and len(unloads) == 1            # 真的换血


def test_install_aborts_when_unload_fails(tmp_path, monkeypatch, capsys):
    """I4：unload 失败必须中止——否则旧 KeepAlive job 带 stale env 双跑。"""
    monkeypatch.setattr(watch_agent, "_home", lambda: tmp_path)
    monkeypatch.setattr(watch_agent, "running_watcher_pids", list)
    monkeypatch.setattr(watch_agent, "is_loaded", lambda: True)
    monkeypatch.setattr(watch_agent, "run_launchctl",
                        lambda argv: (1, "", "boom")
                        if argv[1] == "unload" else (0, "", ""))
    config = make_config(tmp_path)
    assert watch_agent.install(config, ki_exe="/x/knowledge-ingest",
                               config_path="/repo/config.yaml") == 1
    assert "aborting before load" in capsys.readouterr().err


def test_install_warns_on_running_watcher(tmp_path, fake_launchd, capsys,
                                          monkeypatch):
    """I5：nohup watcher 还活着 → 安装时大声警告（session 互斥）。"""
    config = make_config(tmp_path)
    monkeypatch.setattr(watch_agent, "running_watcher_pids",
                        lambda: [30848])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                               config_path="/repo/config.yaml") == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "30848" in err


def test_uninstall_removes_orphan_job_without_plist(tmp_path, fake_launchd,
                                                     capsys):
    """I3：plist 被手工删掉但 job 还 loaded → 按 label 移除，不能装死。"""
    config = make_config(tmp_path)
    watch_agent.install(config, ki_exe="/repo/.venv/bin/knowledge-ingest",
                        config_path="/repo/config.yaml")
    watch_agent.plist_install_path().unlink()                # 手工删除
    capsys.readouterr()
    assert watch_agent.uninstall(config) == 0
    assert "removed orphan job" in capsys.readouterr().out
    assert any(a[1] == "remove" for a in fake_launchd)
