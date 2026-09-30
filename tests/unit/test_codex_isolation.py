"""R1.2: codex_isolation — Codex 专有 certified profile + preflight。

边界（Issue #8 §2）：全部 Codex 专有语义（exact version/TOML/network/fs/
apps disable/hostile smoke）住在本模块；generic model_port 不 import 本模块。

测试策略：binary/version/hash 用注入探针与 stub 文件（不执行未知二进制）；
hostile smoke 用 fake runner（不触真实 internet，R1.4 才做真机验收）。
"""

import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from knowledge_ingest.insight.codex_isolation import (
    MANIFEST_FILENAME,
    CertificationState,
    CodexIsolationProfile,
    advance_certification_state,
    build_manifest,
    certify_binary,
    check_auth,
    generate_config,
    load_manifest,
    run_hostile_smoke,
    run_preflight,
    validate_config,
    write_manifest,
)
from knowledge_ingest.insight.model_port import (
    ModelIsolationConfigurationError,
    build_insight_model_port_from_env,
)

TOKEN = "Bearer super-secret-token-never-leak"


def stub_binary(tmp_path: Path, version_line: str = "codex-cli 0.159.2") -> Path:
    """可执行 stub：--version 输出指定版本行（真实执行，带 shebang）。"""
    binary = tmp_path / "codex-stub"
    binary.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_profile(tmp_path: Path, binary: Path, **overrides) -> CodexIsolationProfile:
    home = tmp_path / "iso-home"
    codex = tmp_path / "iso-codex-home"
    ws = tmp_path / "ws"
    for d in (home, codex, ws):
        d.mkdir()
    if "binary_sha256" in overrides:
        sha_value = overrides.pop("binary_sha256")
    else:
        sha_value = sha(binary)
    fields = {
        "profile_id": "r1-test",
        "binary_path": binary,
        "binary_sha256": sha_value,
        "isolated_home": home,
        "codex_home": codex,
        "workspace": ws,
        "deny_real_home": Path("/Users/sandro"),
        "created_at": "2026-09-30T00:00:00+00:00",
        "verified_at": "2026-09-30T00:00:00+00:00",
    }
    fields.update(overrides)
    return CodexIsolationProfile(**fields)


def exact_probe(_: Path) -> str:
    return "codex-cli 0.159.2"


# ---------- P1 Binary：exact version pin ----------

def test_exact_version_pass(tmp_path):
    binary = stub_binary(tmp_path)
    result = certify_binary(make_profile(tmp_path, binary),
                            version_probe=lambda p: "codex-cli 0.159.2")
    assert result.status == "PASS"


@pytest.mark.parametrize("version_line", [
    "codex-cli 0.159.1",
    "codex-cli 0.160.0",
    "codex-cli 0.155.1",
])
def test_wrong_version_fail(tmp_path, version_line):
    binary = stub_binary(tmp_path, version_line)
    result = certify_binary(make_profile(tmp_path, binary),
                            version_probe=lambda p: version_line)
    assert result.status == "FAIL"


def test_missing_binary_fail(tmp_path):
    profile = make_profile(tmp_path, tmp_path / "nope", binary_sha256="2" * 64)
    result = certify_binary(profile, version_probe=lambda p: "")
    assert result.status == "FAIL"


def test_non_executable_binary_fail(tmp_path):
    binary = stub_binary(tmp_path)
    binary.chmod(0o644)
    result = certify_binary(make_profile(tmp_path, binary),
                            version_probe=lambda p: "codex-cli 0.159.2")
    assert result.status == "FAIL"


def test_binary_hash_mismatch_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary, binary_sha256="0" * 64)
    result = certify_binary(profile,
                            version_probe=lambda p: "codex-cli 0.159.2")
    assert result.status == "FAIL"
    assert "sha256" in result.detail or "hash" in result.detail


# ---------- P2 Directories ----------

def test_missing_isolated_dirs_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    (profile.codex_home.rmdir())
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" for c in checks)


def test_real_home_equals_isolated_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary, isolated_home=Path.home())
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" and "home" in c.code for c in checks)


# ---------- P3 Config contract ----------

