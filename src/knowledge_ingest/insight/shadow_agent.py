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

import plistlib
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


def generate_insight_plist_bytes(*, ki_exe: str, config_path: str,
                                 environment: Mapping[str, str] | None = None
                                 ) -> bytes:
    unapproved = sorted(set(environment or ()) - set(APPROVED_PLIST_ENV_KEYS))
    if unapproved:
        raise ValueError(
            f"refusing to persist unapproved plist env keys: {unapproved}")
    cfg = {
        "Label": LABEL,
        "ProgramArguments": [
            ki_exe, "insight", "scan", "--provider", "telegram",
            "--config", config_path,
        ],
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
                         version_probe=None) -> ShadowInstallResult:
    """fail-closed install：先过 activation gate，再谈写 plist。

    非 PASS → 直接 refuse，`plist_path` 既不创建也不覆盖（父目录都不 mkdir）。
    """
    plist_path = default_plist_path() if plist_path is None else Path(plist_path)
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
                                        environment=environment)
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
