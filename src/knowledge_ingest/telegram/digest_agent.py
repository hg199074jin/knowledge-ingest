"""TG7：digest 定时 LaunchAgent（08:00/12:00/20:00）。

复用 watch_agent 的 launchctl 纪律：launchctl 只经 run_launchctl()
调用；plist 生成是纯函数。digest 任务本身只读采集库，
`telegram digest --send` 在无 WxPusher 凭据时降级打印（H2 补凭据即生效）。
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from knowledge_ingest.config import AppConfig

SCHEDULE = [{"Hour": 8, "Minute": 0}, {"Hour": 12, "Minute": 0},
            {"Hour": 20, "Minute": 0}]


def _home() -> Path:
    return Path.home()


def label_for(user: str | None = None) -> str:
    user = user or _home().name
    return f"com.{user}.ki-telegram-digest"


def plist_install_path(user: str | None = None) -> Path:
    return (_home() / "Library" / "LaunchAgents"
            / f"{label_for(user)}.plist")


def log_path(config: AppConfig) -> Path:
    return (Path.home() / "Library" / "Logs" / "knowledge-ingest"
            / "telegram-digest.log")


def run_launchctl(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, text=True, capture_output=True, check=False,
                          timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def is_loaded(user: str | None = None) -> bool:
    label = label_for(user)
    try:
        code, out, _err = run_launchctl(["launchctl", "list"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return code == 0 and label in out


def program_arguments(ki_exe: str) -> list[str]:
    return [ki_exe, "telegram", "digest", "--send"]


def generate_plist_bytes(config: AppConfig, ki_exe: str) -> bytes:
    cfg = {
        "Label": label_for(),
        "ProgramArguments": program_arguments(ki_exe),
        "StartCalendarInterval": SCHEDULE,
        "WorkingDirectory": str(
            Path(__file__).resolve().parents[3]),
        "StandardOutPath": str(log_path(config)),
        "StandardErrorPath": str(log_path(config)),
    }
    return plistlib.dumps(cfg, fmt=plistlib.FMT_XML)


def _sha(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()[:16]}"


def install(config: AppConfig, *, ki_exe: str | None = None) -> int:
    import sys

    plist_dst = plist_install_path()
    log_path(config).parent.mkdir(parents=True, exist_ok=True)
    exe = ki_exe or (Path(sys.executable).absolute().parent
                     / "knowledge-ingest")
    if not Path(exe).is_file():
        exe = Path(__file__).resolve().parents[3] / ".venv" / "bin" \
            / "knowledge-ingest"
    plist_data = generate_plist_bytes(config, str(exe))
    old = plist_dst.read_bytes() if plist_dst.exists() else None
    if old == plist_data and is_loaded():
        print(f"digest-agent: already up to date ({plist_dst})")
        return 0
    if is_loaded():
        run_launchctl(["launchctl", "unload", str(plist_dst)])
    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    plist_dst.write_bytes(plist_data)
    code, out, err = run_launchctl(["launchctl", "load", str(plist_dst)])
    if code != 0:
        print(f"digest-agent: launchctl load failed: {(err or out).strip()}")
        return 1
    print(f"digest-agent: installed ({plist_dst})")
    print("digest-agent: schedule 08:00 / 12:00 / 20:00 (daily)")
    return 0


def status(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        print("digest-agent: not installed "
              "(run `knowledge-ingest telegram digest-agent install`)")
        return 0
    state = "loaded" if is_loaded() else "NOT loaded"
    cfg = plistlib.loads(plist_dst.read_bytes())
    print(f"digest-agent: installed, {state}")
    print(f"digest-agent: schedule = {cfg['StartCalendarInterval']}")
    print(f"digest-agent: log {log_path(config)}")
    return 0


def uninstall(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        print("digest-agent: not installed")
        return 0
    if is_loaded():
        run_launchctl(["launchctl", "unload", str(plist_dst)])
    plist_dst.unlink()
    print(f"digest-agent: uninstalled (log kept at {log_path(config)})")
    return 0
