"""V2.1-1 P0：telegram watcher 的 LaunchAgent 持久化。

watcher 是采集的命脉：nohup 裸进程重启即死，且分类通道的环境变量只存在
于启动命令里——重启后无人带参拉起，reconcile 不会自愈。本模块把
`telegram watch` 安装为 launchd 服务（RunAtLoad + KeepAlive），并在
install 时刻把通道/开关环境变量持久化进 plist。

纪律与 watchdog 相同：launchctl 只经 run_launchctl() 调用（测试
monkeypatch）；plist 生成是纯函数。KeepAlive=true：watcher 只应在
崩溃或被杀时退出，任何退出都应立刻复活。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from knowledge_ingest.config import AppConfig

# install 时持久化的环境变量：通道配置 + 显式开关 + PATH（codex/uv 依赖）
_MANAGED_ENV_KEYS = (
    "KI_TELEGRAM_LLM_CMD",
    "KI_TELEGRAM_LLM_TIMEOUT",
    "KI_TELEGRAM_LLM_MAX_CALLS",
    "KI_TELEGRAM_LLM_BREAKER_EMPTY",
    "KI_TELEGRAM_LLM_BREAKER_RATE_LIMIT",
    "KI_TELEGRAM_LLM_CWD",
)


def _home() -> Path:
    return Path.home()


def label_for(user: str | None = None) -> str:
    user = user or _home().name
    return f"com.{user}.ki-telegram-watch"


def plist_install_path(user: str | None = None) -> Path:
    return (_home() / "Library" / "LaunchAgents"
            / f"{label_for(user)}.plist")


def launchd_log_path(config: AppConfig) -> Path:
    return (config.pipeline_root / "logs" / "launchd"
            / "telegram-watch.log")


def run_launchctl(argv: list[str]) -> tuple[int, str, str]:
    """Adapter for launchctl; monkeypatched in tests."""
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


def collect_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """install 时刻的环境快照 → plist EnvironmentVariables。

    KI_TELEGRAM_HANDOFF 显式写 0（未设置时），让"自动建 k2c job 永远是
    有人开过的"在 plist 里可审计；PATH 必带（模型通道要找 codex/uv）。
    """
    env = dict(environ if environ is not None else os.environ)
    collected = {"PATH": env.get("PATH") or "/usr/bin:/bin"}
    for key in _MANAGED_ENV_KEYS:
        value = env.get(key)
        if value:
            collected[key] = value
    collected["KI_TELEGRAM_HANDOFF"] = env.get("KI_TELEGRAM_HANDOFF") or "0"
    return collected


def generate_plist_bytes(config: AppConfig, python_exe: str,
                         env: dict[str, str]) -> bytes:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = {
        "Label": label_for(),
        "ProgramArguments": [python_exe, "-m", "knowledge_ingest",
                             "telegram", "watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": env,
        "StandardOutPath": str(launchd_log_path(config)),
        "StandardErrorPath": str(launchd_log_path(config)),
    }
    return plistlib.dumps(cfg, fmt=plistlib.FMT_XML)


def _sha(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()[:16]}"


def install(config: AppConfig, *, python_exe: str | None = None,
            environ: dict[str, str] | None = None) -> int:
    import sys

    plist_dst = plist_install_path()
    launchd_log_path(config).parent.mkdir(parents=True, exist_ok=True)
    plist_data = generate_plist_bytes(
        config, python_exe or sys.executable, collect_env(environ))

    old_plist = plist_dst.read_bytes() if plist_dst.exists() else None
    if old_plist == plist_data and is_loaded():
        print(f"watch-agent: already up to date ({plist_dst})")
        return 0

    if old_plist is not None and old_plist != plist_data:
        print(f"watch-agent: plist updated ({_sha(old_plist)} -> "
              f"{_sha(plist_data)})")
    if is_loaded():
        run_launchctl(["launchctl", "unload", str(plist_dst)])
    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    plist_dst.write_bytes(plist_data)
    code, out, err = run_launchctl(["launchctl", "load", str(plist_dst)])
    if code != 0:
        print(f"watch-agent: launchctl load failed: {(err or out).strip()}")
        return 1
    print(f"watch-agent: installed ({plist_dst})")
    print(f"watch-agent: loaded; channel/handoff env persisted "
          f"({len(collect_env(environ))} vars)")
    return 0


def status(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        print("watch-agent: not installed "
              "(run `knowledge-ingest telegram watch-agent install`)")
        return 0
    loaded = is_loaded()
    state = "loaded" if loaded else "NOT loaded (launchctl list miss)"
    cfg = plistlib.loads(plist_dst.read_bytes())
    print(f"watch-agent: installed, {state}")
    print(f"watch-agent: plist {plist_dst} ({_sha(plist_dst.read_bytes())})")
    print(f"watch-agent: channel cmd = "
          f"{cfg['EnvironmentVariables'].get('KI_TELEGRAM_LLM_CMD') or 'none'}")
    print(f"watch-agent: KI_TELEGRAM_HANDOFF = "
          f"{cfg['EnvironmentVariables'].get('KI_TELEGRAM_HANDOFF')}")
    print(f"watch-agent: log {launchd_log_path(config)}")
    return 0


def uninstall(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        print("watch-agent: not installed")
        return 0
    if is_loaded():
        run_launchctl(["launchctl", "unload", str(plist_dst)])
    plist_dst.unlink()
    print(f"watch-agent: uninstalled (log kept at "
          f"{launchd_log_path(config)})")
    return 0
