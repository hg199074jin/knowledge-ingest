"""V3 M8 + R1.3: Shadow Agent——LaunchAgent 调度 + provider isolation 激活门。

R1.3（Issue #14 §6–§10）把 R1.1 generic isolation + R1.2 Codex certified
profile/preflight 接到**真正的 activation path**（原实现只有 plist generator，
写文件逻辑在 cli.py）：

- `evaluate_activation_readiness()`：薄委托 doctor 的 `collect_isolation_readiness`
  （唯一判定源，本模块不重算任何 Codex 语义）；
- `install_shadow_agent()`：readiness 非 PASS → **在写任何字节之前** refuse；
  现有 plist 保持 bytes/hash 不变（R1.3 关键 Human Gate 防线：R1.4 未把
  profile 推进到 HOSTILE_SMOKE_PASS 前，人工执行 install 也必须失败）；
- `build_plist_environment()`：只持久化 approved non-secret control-plane 值，
  `KI_INSIGHT_MODEL_CMD` 必须来自 `build_certified_model_command(profile)`。

本轮不执行任何 production 操作（不 launchctl、不删除 plist、不改 V2）。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from knowledge_ingest.insight.codex_isolation import (
    CodexIsolationProfile,
    build_certified_model_command,
)
from knowledge_ingest.insight.doctor import (
    ENV_CODEX_MANIFEST,
    ENV_DENY_REAL_HOME,
    ENV_ISOLATION_CHILD_PATH,
    ENV_ISOLATION_CODEX_HOME,
    ENV_ISOLATION_HOME,
    ENV_ISOLATION_PROFILE_ID,
    ENV_ISOLATION_WORKSPACE,
    ENV_MODEL_CMD,
    ENV_REQUIRE_ISOLATION,
    IsolationReadiness,
    collect_isolation_readiness,
)

LABEL = "com.sandro.ki-insight-shadow"
START_INTERVAL_SECONDS = 300
PLIST_FILENAME = f"{LABEL}.plist"
DEFAULT_CHILD_PATH = "/usr/bin:/bin"

#: V3 shadow plist 允许持久化的 control-plane 键（全部非 secret）。
#: allowlist 而非 deny list：新增键必须显式登记，杜绝 wildcard/ambient 抄写。
APPROVED_PLIST_ENV_KEYS = (
    ENV_REQUIRE_ISOLATION,
    ENV_MODEL_CMD,
    ENV_ISOLATION_PROFILE_ID,
    ENV_ISOLATION_HOME,
    ENV_ISOLATION_CODEX_HOME,
    ENV_ISOLATION_WORKSPACE,
    ENV_ISOLATION_CHILD_PATH,
    ENV_CODEX_MANIFEST,
    ENV_DENY_REAL_HOME,
)

#: V2 labels/路径：R1.3 零接触，安装/卸载路径都不得读写它们。
V2_UNTOUCHED_LABELS = ("com.*.ki-telegram-watch", "com.*.ki-telegram-digest")


def insight_label() -> str:
    return LABEL


def default_plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def default_plist_path() -> Path:
    return default_plist_dir() / PLIST_FILENAME


def build_plist_environment(profile: CodexIsolationProfile, *,
                            manifest_path: Path,
                            child_path: str = DEFAULT_CHILD_PATH) -> dict[str, str]:
    """生成 V3 plist 的 non-secret control-plane env（§7 契约）。

    `KI_INSIGHT_MODEL_CMD` 由 certified command helper 生成，而非抄当前 shell；
    不含 auth token / auth.json 内容 / API key / GitHub token / SSH credential /
    宿主 PATH / stage overrides。`KI_INSIGHT_DENY_REAL_HOME` 只在 control plane
    传递（real-home deny 事实），不进 model child env（R1.1 allowlist 保证）。
    """
    environment = {
        ENV_REQUIRE_ISOLATION: "1",
        ENV_MODEL_CMD: build_certified_model_command(profile),
        ENV_ISOLATION_PROFILE_ID: profile.profile_id,
        ENV_ISOLATION_HOME: str(profile.isolated_home),
        ENV_ISOLATION_CODEX_HOME: str(profile.codex_home),
        ENV_ISOLATION_WORKSPACE: str(profile.workspace),
        ENV_ISOLATION_CHILD_PATH: child_path,
        ENV_CODEX_MANIFEST: str(manifest_path),
        ENV_DENY_REAL_HOME: str(profile.deny_real_home),
    }
    unknown = sorted(set(environment) - set(APPROVED_PLIST_ENV_KEYS))
    if unknown:  # pragma: no cover — allowlist 与生成逻辑同文件维护
        raise ValueError(f"unapproved plist environment keys: {unknown}")
    return environment


class ShadowAgentActivationError(RuntimeError):
    """R1.5 §6：load 前 activation gate 未 PASS（fail-closed，绝不 bootstrap）。"""


def generate_insight_plist_bytes(*, ki_exe: str, config_path: str,
                                 environment: Mapping[str, str] | None = None,
                                 scan_limit: int | None = None) -> bytes:
    """生成 V3 plist。

    `scan_limit` 写入 ProgramArguments（`--limit N`）：canary 必须**有界**，且
    boundedness 必须表达在部署资产里而不是只存在于人工命令中（Issue #28 §6）。
    """
    unapproved = sorted(set(environment or ()) - set(APPROVED_PLIST_ENV_KEYS))
    if unapproved:
        raise ValueError(
            f"refusing to persist unapproved plist env keys: {unapproved}")
    arguments = [ki_exe, "insight", "scan", "--provider", "telegram",
                 "--config", config_path]
    if scan_limit is not None:
        if isinstance(scan_limit, bool) or not isinstance(scan_limit, int) \
                or scan_limit < 1:
            raise ValueError("scan_limit must be a positive int")
        arguments += ["--limit", str(scan_limit)]
    cfg = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "StartInterval": START_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "StandardOutPath": "/Users/sandro/Library/Logs/knowledge-ingest/"
                           "insight-shadow.log",
        "StandardErrorPath": "/Users/sandro/Library/Logs/knowledge-ingest/"
                             "insight-shadow.log",
    }
    if environment is not None:
        cfg["EnvironmentVariables"] = dict(environment)
    return plistlib.dumps(cfg, fmt=plistlib.FMT_XML)


# ---------- activation readiness / install gate ----------

def evaluate_activation_readiness(env: Mapping[str, str] | None = None, *,
                                  real_home: Path | None = None,
                                  version_probe=None) -> IsolationReadiness:
    """薄委托 doctor 聚合（唯一判定源；本模块不重算 Codex 语义）。"""
    return collect_isolation_readiness(env, real_home=real_home,
                                       version_probe=version_probe)


@dataclass(frozen=True)
class ShadowInstallResult:
    installed: bool
    wrote: bool
    plist_path: Path
    readiness_status: str
    detail: str
    checks: tuple = ()
    environment: tuple[tuple[str, str], ...] = ()

    def rows(self) -> list[str]:
        return [f"[{c.status:4}] {c.name}: {c.detail}" for c in self.checks]


def install_shadow_agent(*, plist_path: Path | None = None,
                         ki_exe: str,
                         config_path: str,
                         env: Mapping[str, str] | None = None,
                         real_home: Path | None = None,
                         child_path: str = DEFAULT_CHILD_PATH,
                         scan_limit: int | None = None,
                         version_probe=None) -> ShadowInstallResult:
    """fail-closed install：先过 activation gate，再谈写 plist。

    非 PASS → 直接 refuse，`plist_path` 既不创建也不覆盖（父目录都不 mkdir）。
    非法 `scan_limit` 同样在**任何副作用之前**拒绝并返回 refuse（本函数不抛异常，
    与 R1.3/R1.4 确立的"install 永远返回判定"契约一致）。
    """
    plist_path = default_plist_path() if plist_path is None else Path(plist_path)
    if scan_limit is not None and (
            isinstance(scan_limit, bool) or not isinstance(scan_limit, int)
            or scan_limit < 1):
        return ShadowInstallResult(
            installed=False, wrote=False, plist_path=plist_path,
            readiness_status="REFUSED",
            detail="refused: scan_limit must be a positive int")
    readiness = evaluate_activation_readiness(env, real_home=real_home,
                                              version_probe=version_probe)
    if readiness.status != "PASS" or readiness.profile is None \
            or readiness.manifest_path is None:
        return ShadowInstallResult(
            installed=False, wrote=False, plist_path=plist_path,
            readiness_status=readiness.status,
            detail=f"refused: activation_ready={readiness.status} "
                   f"({readiness.detail})",
            checks=readiness.checks)
    profile = readiness.profile
    environment = build_plist_environment(
        profile, manifest_path=readiness.manifest_path, child_path=child_path)
    blob = generate_insight_plist_bytes(ki_exe=ki_exe, config_path=config_path,
                                        environment=environment,
                                        scan_limit=scan_limit)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(blob)
    return ShadowInstallResult(
        installed=True, wrote=True, plist_path=plist_path,
        readiness_status=readiness.status,
        detail=f"installed: profile={profile.profile_id} "
               f"state={profile.certification_state.value}",
        checks=readiness.checks,
        environment=tuple(sorted(environment.items())))


# ---------- status / uninstall（不做 production 操作）----------

def shadow_agent_status(*, plist_path: Path | None = None,
                       env: Mapping[str, str] | None = None,
                       real_home: Path | None = None,
                       version_probe=None) -> dict[str, str]:
    """状态展示：plist 存在 ≠ isolation ready（§10）。"""
    plist_path = default_plist_path() if plist_path is None else Path(plist_path)
    readiness = evaluate_activation_readiness(env, real_home=real_home,
                                              version_probe=version_probe)
    profile = readiness.profile
    return {
        "label": LABEL,
        "plist_path": str(plist_path),
        "plist_exists": "yes" if plist_path.is_file() else "no",
        "profile_id": profile.profile_id if profile else "unavailable",
        "certification_state": (readiness.certification_state or "unavailable"),
        "activation_ready": readiness.status,
        "activation_detail": readiness.detail,
        "note": "plist file existence does NOT imply isolation readiness",
        "v2_untouched": ",".join(V2_UNTOUCHED_LABELS),
    }


def uninstall_notice() -> tuple[str, ...]:
    """V3-only 卸载提示；本轮不执行 launchctl / 不删文件（不做 production 操作）。"""
    return (
        (f"{LABEL} (V3 shadow agent): unload is manual "
         f"(`launchctl bootout gui/$UID/{LABEL}`), R1.3 performs no "
         "production action"),
        f"V2 untouched: {', '.join(V2_UNTOUCHED_LABELS)}")


def status_lines(status: Mapping[str, str]) -> list[str]:
    return [f"{key}: {value}" for key, value in status.items()]


# ---------- V3-only launchctl lifecycle（R1.5 §14）----------

def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def bootstrap_argv(plist_path: Path) -> list[str]:
    """V3 label only；argv 固定构造，无 shell、无 wildcard、无 V2 label。"""
    return ["launchctl", "bootstrap", launchd_domain(), str(plist_path)]


def bootout_argv() -> list[str]:
    return ["launchctl", "bootout",
            f"{launchd_domain()}/{LABEL}"]


def print_argv() -> list[str]:
    return ["launchctl", "print",
            f"{launchd_domain()}/{LABEL}"]


def _default_launchctl(run: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(run, capture_output=True, text=True, timeout=60,
                          check=False)


def load_shadow_agent(*, plist_path: Path | None = None,
                      env: Mapping[str, str] | None = None,
                      real_home: Path | None = None,
                      version_probe=None,
                      run_fn=None) -> dict[str, object]:
    """bootstrap V3 label（`launchctl bootstrap gui/$UID <plist>`）。

    R1.5 §6：write 与 **load** 都必须在 activation gate 之后。plist 可能是旧认证
    身份留下的陈旧资产，因此 load 独立复核 readiness，绝不假设"文件存在=已认证"。
    """
    plist_path = default_plist_path() if plist_path is None else Path(plist_path)
    if not plist_path.is_file():
        raise FileNotFoundError(f"V3 plist not found: {plist_path.name}")
    readiness = evaluate_activation_readiness(env, real_home=real_home,
                                              version_probe=version_probe)
    if readiness.status != "PASS":
        raise ShadowAgentActivationError(
            f"refusing to load: activation_ready={readiness.status} "
            f"({readiness.detail})")
    run = run_fn or _default_launchctl
    argv = bootstrap_argv(plist_path)
    proc = run(argv)
    return {"action": "bootstrap", "argv": argv, "rc": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:200]}


def unload_shadow_agent(*, run_fn=None) -> dict[str, object]:
    """bootout V3 label only（rollback 的第一步）。"""
    run = run_fn or _default_launchctl
    argv = bootout_argv()
    proc = run(argv)
    return {"action": "bootout", "argv": argv, "rc": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:200]}


def remove_shadow_agent_plist(*, plist_path: Path | None = None) -> dict[str, object]:
    """删除 V3 plist（只允许 V3 文件名；绝不触碰其它 LaunchAgent）。"""
    plist_path = default_plist_path() if plist_path is None else Path(plist_path)
    if plist_path.name != PLIST_FILENAME:
        raise ValueError(f"refusing to remove non-V3 plist: {plist_path.name}")
    existed = plist_path.is_file()
    if existed:
        plist_path.unlink()
    return {"action": "remove_plist", "path": str(plist_path),
            "existed": existed, "rc": 0}


def launchd_status(*, run_fn=None) -> dict[str, object]:
    """V3 launchd job 状态（`launchctl print` 的 exit code 解读）。"""
    run = run_fn or _default_launchctl
    argv = print_argv()
    proc = run(argv)
    return {"action": "print", "argv": argv, "rc": proc.returncode,
            "loaded": proc.returncode == 0}
