"""v0.3 A4: watchdog installer — dynamic paths, idempotent install.

Unit tests never touch the real launchd: run_launchctl is monkeypatched.
Nor the real machine PATH: _resolve_ki is monkeypatched in fake_home,
so generated content cannot depend on where knowledge-ingest happens
to be installed on the host running the tests (test isolation).
"""

import plistlib
from pathlib import Path

import pytest

from knowledge_ingest import watchdog
from knowledge_ingest.config import AppConfig


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus-root"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive", "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(watchdog, "_home", lambda: home)
    fake_ki = tmp_path / "bin" / "knowledge-ingest"
    monkeypatch.setattr(watchdog, "_resolve_ki", lambda: str(fake_ki))
    calls: list[list[str]] = []
    loaded: list[bool] = [False]

    def fake_launchctl(argv: list[str]):
        calls.append(argv)
        if argv[:2] == ["launchctl", "list"]:
            out = watchdog.label_for() + "\n" if loaded[0] else ""
            return 0, out, ""
        if argv[:2] == ["launchctl", "load"]:
            loaded[0] = True
        elif argv[:2] == ["launchctl", "unload"]:
            loaded[0] = False
        return 0, "", ""

    monkeypatch.setattr(watchdog, "run_launchctl", fake_launchctl)
    return home, calls, fake_ki


def test_install_generates_dynamic_script_and_plist(tmp_path, fake_home):
    config = make_config(tmp_path)
    _home, calls, fake_ki = fake_home
    rc = watchdog.install(config)
    assert rc == 0
    script = config.pipeline_root / "bin" / "ki-resume.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111  # executable
    text = script.read_text(encoding="utf-8")
    assert str(config.pipeline_root) in text          # dynamic paths
    assert "resume --exec" in text
    assert "export PATH=" in text
    assert f'KI="{fake_ki}"' in text                  # injected KI path, verbatim
    assert "/Users/" not in text                      # no real user home leaks in
    label = watchdog.label_for()
    plist_path = watchdog.plist_install_path()
    assert plist_path.is_file()
    cfg = plistlib.loads(plist_path.read_bytes())
    assert cfg["Label"] == label
    assert cfg["ProgramArguments"] == ["/bin/zsh", str(script)]
    assert cfg["StartInterval"] == 900
    assert cfg["RunAtLoad"] is True
    # TCC 修复后：launchd 日志固定内建盘（不再随 pipeline_root 走）
    assert cfg["StandardOutPath"] == str(watchdog.launchd_log_path(config))
    assert calls, "launchctl load must be invoked via adapter"
    assert any("load" in argv for argv in calls)


def test_install_is_idempotent(tmp_path, fake_home, capsys):
    config = make_config(tmp_path)
    assert watchdog.install(config) == 0
    assert watchdog.install(config) == 0
    assert "already up to date" in capsys.readouterr().out.lower()


def test_status_reports_installed_and_missing(tmp_path, fake_home, capsys):
    config = make_config(tmp_path)
    rc = watchdog.status(config)
    assert rc == 0
    assert "not installed" in capsys.readouterr().out
    watchdog.install(config)
    capsys.readouterr()
    rc = watchdog.status(config)
    assert rc == 0
    out = capsys.readouterr().out
    assert "installed" in out.lower() or "loaded" in out.lower()


def test_uninstall_removes_files_keeps_logs(tmp_path, fake_home):
    config = make_config(tmp_path)
    watchdog.install(config)
    log_dir = watchdog.launchd_log_path(config).parent   # fake home 下
    assert log_dir.exists()                              # install 建好目录
    (log_dir / "ki-resume-launchd.log").write_text("log")  # 模拟 launchd 落日志
    rc = watchdog.uninstall(config)
    assert rc == 0
    assert not watchdog.plist_install_path().exists()
    assert not (config.pipeline_root / "bin" / "ki-resume.sh").exists()
    assert (log_dir / "ki-resume-launchd.log").exists()  # logs kept


def test_uninstall_when_not_installed_is_ok(tmp_path, fake_home):
    config = make_config(tmp_path)
    assert watchdog.uninstall(config) == 0
