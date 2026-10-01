"""V3 M8 + R1.3: Insight Doctor——只读体检 + isolation 聚合。

R1.3（Issue #14 §5）在既有只读体检之上聚合两层隔离事实：

- D1 generic isolation：R1.1 factory / strict flag / 目录 / stage override /
  base command 绝对路径（唯一来源仍是 model_port factory，本模块只调用）；
- D2 provider profile：由 codex_isolation 的 runtime loader 重建
  `CodexIsolationProfile`（version/hash/CLI args/policy 全部来自 profile，
  本模块不重写任何 Codex 专有常量）；
- D3 preflight：直接调用 R1.2 `run_preflight()`，overall + 全部 FAIL/BLOCKED
  子项可见（BLOCKED 不掩盖并存 FAIL）；
- D4 command consistency：active `KI_INSIGHT_MODEL_CMD` 的 argv 必须与
  `certified_model_argv(profile)` 精确一致（只报位置/长度，不回显原文）；
- D5 certification state：PREPARED / PREFLIGHT_PASS / HOSTILE_SMOKE_PASS
  与 evidence ref 显式展示；
- D6 activation readiness：五项条件同时满足才 PASS。

未认证（R1.4 未把 profile 推进到 HOSTILE_SMOKE_PASS）时 activation_ready
非 PASS 是**正常安全行为**，绝不为让体检好看而降级规则。
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.insight.codex_isolation import (
    CertificationState,
    CodexIsolationProfile,
    CodexProfileLoadError,
    canonical_manifest_path,
    certified_model_argv,
    load_codex_profile_from_runtime,
    run_preflight,
)
from knowledge_ingest.insight.model_port import (
    STAGE_ENV_VARS,
    ModelIsolationConfigurationError,
    build_insight_model_port_from_env,
)

# ---- control-plane isolation env 契约（R1.1 冻结名的只读投影）----
ENV_REQUIRE_ISOLATION = "KI_INSIGHT_REQUIRE_ISOLATION"
ENV_MODEL_CMD = "KI_INSIGHT_MODEL_CMD"
ENV_ISOLATION_HOME = "KI_INSIGHT_ISOLATION_HOME"
ENV_ISOLATION_CODEX_HOME = "KI_INSIGHT_ISOLATION_CODEX_HOME"
ENV_ISOLATION_WORKSPACE = "KI_INSIGHT_ISOLATION_WORKSPACE"
ENV_ISOLATION_PROFILE_ID = "KI_INSIGHT_ISOLATION_PROFILE_ID"
ENV_ISOLATION_CHILD_PATH = "KI_INSIGHT_ISOLATION_CHILD_PATH"
ENV_CODEX_MANIFEST = "KI_INSIGHT_CODEX_MANIFEST"
ENV_DENY_REAL_HOME = "KI_INSIGHT_DENY_REAL_HOME"

_MAX_DETAIL_CHARS = 160


def _sanitize(detail: str) -> str:
    return " ".join(str(detail).split())[:_MAX_DETAIL_CHARS]


@dataclass(frozen=True)
class IsolationCheck:
    """单条隔离体检结论（layer ∈ D1..D6）。"""

    layer: str
    name: str
    status: str
    detail: str

    def row(self) -> tuple[str, str, str]:
        return (self.name, self.status, self.detail)


@dataclass(frozen=True)
class IsolationReadiness:
    """shadow activation 的判定输入（doctor D6 与 install gate 共用）。"""

    status: str                          # activation_ready: PASS / FAIL / BLOCKED
    detail: str
    checks: tuple[IsolationCheck, ...]
    profile: CodexIsolationProfile | None
    manifest_path: Path | None
    certification_state: str

    def rows(self) -> list[tuple[str, str, str]]:
        return [c.row() for c in self.checks] + [
            ("activation_ready", self.status, self.detail)]


# ---------- D1 generic isolation ----------

def _check_generic_isolation(env: Mapping[str, str]) -> tuple[list[IsolationCheck],
                                                            bool]:
    checks: list[IsolationCheck] = []
    strict = env.get(ENV_REQUIRE_ISOLATION, "").strip() == "1"
    checks.append(IsolationCheck(
        "D1", "D1-require-isolation", "PASS" if strict else "FAIL",
        f"{ENV_REQUIRE_ISOLATION}=1" if strict
        else f"{ENV_REQUIRE_ISOLATION} != 1 (strict isolation not required)"))
    overrides = [name for name in STAGE_ENV_VARS.values()
                 if env.get(name, "").strip()]
    checks.append(IsolationCheck(
        "D1", "D1-stage-override", "FAIL" if overrides else "PASS",
        "none" if not overrides
        else "forbidden under strict isolation: " + ",".join(overrides)))
    factory_ok = False
    try:
        port = build_insight_model_port_from_env(dict(env))
        factory_ok = bool(strict and port.isolation is not None
                          and port.isolation.require_strict)
        factory_detail = (f"R1.1 factory built strict profile "
                          f"({port.isolation.profile_id})" if factory_ok
                          else "factory returned a non-strict port")
        factory_status = "PASS" if factory_ok else "FAIL"
    except (ModelIsolationConfigurationError, ValueError) as exc:
        # R1.1 契约：ModelIsolationConfigurationError 消息只含变量名；
        # ValueError = factory 内 shlex 解析失败（R1.1 未类型化，本轮不改）。
        # 两种都必须降级为可见 FAIL，绝不让体检崩掉。
        factory_detail = _sanitize(
            f"R1.1 factory rejected: {type(exc).__name__}"
            if isinstance(exc, ValueError)
            else f"R1.1 factory rejected: {exc}")
        factory_status = "FAIL"
    checks.append(IsolationCheck("D1", "D1-factory", factory_status,
                                 factory_detail))
    home = env.get(ENV_ISOLATION_HOME, "").strip()
    workspace = env.get(ENV_ISOLATION_WORKSPACE, "").strip()
    dirs_ok = bool(home) and bool(workspace) \
        and Path(home).is_dir() and Path(workspace).is_dir()
    checks.append(IsolationCheck(
        "D1", "D1-isolation-dirs", "PASS" if dirs_ok else "FAIL",
        f"{ENV_ISOLATION_HOME}={Path(home).name or 'unset'}, "
        f"{ENV_ISOLATION_WORKSPACE}={Path(workspace).name or 'unset'}"))
    command = env.get(ENV_MODEL_CMD, "").strip()
    absolute = False
    if command:
        try:
            absolute = os.path.isabs(shlex.split(command)[0])
        except ValueError:
            absolute = False
    checks.append(IsolationCheck(
        "D1", "D1-base-command-absolute", "PASS" if absolute else "FAIL",
        "absolute binary path" if absolute
        else "KI_INSIGHT_MODEL_CMD unset, unparsable, or not an absolute path"))
    return checks, bool(strict and factory_ok and dirs_ok and absolute
                        and not overrides)


# ---------- D2 provider profile ----------

def _check_provider_profile(env: Mapping[str, str],
                            real_home: Path) -> tuple[list[IsolationCheck],
                                                      CodexIsolationProfile | None,
                                                      Path | None]:
    checks: list[IsolationCheck] = []
    raw_path = env.get(ENV_CODEX_MANIFEST, "").strip()
    if not raw_path:
        checks.append(IsolationCheck(
            "D2", "D2-manifest-path", "FAIL",
            f"{ENV_CODEX_MANIFEST} not configured"))
        return checks, None, None
    manifest_path = Path(raw_path)
    checks.append(IsolationCheck("D2", "D2-manifest-path", "PASS",
                                 str(manifest_path)))
    try:
        profile = load_codex_profile_from_runtime(
            manifest_path=manifest_path, real_home=real_home)
    except CodexProfileLoadError as exc:
        checks.append(IsolationCheck("D2", "D2-manifest-load", "FAIL",
                                     _sanitize(str(exc))))
        return checks, None, manifest_path
    checks.append(IsolationCheck(
        "D2", "D2-manifest-load", "PASS",
        f"schema v{profile.manifest_schema_version}, provider=codex"))
    canonical = canonical_manifest_path(profile)
    located = canonical == manifest_path
    checks.append(IsolationCheck(
        "D2", "D2-manifest-location", "PASS" if located else "FAIL",
        "canonical location under codex home" if located
        else "preflight reads a different manifest location"))
    env_profile_id = env.get(ENV_ISOLATION_PROFILE_ID, "").strip()
    id_ok = not env_profile_id or env_profile_id == profile.profile_id
    checks.append(IsolationCheck(
        "D2", "D2-profile-id", "PASS" if id_ok else "FAIL",
        profile.profile_id if id_ok
        else f"{ENV_ISOLATION_PROFILE_ID} does not match manifest profile_id"))
    checks.append(IsolationCheck(
        "D2", "D2-binary-version", "PASS",
        f"exact {profile.required_binary_version} pin"))
    checks.append(IsolationCheck(
        "D2", "D2-binary-identity", "PASS",
        f"{profile.binary_kind}, sha256={profile.binary_sha256[:12]}…"))
    checks.append(IsolationCheck(
        "D2", "D2-cli-args", "PASS",
        f"{len(profile.required_cli_args)} certified tokens: "
        + " ".join(profile.required_cli_args)))
    checks.append(IsolationCheck(
        "D2", "D2-provider-policy", "PASS",
        f"network={profile.network_policy}, fs={profile.filesystem_policy}, "
        f"web_apps={profile.web_apps_policy}"))
    return checks, profile, manifest_path


# ---------- D3 preflight ----------

def _check_preflight(profile: CodexIsolationProfile | None,
                     version_probe) -> tuple[list[IsolationCheck], str,
                                             list]:
    if profile is None:
        return ([IsolationCheck("D3", "D3-overall", "FAIL",
                                "preflight not run: provider profile unavailable")],
                "FAIL", [])
    overall, results = run_preflight(profile, version_probe=version_probe)
    checks = [IsolationCheck("D3", "D3-overall", overall,
                             f"R1.2 preflight overall={overall}")]
    # overall + 全部非 PASS 子项：BLOCKED 不得掩盖并存 FAIL
    for result in results:
        if result.status == "PASS":
            continue
        checks.append(IsolationCheck(
            "D3", f"D3-{result.code}", result.status,
            _sanitize(result.detail)))
    return checks, overall, results


# ---------- D4 command consistency ----------

def _check_command_consistency(
        env: Mapping[str, str],
        profile: CodexIsolationProfile | None) -> tuple[list[IsolationCheck], bool]:
    command = env.get(ENV_MODEL_CMD, "").strip()
    if not command:
        return ([IsolationCheck("D4", "D4-command-consistency", "FAIL",
                                "KI_INSIGHT_MODEL_CMD not configured")], False)
    if profile is None:
        return ([IsolationCheck(
            "D4", "D4-command-consistency", "BLOCKED",
            "certified command unknown: provider profile unavailable")], False)
    want = list(certified_model_argv(profile))
    try:
        actual = shlex.split(command)
    except ValueError:
        return ([IsolationCheck("D4", "D4-command-consistency", "FAIL",
                                "unparsable model command")], False)
    if actual == want:
        return ([IsolationCheck(
            "D4", "D4-command-consistency", "PASS",
            f"{len(actual)} argv tokens match certified command")], True)
    index = next((i for i, (a, b) in enumerate(zip(actual, want)) if a != b),
                 min(len(actual), len(want)))
    return ([IsolationCheck(
        "D4", "D4-command-consistency", "FAIL",
        f"argv drift: want {len(want)} tokens, got {len(actual)}, "
        f"first diff at index {index}")], False)


# ---------- D5 certification state ----------

def _check_certification(
        profile: CodexIsolationProfile | None) -> tuple[list[IsolationCheck], str,
                                                       bool]:
    if profile is None:
        return ([IsolationCheck("D5", "D5-certification-state", "BLOCKED",
                                "unknown (provider profile unavailable)"),
                 IsolationCheck("D5", "D5-hostile-smoke-evidence", "BLOCKED",
                                "unknown (provider profile unavailable)")],
                "", False)
    state = profile.certification_state
    state_ok = state is CertificationState.HOSTILE_SMOKE_PASS
    ref = profile.hostile_smoke_ref.strip()
    checks = [
        IsolationCheck("D5", "D5-certification-state",
                       "PASS" if state_ok else "FAIL", state.value),
        IsolationCheck("D5", "D5-hostile-smoke-evidence",
                       "PASS" if ref else "FAIL",
                       _sanitize(ref) if ref
                       else "absent (hostile smoke acceptance pending)"),
    ]
    return checks, state.value, bool(ref)


# ---------- D6 activation readiness ----------

def collect_isolation_readiness(
        env: Mapping[str, str] | None = None, *,
        real_home: Path | None = None,
        version_probe=None) -> IsolationReadiness:
    """聚合 D1–D6 并给出 activation_ready（shadow install gate 的唯一判定源）。"""
    env = dict(os.environ) if env is None else dict(env)
    if real_home is None:
        real_home = Path(env.get(ENV_DENY_REAL_HOME, "").strip() or Path.home())

    d1_checks, d1_ok = _check_generic_isolation(env)
    d2_checks, profile, manifest_path = _check_provider_profile(env, real_home)
    d3_checks, preflight_overall, preflight_results = _check_preflight(
        profile, version_probe)
    d4_checks, command_ok = _check_command_consistency(env, profile)
    d5_checks, state, ref_ok = _check_certification(profile)

    # provider 条件 = profile 重建完整（含 manifest 位置单一来源）+ preflight。
    # 否则"loader 读的那份 manifest 与 preflight 校验的那份不是同一份"会
    # 让未验证的认证声明通过 activation gate。
    d2_broken = any(c.status == "FAIL" for c in d2_checks)
    if profile is None or d2_broken:
        preflight_status = "FAIL"
    elif preflight_overall == "PASS":
        preflight_status = "PASS"
    elif any(result.status == "FAIL" for result in preflight_results):
        preflight_status = "FAIL"
    else:
        preflight_status = "BLOCKED"
    state_ok = state == CertificationState.HOSTILE_SMOKE_PASS.value
    conditions = (
        ("strict-generic-isolation", "PASS" if d1_ok else "FAIL"),
        ("provider-preflight-pass", preflight_status),
        ("certified-command-exact",
         "PASS" if command_ok else
         ("BLOCKED" if profile is None else "FAIL")),
        ("certification-hostile-smoke-pass",
         "BLOCKED" if profile is None else ("PASS" if state_ok else "FAIL")),
        ("hostile-smoke-evidence-present",
         "BLOCKED" if profile is None else ("PASS" if ref_ok else "FAIL")),
    )
    unmet = [f"{name}={status}" for name, status in conditions
             if status != "PASS"]
    if not unmet:
        status, detail = "PASS", "all activation conditions satisfied"
    else:
        statuses = {s for _, s in conditions}
        status = "FAIL" if "FAIL" in statuses else "BLOCKED"
        detail = ", ".join(unmet)
    checks = (tuple(d1_checks) + tuple(d2_checks) + tuple(d3_checks)
              + tuple(d4_checks) + tuple(d5_checks))
    return IsolationReadiness(status=status, detail=detail, checks=checks,
                              profile=profile, manifest_path=manifest_path,
                              certification_state=state)


def run_insight_doctor(config: AppConfig, store, *,
                       env: Mapping[str, str] | None = None,
                       real_home: Path | None = None,
                       version_probe=None) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    root = Path(config.pipeline_root) / "insight"
    writable = root.is_dir() and os.access(root, os.W_OK)
    checks.append(("insight_root", "PASS" if writable else "WARN",
                   str(root)))
    if store is not None:
        version = store.user_version()
        checks.append(("schema_version",
                       "PASS" if version >= 1 else "FAIL",
                       f"user_version={version}"))
    else:
        checks.append(("schema_version", "FAIL", "store is None"))
    model_cmd = os.environ.get("KI_INSIGHT_MODEL_CMD", "").strip()
    checks.append(("model_cmd", "PASS" if model_cmd else "WARN",
                   model_cmd[:60] or "not configured"))
    retrieval_cmd = os.environ.get("KI_INSIGHT_RETRIEVAL_CMD", "").strip()
    checks.append(("retrieval_cmd", "PASS" if retrieval_cmd else "WARN",
                   retrieval_cmd[:60] or "not configured"))
    if store is not None:
        pending = store._conn.execute(
            "SELECT COUNT(*) FROM cognition_proposals "
            "WHERE gate_status = 'pending'").fetchone()[0]
        checks.append(("pending_gates", "INFO", str(pending)))
        blocked = store._conn.execute(
            "SELECT COUNT(*) FROM insight_runs "
            "WHERE status = 'blocked'").fetchone()[0]
        checks.append(("blocked_runs", "INFO", str(blocked)))
    readiness = collect_isolation_readiness(env, real_home=real_home,
                                            version_probe=version_probe)
    checks.extend(readiness.rows())
    return checks
