import subprocess
from pathlib import Path

import pytest

from knowledge_ingest.runner import CommandResult, poll, run_checked, safe_argv, spawn


def test_run_checked_returns_result_without_raising_on_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 1, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_checked(["uv", "run", "docchunk", "verify", "x"], cwd=tmp_path)
    assert isinstance(result, CommandResult)
    assert result.returncode == 1
    assert result.stderr == "err"
    assert captured["argv"] == ["uv", "run", "docchunk", "verify", "x"]
    assert captured["shell"] is False
    assert captured["capture_output"] is True
    assert captured["check"] is False


def test_run_checked_propagates_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_checked(["slow"], cwd=tmp_path, timeout=1)


def test_run_checked_real_binary(tmp_path: Path):
    result = run_checked(["/bin/echo", "hello"], cwd=tmp_path)
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert result.started_at <= result.ended_at


def test_safe_argv_redacts_secret_values():
    argv = ["bdpan", "download", "--token", "supersecret", "file.pdf"]
    assert "supersecret" not in " ".join(safe_argv(argv))


def test_spawn_and_poll_roundtrip(tmp_path: Path):
    log_path = tmp_path / "cmd.log"
    proc = spawn(["/bin/echo", "spawned"], cwd=tmp_path, log_path=log_path)
    final = None
    for _ in range(100):
        final = poll(proc)
        if final is not None:
            break
        import time
        time.sleep(0.05)
    assert final is not None
    assert final.returncode == 0
    assert "spawned" in log_path.read_text(encoding="utf-8")
