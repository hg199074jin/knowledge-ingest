"""V2.1-1 P0：telegram watcher 的 LaunchAgent 持久化。

watcher 是采集的命脉：nohup 裸进程重启即死，且分类通道的环境变量只存在
于启动命令里——重启后无人带参拉起，reconcile 不会自愈。本模块把
`telegram watch` 安装为 launchd 服务（RunAtLoad + KeepAlive），并在
install 时刻把通道/开关环境变量持久化进 plist。

纪律与 watchdog 相同：launchctl 只经 run_launchctl() 调用（测试
monkeypatch）；plist 生成是纯函数。KeepAlive=true：watcher 只应在
崩溃或被杀时退出，任何退出都应立刻复活。

本机现实约束（TCC）：数据根在可移动卷（ORICO）上时，launchd 子进程
默认没有该卷的访问授权——表现为 spawn 挂起或 EX_CONFIG/78。需在
"隐私与安全性 → 完整磁盘访问"里对解释器本体授权；授权前用 nohup
方式运行 watcher（迁移：先停 nohup → install → status 验证）。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
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
    """launchd 的 stdout/stderr 必须落内建盘：实测指向外置盘（ORICO）时
    spawn 直接 EX_CONFIG/78（launchd 无法打开该卷上的日志文件）。
    config 参数保留以维持调用方形状，日志固定走 ~/Library/Logs。"""
    return Path.home() / "Library" / "Logs" / "knowledge-ingest"         / "telegram-watch.log"


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


def program_arguments(ki_exe: str, config_path: str | Path) -> list[str]:
    # --config 必须钉死：launchd 的 cwd 不可控，不钉则配置解析漂移
    return [ki_exe, "telegram", "watch", "--config", str(config_path)]


def repo_root() -> Path:
    # watch_agent.py 位于 src/knowledge_ingest/telegram/ → 仓库根是 parents[3]
    return Path(__file__).resolve().parents[3]


def _ki_exe() -> str:
    """定位 console script：先看解释器同目录，再回退仓库 venv 布局。

    不能对 sys.executable 做 resolve()——venv 的 python3 是指向 uv
    管理解释器的符号链接，resolve 后同目录就不再是 venv 的 bin。
    """
    import sys

    candidates = [
        Path(sys.executable).absolute().parent / "knowledge-ingest",
        repo_root() / ".venv" / "bin" / "knowledge-ingest",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "knowledge-ingest launcher not found "
        f"(tried: {', '.join(str(c) for c in candidates)})")


def generate_plist_bytes(config: AppConfig, ki_exe: str,
                         env: dict[str, str], config_path: str | Path) -> bytes:
    cfg = {
        "Label": label_for(),
        "ProgramArguments": program_arguments(ki_exe, config_path),
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(repo_root()),
        "EnvironmentVariables": env,
        "StandardOutPath": str(launchd_log_path(config)),
        "StandardErrorPath": str(launchd_log_path(config)),
    }
    return plistlib.dumps(cfg, fmt=plistlib.FMT_XML)


def _sha(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()[:16]}"


def running_watcher_pids() -> list[int]:
    """检测已运行的 telegram watch 进程（nohup 迁移竞态防护）。"""
    try:
        proc = subprocess.run(["pgrep", "-f", "telegram watch"],
                              text=True, capture_output=True, check=False,
                              timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [int(line) for line in proc.stdout.split() if line.isdigit()]


def install(config: AppConfig, *, environ: dict[str, str] | None = None,
            ki_exe: str | None = None,
            config_path: str | Path | None = None) -> int:
    plist_dst = plist_install_path()
    launchd_log_path(config).parent.mkdir(parents=True, exist_ok=True)
    env = collect_env(environ)
    plist_data = generate_plist_bytes(
        config, ki_exe or _ki_exe(), env,
        config_path or repo_root() / "config.example.yaml")

    pids = running_watcher_pids()
    if pids:
        # session 互斥：双 watcher 会 telethon database is locked（实测）
        print("watch-agent: WARNING a watcher is already running "
              f"(pid {', '.join(str(p) for p in pids)}); stop it first "
              "(two watchers contend for the telethon session)",
              file=sys.stderr, flush=True)

    old_plist = plist_dst.read_bytes() if plist_dst.exists() else None
    if old_plist == plist_data and is_loaded():
        print(f"watch-agent: already up to date ({plist_dst})")
        return 0

    if old_plist is not None and old_plist != plist_data:
        print(f"watch-agent: plist updated ({_sha(old_plist)} -> "
              f"{_sha(plist_data)})")
    if is_loaded():
        code, _out, err = run_launchctl(
            ["launchctl", "unload", str(plist_dst)])
        if code != 0:
            # KeepAlive 服务 unload 失败 = 旧 job 带着 stale env 继续跑，
            # 此时 load 新 plist 会造成双实例——宁可中止
            print(f"watch-agent: launchctl unload failed: "
                  f"{(err or '').strip()}; aborting before load",
                  file=sys.stderr)
            return 1
    plist_dst.parent.mkdir(parents=True, exist_ok=True)
    plist_dst.write_bytes(plist_data)
    code, out, err = run_launchctl(["launchctl", "load", str(plist_dst)])
    if code != 0:
        print(f"watch-agent: launchctl load failed: {(err or out).strip()}")
        return 1
    print(f"watch-agent: installed ({plist_dst})")
    print(f"watch-agent: loaded; channel/handoff env persisted "
          f"({len(env)} vars)")
    return 0


def status(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        print("watch-agent: not installed "
              "(run `knowledge-ingest telegram watch-agent install`)")
        return 0
    loaded = is_loaded()
    state = "loaded" if loaded else "NOT loaded (launchctl list miss)"
    try:
        cfg = plistlib.loads(plist_dst.read_bytes())
    except plistlib.InvalidFileException:
        print(f"watch-agent: plist corrupt ({plist_dst}); reinstall "
              "to regenerate")
        return 1
    env = cfg.get("EnvironmentVariables") or {}
    print(f"watch-agent: installed, {state}")
    print(f"watch-agent: plist {plist_dst} ({_sha(plist_dst.read_bytes())})")
    print(f"watch-agent: channel cmd = "
          f"{env.get('KI_TELEGRAM_LLM_CMD') or 'none'}")
    print(f"watch-agent: KI_TELEGRAM_HANDOFF = "
          f"{env.get('KI_TELEGRAM_HANDOFF') or 'unset'}")
    print(f"watch-agent: log {launchd_log_path(config)}")
    return 0


def uninstall(config: AppConfig) -> int:
    plist_dst = plist_install_path()
    if not plist_dst.exists():
        if is_loaded():
            # 手工删 plist 的经典残留：KeepAlive job 还在无限重启。
            # unload 需要文件在场——按 label 移除。
            code, _out, err = run_launchctl(
                ["launchctl", "remove", label_for()])
            if code != 0:
                print(f"watch-agent: launchctl remove failed: "
                      f"{(err or '').strip()}", file=sys.stderr)
                return 1
            print("watch-agent: removed orphan job by label "
                  f"({label_for()})")
            return 0
        print("watch-agent: not installed")
        return 0
    if is_loaded():
        code, _out, err = run_launchctl(
            ["launchctl", "unload", str(plist_dst)])
        if code != 0:
            print(f"watch-agent: launchctl unload failed: "
                  f"{(err or '').strip()}; trying remove by label",
                  file=sys.stderr)
            code, _out, err = run_launchctl(
                ["launchctl", "remove", label_for()])
            if code != 0:
                print(f"watch-agent: launchctl remove failed: "
                      f"{(err or '').strip()}; orphan may respawn — "
                      "reboot or remove manually", file=sys.stderr)
                return 1
    plist_dst.unlink()
    print(f"watch-agent: uninstalled (log kept at "
          f"{launchd_log_path(config)})")
    return 0