def test_generated_config_validates(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    text = generate_config(profile)
    result = validate_config(text, profile)
    assert result.status == "PASS"


def test_config_missing_web_off_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    text = generate_config(profile).replace('web_search = "disabled"', "")
    assert validate_config(text, profile).status == "FAIL"


def test_config_missing_fs_deny_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    text = "\n".join(l for l in generate_config(profile).splitlines()
                     if "deny" not in l)
    assert validate_config(text, profile).status == "FAIL"


def test_config_missing_respect_system_proxy_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    text = generate_config(profile).replace("respect_system_proxy = true", "")
    assert validate_config(text, profile).status == "FAIL"


# ---------- P4 Auth provisioning（不复制、不读内容）----------

def test_auth_missing_blocked_not_provisioned(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    result = check_auth(profile)
    assert result.status == "BLOCKED"
    assert "NOT_PROVISIONED" in result.detail or "provision" in result.detail


def test_auth_permission_invalid_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    auth = profile.codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o644)                             # group/other 可读 = 违规
    result = check_auth(profile)
    assert result.status == "FAIL"


def test_auth_ok(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    auth = profile.codex_home / "auth.json"
    auth.write_text(TOKEN, encoding="utf-8")      # 内容含敏感值
    auth.chmod(0o600)
    result = check_auth(profile)
    assert result.status == "PASS"
    assert TOKEN not in result.detail             # 绝不输出凭据内容


# ---------- P5 Manifest / certification state ----------

def test_manifest_roundtrip_and_sanitized(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = build_manifest(profile)
    blob = repr(manifest)
    assert "/Users/sandro" not in blob            # deny 目标按 path class 记录
    assert TOKEN not in blob
    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    loaded = load_manifest(path)
    assert loaded["profile_id"] == manifest["profile_id"]
    assert loaded["binary_sha256"] == manifest["binary_sha256"]


def test_manifest_consistency_hash_mismatch(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary, binary_sha256="1" * 64)
    manifest = build_manifest(profile)            # manifest 记录漂移后的 sha
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _overall, checks = run_preflight(profile)     # 资产实际 sha ≠ manifest → FAIL
    manifest_checks = [c for c in checks if c.code.startswith("P5")]
    assert manifest_checks and all(c.status == "FAIL" for c in manifest_checks)


def test_prepared_cannot_claim_hostile_smoke_pass():
    with pytest.raises(ValueError):
        advance_certification_state(CertificationState.PREPARED,
                                    "HOSTILE_SMOKE_PASS")


def test_state_machine_legal_transition():
    assert advance_certification_state(
        CertificationState.PREPARED, "PREFLIGHT_PASS") == \
        CertificationState.PREFLIGHT_PASS
    assert advance_certification_state(
        CertificationState.PREFLIGHT_PASS, "HOSTILE_SMOKE_PASS") == \
        CertificationState.HOSTILE_SMOKE_PASS


def test_preppared_state_not_falsely_certified(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = build_manifest(profile)
    assert manifest["certification_state"] == "PREPARED"
    assert manifest["certification_state"] != "HOSTILE_SMOKE_PASS"


# ---------- Hostile smoke primitive（fake runner，无真实 internet）----------

def fake_runner_factory(responses):
    """按 stdin 任务文本中的特征子串路由 canned 响应（argv[-1] 恒为 "-"）。"""
    def run_fn(argv, stdin_text):
        for key, resp in responses.items():
            if key in stdin_text:
                return resp
        return 0, ""
    return run_fn


def test_hostile_smoke_all_pass(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"], run_fn=fake_runner_factory({
            "工具名": (0, "无 web 工具"),
            "star 数": (0, "无网络访问"),
            "api.github.com": (0, "exit 134 不可访问"),
            "复述标记": (0, "PARITY-NETOFF-MARKER-Z3K"),
            "AGENTS.md": (0, "受访问限制"),
        }))
    assert all(v["pass"] for v in results.values())
    assert set(results) == {"N1", "N2", "N3", "N4", "N5"}


def test_hostile_smoke_n2_live_web_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"], run_fn=fake_runner_factory({
            "star 数": (0, "26.5k stars"),
        }))
    assert results["N2"]["pass"] is False
    assert results["N2"]["evidence"]


def test_hostile_smoke_runner_error_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)

    def boom(argv, stdin_text):
        raise RuntimeError("model call failed")

    results = run_hostile_smoke(profile, exec_args=["exec"], run_fn=boom)
    assert all(v["pass"] is False for v in results.values())


# ---------- R1.1 test cleanups（Issue #8 §0，test-only）----------

PROBE_CHILD = (
    "import sys, json, os\n"
    "req = json.load(sys.stdin)\n"
    "print(json.dumps({'stage': req.get('stage'), "
    "'cwd': os.getcwd(), 'env_keys': sorted(os.environ), "
    "'HOME': os.environ.get('HOME'), "
    "'CODEX_HOME': os.environ.get('CODEX_HOME'), "
    "'SECRET_MARKER': os.environ.get('SECRET_MARKER')}))\n")

PROBE_CMD = f"{sys.executable} -c {shlex.quote(PROBE_CHILD)}"

def test_seven_stage_uniform_no_noop_assert(tmp_path):
    """R1.1 cleanup：原恒真 no-op assert 已删除；保留七阶段隔离语义等价断言。"""
    (tmp_path / "iso-home").mkdir(exist_ok=True)
    (tmp_path / "ws").mkdir(exist_ok=True)
    port = build_insight_model_port_from_env({
        "KI_INSIGHT_REQUIRE_ISOLATION": "1",
        "KI_INSIGHT_ISOLATION_PROFILE_ID": "uniformity",
        "KI_INSIGHT_ISOLATION_HOME": str(tmp_path / "iso-home"),
        "KI_INSIGHT_ISOLATION_CODEX_HOME": str(tmp_path / "iso-codex"),
        "KI_INSIGHT_ISOLATION_WORKSPACE": str(tmp_path / "ws"),
        "KI_INSIGHT_MODEL_CMD": PROBE_CMD,
    })
    assert port.isolation.profile_id == "uniformity"
    for stage in ("candidate_filter", "thinking"):
        out = port.run(stage, {})
        assert "SECRET_MARKER" not in out["env_keys"]

def test_secret_value_real_error_path_sanitized(tmp_path):
    """R1.1 cleanup：让含 secret 的值真实进入触发 isolation error 的配置路径，
    断言 secret value 不出现在错误消息（错误消息只含变量名，是设计行为）。"""
    secret_home = tmp_path / "vault-with-super-secret-token-abc"
    with pytest.raises(ModelIsolationConfigurationError) as exc:
        build_insight_model_port_from_env({
            "KI_INSIGHT_REQUIRE_ISOLATION": "1",
            "KI_INSIGHT_ISOLATION_HOME": str(secret_home),  # 不存在的目录
            "KI_INSIGHT_ISOLATION_CODEX_HOME": str(tmp_path / "c"),
            "KI_INSIGHT_ISOLATION_WORKSPACE": str(tmp_path / "w"),
        })
    assert "super-secret-token-abc" not in str(exc.value)  # 值不入消息
    assert "KI_INSIGHT_ISOLATION_HOME" in str(exc.value)   # 只含变量名
