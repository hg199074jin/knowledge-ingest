"""R1.3: Shadow Agent——activation gate / plist env 契约 / 覆盖安全（§6–§10）。

覆盖（Issue #14 §11 shadow agent）：
1. readiness 失败 → 写文件之前 refuse；
2. PREFLIGHT_PASS 不能 install（R1.4 未认证前是 Human Gate 防线）；
3. 只有 HOSTILE_SMOKE_PASS 才进入写 plist 路径；
4. 生成的 plist env 只有 approved non-secret keys；
5. plist model command 与 certified command 精确一致；
6. 无 auth / token / stage override；
7. 现有 plist + readiness 失败 → bytes/hash 不变；
8. V2 label/路径零接触；
9. status 不把"文件存在"当作 ready。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import plistlib
import types
from pathlib import Path

import pytest

from knowledge_ingest.cli import (
    _production_executable,
    _require_production_config_path,
)
from knowledge_ingest.insight.codex_isolation import (
    CertificationState,
    build_certified_model_command,
)
from knowledge_ingest.insight.shadow_agent import (
    APPROVED_PLIST_ENV_KEYS,
    LABEL,
    PLIST_FILENAME,
    ShadowInstallResult,
    bootout_argv,
    bootstrap_argv,
    build_plist_environment,
    generate_insight_plist_bytes,
    install_shadow_agent,
    launchd_status,
    load_shadow_agent,
    print_argv,
    remove_shadow_agent_plist,
    shadow_agent_status,
    uninstall_notice,
    unload_shadow_agent,
)

from .conftest import AUTH_SECRET

KI_EXE = "/opt/homebrew/bin/knowledge-ingest"
CONFIG = "/Volumes/ORICO/Projects/knowledge-ingest/config.yaml"


def _install(certified, tmp_path: Path, **build_kwargs):
    assets = certified(**build_kwargs)
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    result = install_shadow_agent(
        plist_path=plist_path, ki_exe=KI_EXE, config_path=CONFIG,
        env=assets.env, real_home=assets.real_home,
        version_probe=assets.version_probe)
    return assets, plist_path, result


# ---------- 1/2/3：fail-closed install ----------

def test_install_refused_without_any_isolation_config(tmp_path):
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    result = install_shadow_agent(plist_path=plist_path, ki_exe=KI_EXE,
                                  config_path=CONFIG, env={},
                                  real_home=Path("/tmp"),
                                  version_probe=lambda p: "")
    assert result.installed is False
    assert result.wrote is False
    assert result.readiness_status == "FAIL"
    assert not plist_path.exists()
    assert not plist_path.parent.exists()      # refuse 时连目录都不 mkdir


def test_install_refused_when_preflight_only_pass(certified, tmp_path):
    """R1.4 尚未把 profile 推进到 HOSTILE_SMOKE_PASS → 人工 install 也必须失败。"""
    _assets, plist_path, result = _install(
        certified, tmp_path,
        state=CertificationState.PREFLIGHT_PASS, evidence="")
    assert result.installed is False
    assert not plist_path.exists()
    assert "activation_ready=FAIL" in result.detail


def test_install_refused_when_preflight_blocked(certified, tmp_path):
    _assets, plist_path, result = _install(certified, tmp_path, auth=False)
    assert result.installed is False
    assert result.readiness_status == "BLOCKED"
    assert not plist_path.exists()


def test_install_refused_when_command_drift(certified, tmp_path):
    assets = certified()
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    drifted = assets.env_with(
        KI_INSIGHT_MODEL_CMD=build_certified_model_command(assets.profile)
        .replace("--disable web_search_request", ""))
    result = install_shadow_agent(plist_path=plist_path, ki_exe=KI_EXE,
                                  config_path=CONFIG, env=drifted,
                                  real_home=assets.real_home,
                                  version_probe=assets.version_probe)
    assert result.installed is False
    assert not plist_path.exists()


def test_install_writes_only_after_hostile_smoke_pass(certified, tmp_path):
    assets, plist_path, result = _install(certified, tmp_path)
    assert result.installed is True and result.wrote is True
    assert plist_path.is_file()
    assert result.environment
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == LABEL
    assert parsed["ProgramArguments"] == [
        KI_EXE, "insight", "scan", "--provider", "telegram",
        "--config", CONFIG]
    assert parsed["EnvironmentVariables"]["KI_INSIGHT_CODEX_MANIFEST"] == \
        str(assets.manifest_path)


# ---------- 4/5/6：plist env 契约 ----------

def test_plist_env_contains_only_approved_keys(certified, tmp_path):
    _assets, plist_path, _result = _install(certified, tmp_path)
    environment = plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
    assert set(environment) == set(APPROVED_PLIST_ENV_KEYS)
    assert environment["KI_INSIGHT_REQUIRE_ISOLATION"] == "1"
    for key in ("KI_INSIGHT_ISOLATION_PROFILE_ID", "KI_INSIGHT_ISOLATION_HOME",
                "KI_INSIGHT_ISOLATION_CODEX_HOME",
                "KI_INSIGHT_ISOLATION_WORKSPACE",
                "KI_INSIGHT_ISOLATION_CHILD_PATH", "KI_INSIGHT_CODEX_MANIFEST",
                "KI_INSIGHT_DENY_REAL_HOME"):
        assert environment[key]


def test_plist_env_has_no_secret_and_no_stage_override(certified, tmp_path):
    _assets, plist_path, _result = _install(certified, tmp_path)
    blob = plist_path.read_bytes()
    parsed = plistlib.loads(blob)
    environment = parsed["EnvironmentVariables"]
    assert "PATH" not in environment              # 不复制宿主 PATH
    for name in environment:
        assert "TOKEN" not in name.upper()
        assert "KEY" not in name.upper()
        assert "SECRET" not in name.upper()
        assert not name.startswith("KI_INSIGHT_CANDIDATE_CMD")
    text = blob.decode("utf-8")
    assert AUTH_SECRET not in text
    assert "OPENAI_API_KEY" not in text
    assert "auth.json" not in text
    for stage in ("KI_INSIGHT_CANDIDATE_CMD", "KI_INSIGHT_VALUE_CMD",
                  "KI_INSIGHT_THINK_CMD", "KI_INSIGHT_CRITIC_CMD"):
        assert stage not in text


def test_plist_model_command_is_exact_certified_command(certified, tmp_path):
    assets, plist_path, _result = _install(certified, tmp_path)
    environment = plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
    command = environment["KI_INSIGHT_MODEL_CMD"]
    assert command == build_certified_model_command(assets.profile)
    import shlex
    argv = shlex.split(command)
    assert argv[0] == str(assets.profile.binary_path)
    assert argv[1] == "exec"
    assert argv[-1] == "-"
    for flag in ("--disable apps", "--disable web_search",
                 "--disable web_search_request"):
        assert flag in " ".join(argv)
    assert "-c orchestrator.skills.enabled=false" in " ".join(argv)
    assert "--skip-git-repo-check" in argv


def test_build_plist_environment_uses_profile_single_source(certified):
    assets = certified()
    environment = build_plist_environment(
        assets.profile, manifest_path=assets.manifest_path)
    assert environment["KI_INSIGHT_MODEL_CMD"] == \
        build_certified_model_command(assets.profile)
    assert environment["KI_INSIGHT_ISOLATION_HOME"] == \
        str(assets.profile.isolated_home)
    assert environment["KI_INSIGHT_ISOLATION_CHILD_PATH"] == "/usr/bin:/bin"


def test_generate_plist_rejects_unapproved_env_keys():
    with pytest.raises(ValueError):
        generate_insight_plist_bytes(
            ki_exe=KI_EXE, config_path=CONFIG,
            environment={"OPENAI_API_KEY": "sk-leak"})


def test_generate_plist_without_env_stays_backward_compatible():
    parsed = plistlib.loads(generate_insight_plist_bytes(
        ki_exe=KI_EXE, config_path=CONFIG))
    assert "EnvironmentVariables" not in parsed


# ---------- 7：覆盖安全 ----------

def test_existing_plist_unchanged_when_readiness_fails(certified, tmp_path):
    assets = certified()
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(generate_insight_plist_bytes(
        ki_exe="/usr/bin/legacy-ki", config_path="/legacy/config.yaml"))
    before_bytes = plist_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()

    env = assets.env_with(
        KI_INSIGHT_MODEL_CMD=build_certified_model_command(assets.profile)
        .replace("--disable apps", ""))
    result = install_shadow_agent(plist_path=plist_path, ki_exe=KI_EXE,
                                  config_path=CONFIG, env=env,
                                  real_home=assets.real_home,
                                  version_probe=assets.version_probe)
    assert result.installed is False
    assert plist_path.read_bytes() == before_bytes
    assert hashlib.sha256(plist_path.read_bytes()).hexdigest() == before_hash
    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"][0] == \
        "/usr/bin/legacy-ki"


def test_existing_plist_unchanged_when_state_not_hostile(certified, tmp_path):
    _assets, plist_path, result = _install(
        certified, tmp_path, state=CertificationState.PREPARED, evidence="")
    assert result.installed is False
    assert not plist_path.exists()


# ---------- 8：V2 零接触 ----------

def test_v2_launch_agents_are_never_touched(certified, tmp_path):
    v2_dir = tmp_path / "LaunchAgents"
    v2_dir.mkdir(parents=True)
    v2_sentinels = {
        v2_dir / "com.sandro.ki-telegram-watch.plist": b"<v2-watch/>",
        v2_dir / "com.sandro.ki-telegram-digest.plist": b"<v2-digest/>",
    }
    for path, blob in v2_sentinels.items():
        path.write_bytes(blob)
    _assets, plist_path, _result = _install(certified, tmp_path)
    assert plist_path.is_file()               # 只新增 V3 一个文件
    assert {p.name for p in v2_dir.iterdir()} == \
        {PLIST_FILENAME, *(p.name for p in v2_sentinels)}
    for path, blob in v2_sentinels.items():
        assert path.read_bytes() == blob
    for line in uninstall_notice():
        assert line


def test_uninstall_is_advisory_only(tmp_path):
    """本轮不做 production 操作：不删文件、不 launchctl。"""
    assert all(isinstance(line, str) for line in uninstall_notice())
    assert "manual" in " ".join(uninstall_notice())


# ---------- 9：status 语义 ----------

def test_status_does_not_treat_plist_existence_as_ready(certified, tmp_path):
    assets = certified(state=CertificationState.PREFLIGHT_PASS, evidence="")
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(generate_insight_plist_bytes(
        ki_exe=KI_EXE, config_path=CONFIG))
    status = shadow_agent_status(plist_path=plist_path, env=assets.env,
                                 real_home=assets.real_home,
                                 version_probe=assets.version_probe)
    assert status["plist_exists"] == "yes"
    assert status["activation_ready"] != "PASS"
    assert "does NOT imply" in status["note"]


def test_status_reports_profile_and_state_when_ready(certified, tmp_path):
    assets = certified()
    plist_path = tmp_path / "LaunchAgents" / PLIST_FILENAME
    status = shadow_agent_status(plist_path=plist_path, env=assets.env,
                                 real_home=assets.real_home,
                                 version_probe=assets.version_probe)
    assert status["profile_id"] == assets.profile.profile_id
    assert status["certification_state"] == "HOSTILE_SMOKE_PASS"
    assert status["activation_ready"] == "PASS"
    assert status["label"] == LABEL


def test_status_without_profile_is_unavailable(tmp_path):
    status = shadow_agent_status(plist_path=tmp_path / "missing.plist", env={},
                                 real_home=Path("/tmp"),
                                 version_probe=lambda p: "")
    assert status["profile_id"] == "unavailable"
    assert status["certification_state"] == "unavailable"
    assert status["activation_ready"] == "FAIL"


def test_install_result_is_frozen_dataclass():
    result = ShadowInstallResult(
        installed=False, wrote=False, plist_path=Path("/tmp/x.plist"),
        readiness_status="FAIL", detail="refused")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.installed = True        # type: ignore[misc]


# ---------- R1.5 §14：V3-only launchctl lifecycle ----------

class FakeLaunchctl:
    """记录 argv 的 launchctl 替身（不触碰真实 launchd）。"""

    def __init__(self, rc=0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=self.rc, stderr="",
                                     stdout="")


def test_bootstrap_argv_is_v3_gui_domain_no_shell():
    argv = bootstrap_argv(Path("/tmp/x.plist"))
    assert argv[:2] == ["launchctl", "bootstrap"]
    assert argv[2] == f"gui/{os.getuid()}"
    assert argv[3] == "/tmp/x.plist"
    assert all("*" not in part for part in argv)      # 无 wildcard


def test_bootout_argv_targets_exact_v3_label():
    argv = bootout_argv()
    assert argv == ["launchctl", "bootout",
                    f"gui/{os.getuid()}/{LABEL}"]
    assert "telegram" not in " ".join(argv)          # 绝不涉及 V2 label


def test_print_argv_targets_exact_v3_label():
    assert print_argv() == ["launchctl", "print",
                            f"gui/{os.getuid()}/{LABEL}"]


def test_load_requires_existing_plist(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_shadow_agent(plist_path=tmp_path / "absent.plist")


def test_load_invokes_bootstrap_with_fixed_argv(tmp_path):
    plist = tmp_path / PLIST_FILENAME
    plist.write_bytes(b"<plist/>")
    fake = FakeLaunchctl()
    result = load_shadow_agent(plist_path=plist, run_fn=fake)
    assert fake.calls == [bootstrap_argv(plist)]
    assert result["rc"] == 0


def test_unload_invokes_bootout_v3_only():
    fake = FakeLaunchctl()
    result = unload_shadow_agent(run_fn=fake)
    assert fake.calls == [bootout_argv()]
    assert result["action"] == "bootout"


def test_launchd_status_reports_loaded_by_rc():
    assert launchd_status(run_fn=FakeLaunchctl(rc=0))["loaded"] is True
    assert launchd_status(run_fn=FakeLaunchctl(rc=113))["loaded"] is False


def test_remove_plist_refuses_non_v3_file(tmp_path):
    v2 = tmp_path / "com.sandro.ki-telegram-watch.plist"
    v2.write_bytes(b"<v2/>")
    with pytest.raises(ValueError):
        remove_shadow_agent_plist(plist_path=v2)
    assert v2.is_file()                              # V2 未被触碰


def test_remove_plist_deletes_only_v3(tmp_path):
    v3 = tmp_path / PLIST_FILENAME
    v2 = tmp_path / "com.sandro.ki-telegram-digest.plist"
    v3.write_bytes(b"<v3/>")
    v2.write_bytes(b"<v2/>")
    result = remove_shadow_agent_plist(plist_path=v3)
    assert result["existed"] is True
    assert not v3.exists() and v2.is_file()


def test_lifecycle_never_mentions_v2_labels(tmp_path):
    fake = FakeLaunchctl()
    plist = tmp_path / PLIST_FILENAME
    plist.write_bytes(b"<plist/>")
    load_shadow_agent(plist_path=plist, run_fn=fake)
    unload_shadow_agent(run_fn=fake)
    launchd_status(run_fn=fake)
    blob = " ".join(" ".join(call) for call in fake.calls)
    assert "ki-telegram" not in blob
    assert LABEL in blob


# ---------- R1.5 §8：production config path ----------

def test_production_config_rejects_example_file():
    with pytest.raises(SystemExit) as exc:
        _require_production_config_path(
            argparse.Namespace(config="/tmp/config.example.yaml"))
    assert "config.example.yaml" in str(exc.value)


def test_production_config_requires_explicit_flag():
    with pytest.raises(SystemExit):
        _require_production_config_path(argparse.Namespace(config=None))


def test_production_config_requires_existing_file(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _require_production_config_path(
            argparse.Namespace(config=str(tmp_path / "absent.yaml")))
    assert "config not found" in str(exc.value)


def test_production_config_accepts_real_absolute_file(tmp_path):
    real = tmp_path / "config.yaml"
    real.write_text("pipeline_root: /tmp\n", encoding="utf-8")
    assert _require_production_config_path(
        argparse.Namespace(config=str(real))) == str(real)


def test_production_executable_is_absolute_existing():
    assert Path(_production_executable()).is_absolute()
    assert Path(_production_executable()).is_file()
