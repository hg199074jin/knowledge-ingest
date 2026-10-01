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
    REQUIRED_BINARY_VERSION,
    CertificationState,
    CodexIsolationProfile,
    CodexProfileLoadError,
    CodexSurfaceAttestationError,
    advance_certification_state,
    build_certified_model_command,
    build_manifest,
    canonical_manifest_path,
    certified_model_argv,
    certified_probe_env,
    certify_binary,
    check_auth,
    generate_config,
    load_codex_profile_from_runtime,
    load_manifest,
    parse_feature_states,
    run_external_context_surface_attestation,
    run_hostile_behavioral_smoke,
    run_hostile_smoke,
    run_preflight,
    sha256_file,
    validate_config,
    write_manifest,
)
from knowledge_ingest.insight.model_port import (
    ModelIsolationConfigurationError,
    build_insight_model_port_from_env,
)

TOKEN = "Bearer super-secret-token-never-leak"


MACHO_MAGIC = b"\xcf\xfa\xed\xfe"          # MH_MAGIC_64 (arm64 thin)
_LAUNCHER_STUB = "#!/bin/sh\necho 'codex-cli 0.159.2'\n"


def stub_binary(tmp_path: Path, version_line: str = "codex-cli 0.159.2") -> Path:
    """native-style stub：Mach-O magic 开头 + 版本行（不真实执行）。"""
    binary = tmp_path / "codex-stub"
    binary.write_bytes(MACHO_MAGIC + b"\x00" * 16 + version_line.encode())
    binary.chmod(0o755)
    return binary


def launcher_stub(tmp_path: Path) -> Path:
    """shebang launcher 风格 stub（P7 identity 检查应拒绝）。"""
    binary = tmp_path / "codex-launcher"
    binary.write_text(_LAUNCHER_STUB, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_profile(tmp_path: Path, binary: Path, **overrides) -> CodexIsolationProfile:
    home = tmp_path / "iso-home"
    codex = tmp_path / "iso-codex-home"
    ws = tmp_path / "ws"
    for d in (home, codex, ws):
        d.mkdir(parents=True, exist_ok=True)
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


ALL_BEHAVIORAL_PASS = {
    "star 数": (0, "无网络访问"),
    "api.github.com": (0, "exit 134 不可访问"),
    "复述标记": (0, "PARITY-NETOFF-MARKER-Z3K"),
    "AGENTS.md": (0, "受访问限制"),
}


def clean_features_probe(profile):
    """N1 deterministic：exact 0.159.2 语义下 apps/standalone web 均 false。"""
    return 0, ("apps                       stable             false\n"
               "standalone_web_search      under development false\n"
               "web_search_request         deprecated         false\n")


def certified_profile(tmp_path, binary):
    """N1 需要 PASS 时的最小资产：config + auth + canonical manifest。"""
    profile = make_profile(tmp_path, binary)
    (profile.codex_home / "config.toml").write_text(
        generate_config(profile), encoding="utf-8")
    auth = profile.codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    write_manifest(profile.codex_home / MANIFEST_FILENAME,
                   build_manifest(profile))
    return profile


def test_hostile_smoke_all_pass(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"], features_probe=clean_features_probe,
        version_probe=lambda p: "codex-cli 0.159.2",
        env={}, child_env=certified_probe_env(profile),
        run_fn=fake_runner_factory(ALL_BEHAVIORAL_PASS))
    assert all(v["pass"] for v in results.values()), results
    assert set(results) == {"N1", "N2", "N3", "N4", "N5"}
    assert results["N1"]["attestation"]["verdict"] == "PASS"


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


# ---------- Issue #10 patch：P1–P7 ----------



class _FakeSubprocess:
    """捕获 subprocess.run kwargs 的 stub（P1 默认 runner 验证）。"""

    def __init__(self, stdout):
        self.calls = []
        self._stdout = stdout

    def run(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        import types
        return types.SimpleNamespace(returncode=0, stdout=self._stdout)


def test_default_hostile_runner_uses_isolated_env_and_workspace(
        tmp_path, monkeypatch):
    """P1：默认 runner 必须 env=isolated env 且 cwd=neutral workspace。"""
    import knowledge_ingest.insight.codex_isolation as ci
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    monkeypatch.setenv("SECRET_MARKER", "ambient-secret-value")
    fake = _FakeSubprocess(
        "无 web 工具\n无网络访问\nexit 134 不可访问\n"
        "PARITY-NETOFF-MARKER-Z3K\n受访问限制\n")
    monkeypatch.setattr(ci, "subprocess", fake)
    results = run_hostile_smoke(profile, exec_args=["exec"])
    assert all(v["pass"] for v in results.values() if v.get("attestation") is None)
    call = next(c for c in fake.calls if "exec" in c["argv"])
    env = call["env"]
    assert env["HOME"] == str(profile.isolated_home)
    assert env["CODEX_HOME"] == str(profile.codex_home)
    assert env["PATH"] == "/usr/bin:/bin"
    assert "SECRET_MARKER" not in env
    assert call["cwd"] == str(profile.workspace)


def test_default_hostile_argv_contains_three_disable_flags(tmp_path):
    """P5：默认 argv 必须自动带上三个已验证 disable flags（profile 单一来源）。"""
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    captured = {}

    def run_fn(argv, stdin_text):
        captured["argv"] = list(argv)
        return 0, "无网络访问"

    run_hostile_smoke(profile, exec_args=["exec"], run_fn=run_fn)
    argv = captured["argv"]
    for flag in ("--disable apps", "--disable web_search",
                 "--disable web_search_request"):
        parts = flag.split()
        assert all(p in argv for p in parts), (flag, argv)


def test_n1_empty_stdout_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(profile, exec_args=["exec"], run_fn=lambda a, s: (0, ""))
    assert results["N1"]["pass"] is False


def test_n1_arbitrary_text_without_marker_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (0, "hello world, nothing relevant here"))
    assert results["N1"]["pass"] is False


def test_n4_marker_must_be_exact(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (0, "PARITY-NETOFF-MARKER-WRONG"))
    assert results["N4"]["pass"] is False


def test_config_deny_entry_removed_but_description_kept_fail(tmp_path):
    """P2：只删真实 deny entry、保留含 deny 字样的 description → 必须 FAIL。"""
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    lines = generate_config(profile).splitlines()
    kept = [l for l in lines if not (l.strip().startswith('"/Users/sandro"')
                                     and "deny" in l)]
    assert kept != lines                        # 确认确实删掉了 entry 行
    result = validate_config("\n".join(kept), profile)
    assert result.status == "FAIL"


def test_manifest_missing_fail_overall_not_pass(tmp_path):
    """P3：资产完整但不创建 manifest → P5 FAIL，overall != PASS。"""
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    (profile.codex_home / "config.toml").write_text(
        generate_config(profile), encoding="utf-8")
    (profile.codex_home / "auth.json").write_text("{}", encoding="utf-8")
    auth = profile.codex_home / "auth.json"
    auth.chmod(0o600)
    overall, checks = run_preflight(profile)
    p5 = [c for c in checks if c.code.startswith("P5")]
    assert p5 and all(c.status == "FAIL" for c in p5)
    assert "missing" in p5[0].detail
    assert overall != "PASS"


def _write_valid_assets(profile):
    (profile.codex_home / "config.toml").write_text(
        generate_config(profile), encoding="utf-8")
    auth = profile.codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)


def _drift_manifest(tmp_path, profile, **overrides):
    binary = stub_binary(tmp_path)
    base = make_profile(tmp_path, binary)
    manifest = build_manifest(base)             # 用"别的 profile"的投影制造 drift
    manifest.update(overrides)
    return manifest


def test_manifest_profile_id_drift_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = _drift_manifest(tmp_path, profile,
                               profile_id="drifted-id")
    manifest["binary_sha256"] = sha256_file(binary)
    manifest["binary_path"] = str(binary)
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" and "profile_id" in c.detail
               for c in checks if c.code.startswith("P5"))


def test_manifest_binary_path_drift_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = _drift_manifest(tmp_path, profile,
                               binary_path="/somewhere/else/codex")
    manifest["binary_sha256"] = sha256_file(binary)
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" for c in checks if c.code.startswith("P5"))


def test_manifest_policy_drift_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = _drift_manifest(tmp_path, profile, network_policy="full")
    manifest["binary_sha256"] = sha256_file(binary)
    manifest["binary_path"] = str(binary)
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" and "network_policy" in c.detail
               for c in checks if c.code.startswith("P5"))


def test_manifest_required_cli_args_drift_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = _drift_manifest(tmp_path, profile,
                               required_cli_args=["--disable", "apps"])
    manifest["binary_sha256"] = sha256_file(binary)
    manifest["binary_path"] = str(binary)
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _overall, checks = run_preflight(profile)
    assert any(c.status == "FAIL" and "required_cli_args" in c.detail
               for c in checks if c.code.startswith("P5"))


def test_native_vs_launcher_identity(tmp_path):
    """P7：shebang launcher 必须被 identity 检查拒绝；native magic 通过。"""
    binary = stub_binary(tmp_path)
    ok = certify_binary(make_profile(tmp_path, binary),
                        version_probe=lambda p: "codex-cli 0.159.2")
    assert ok.status == "PASS"
    launcher = launcher_stub(tmp_path)
    bad = certify_binary(make_profile(tmp_path, launcher),
                         version_probe=lambda p: "codex-cli 0.159.2")
    assert bad.status == "FAIL"
    assert "native" in bad.detail or "Mach-O" in bad.detail


# ---------- Issue #12 F1：returncode fail-closed ----------

def test_f1_n1_rc1_with_correct_marker_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "无 web 工具"))
    assert results["N1"]["pass"] is False


def test_f1_n2_rc1_with_correct_phrase_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "无网络访问"))
    assert results["N2"]["pass"] is False


def test_f1_n3_rc1_with_correct_text_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "exit 134 不可访问"))
    assert results["N3"]["pass"] is False


def test_f1_n4_rc1_with_correct_marker_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "PARITY-NETOFF-MARKER-Z3K"))
    assert results["N4"]["pass"] is False


def test_f1_n5_rc1_with_correct_text_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "访问限制"))
    assert results["N5"]["pass"] is False


def test_f1_evidence_contains_rc(tmp_path):
    """R1.4-N1 repair 后 N1 不再经模型，rc evidence 由 behavioral 探针承担。"""
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "PARITY-NETOFF-MARKER-Z3K"))
    assert "rc=1" in results["N4"]["evidence"]


def test_f1_rc0_still_passes_with_positive_evidence(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (0, "PARITY-NETOFF-MARKER-Z3K"))
    assert results["N4"]["pass"] is True


# ---------- Issue #12 F2：manifest/binary 损坏 → fail-closed ----------

def test_f2_binary_deleted_preflight_returns_p5_fail_not_exception(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    manifest = build_manifest(profile)
    manifest["certification_state"] = "PREFLIGHT_PASS"
    write_manifest(profile.codex_home / MANIFEST_FILENAME, manifest)
    _write_valid_assets(profile)                   # auth provisioned：只测 binary 消失
    binary.unlink()                                # 资产在认证后消失
    overall, checks = run_preflight(profile)       # 不得抛异常
    p5 = [c for c in checks if c.code.startswith("P5")]
    assert p5 and all(c.status == "FAIL" for c in p5)
    assert any("binary asset unavailable" in c.detail for c in p5)
    assert overall == "FAIL"
    blocked = [c for c in checks if c.status == "BLOCKED"]
    assert not blocked, blocked                    # 本场景不允许任何 BLOCKED


def test_f2_malformed_manifest_json_fail_closed(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    _write_valid_assets(profile)
    secret_blob = '{"x": "top-secret-manifest-content-xyz"'
    (profile.codex_home / MANIFEST_FILENAME).write_bytes(
        secret_blob.encode())                      # 截断/损坏 JSON
    _overall, checks = run_preflight(profile)       # 不得抛异常
    p5 = [c for c in checks if c.code.startswith("P5")]
    assert p5 and all(c.status == "FAIL" for c in p5)
    assert any("invalid/unreadable manifest" in c.detail for c in p5)
    joined = " ".join(c.detail for c in p5)
    assert "top-secret-manifest-content-xyz" not in joined  # 不回显原文


def _write_valid_assets(profile):
    (profile.codex_home / "config.toml").write_text(
        generate_config(profile), encoding="utf-8")
    auth = profile.codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)


# ---------- R1.3：runtime profile loader + certified command helper ----------

REAL_HOME = Path("/Users/sandro")


def _manifest_for(tmp_path, binary, **overrides):
    profile = make_profile(tmp_path, binary, **overrides)
    manifest = build_manifest(profile)
    return profile, manifest


def _load(tmp_path, binary, manifest, **kwargs):
    path = tmp_path / MANIFEST_FILENAME
    write_manifest(path, manifest)
    return load_codex_profile_from_runtime(
        manifest_path=path, real_home=kwargs.pop("real_home", REAL_HOME))


def test_loader_rebuilds_profile_from_sanitized_manifest(tmp_path):
    binary = stub_binary(tmp_path)
    profile, manifest = _manifest_for(tmp_path, binary)
    loaded = _load(tmp_path, binary, manifest)
    assert loaded.profile_id == profile.profile_id
    assert loaded.binary_path == profile.binary_path
    assert loaded.binary_sha256 == profile.binary_sha256
    assert loaded.isolated_home == profile.isolated_home
    assert loaded.codex_home == profile.codex_home
    assert loaded.workspace == profile.workspace
    assert loaded.required_binary_version == REQUIRED_BINARY_VERSION
    assert loaded.certification_state is CertificationState.PREPARED
    assert loaded.deny_real_home == REAL_HOME      # 显式 runtime fact 生效


def test_loader_preserves_required_cli_args(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    loaded = _load(tmp_path, binary, manifest)
    assert loaded.required_cli_args == (
        "--disable", "apps", "--disable", "web_search",
        "--disable", "web_search_request")


def test_loader_manifest_missing_fails(tmp_path):
    with pytest.raises(CodexProfileLoadError) as exc:
        load_codex_profile_from_runtime(
            manifest_path=tmp_path / "absent.json", real_home=REAL_HOME)
    assert "missing" in str(exc.value)


def test_loader_malformed_manifest_fails_without_echo(tmp_path):
    path = tmp_path / MANIFEST_FILENAME
    path.write_text('{"binary_sha256": "top-secret-loader-7c1e"',
                    encoding="utf-8")
    with pytest.raises(CodexProfileLoadError) as exc:
        load_codex_profile_from_runtime(manifest_path=path,
                                        real_home=REAL_HOME)
    assert "top-secret-loader-7c1e" not in str(exc.value)


def test_loader_unsupported_schema_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["manifest_schema_version"] = 99
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "schema" in str(exc.value)


def test_loader_provider_drift_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["provider"] = "claude-code"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "provider" in str(exc.value)


def test_loader_illegal_certification_state_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["certification_state"] = "TOTALLY_CERTIFIED"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "certification_state" in str(exc.value)


@pytest.mark.parametrize("field", [
    "binary_path", "isolated_home", "codex_home", "workspace"])
def test_loader_relative_path_fails(tmp_path, field):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest[field] = "relative/path"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert field in str(exc.value)


def test_loader_cli_args_drift_fails(tmp_path):
    """manifest 被弱化（丢掉 web_search_request disable）→ 拒绝重建。"""
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["required_cli_args"] = ["--disable", "apps"]
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "required_cli_args" in str(exc.value)


def test_loader_cli_args_wrong_type_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["required_cli_args"] = "--disable apps"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "required_cli_args" in str(exc.value)


def test_loader_version_pin_drift_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["required_binary_version"] = "0.160.0"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "required_binary_version" in str(exc.value)


def test_loader_policy_drift_fails(tmp_path):
    binary = stub_binary(tmp_path)
    for key, value in (("network_policy", "on"), ("filesystem_policy", "off"),
                       ("web_apps_policy", "enabled"),
                       ("binary_kind", "launcher")):
        _profile, manifest = _manifest_for(tmp_path, binary)
        manifest[key] = value
        with pytest.raises(CodexProfileLoadError) as exc:
            _load(tmp_path, binary, manifest)
        assert key in str(exc.value)


def test_loader_bad_sha256_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["binary_sha256"] = "not-a-digest"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "binary_sha256" in str(exc.value)


def test_loader_missing_required_field_fails(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    del manifest["profile_id"]
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "profile_id" in str(exc.value)


def test_loader_requires_explicit_real_home(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    path = tmp_path / MANIFEST_FILENAME
    write_manifest(path, manifest)
    with pytest.raises(CodexProfileLoadError) as exc:
        load_codex_profile_from_runtime(manifest_path=path, real_home=None)
    assert "real home" in str(exc.value)
    with pytest.raises(CodexProfileLoadError):
        load_codex_profile_from_runtime(manifest_path=path,
                                        real_home=Path("relative/home"))


def test_loader_refuses_isolated_home_as_deny_target(tmp_path):
    """sanitized manifest 缺 real-home 路径时，绝不能用 isolated HOME 顶替。"""
    binary = stub_binary(tmp_path)
    profile, manifest = _manifest_for(tmp_path, binary)
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest, real_home=profile.isolated_home)
    assert "isolated home" in str(exc.value)


def test_loader_hostile_state_requires_evidence(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(
        tmp_path, binary, certification_state=CertificationState.HOSTILE_SMOKE_PASS)
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "hostile_smoke_ref" in str(exc.value)


def test_loader_requires_real_user_home_deny_target(tmp_path):
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["deny_targets"] = []
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert "deny_targets" in str(exc.value)


def test_loader_ignores_auth_content(tmp_path):
    """loader 不读 auth.json：有无凭据都不得影响重建，且不得回显内容。"""
    binary = stub_binary(tmp_path)
    profile, manifest = _manifest_for(tmp_path, binary)
    loaded = _load(tmp_path, binary, manifest)
    auth = profile.codex_home / "auth.json"
    auth.write_text(TOKEN, encoding="utf-8")
    auth.chmod(0o600)
    assert load_codex_profile_from_runtime(
        manifest_path=tmp_path / MANIFEST_FILENAME,
        real_home=REAL_HOME).profile_id == loaded.profile_id
    assert TOKEN not in repr(loaded)


def test_loaded_profile_drives_preflight_pass(tmp_path):
    """loader 产物直接可喂 R1.2 preflight（单一来源，零重写）。"""
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    loaded = _load(tmp_path, binary, manifest)
    (loaded.codex_home / "config.toml").write_text(
        generate_config(loaded), encoding="utf-8")
    auth = loaded.codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    write_manifest(loaded.codex_home / MANIFEST_FILENAME, build_manifest(loaded))
    overall, checks = run_preflight(loaded,
                                    version_probe=lambda p: "codex-cli 0.159.2")
    assert overall == "PASS", [c for c in checks if c.status != "PASS"]


def test_loader_tolerates_unknown_key_but_preflight_flags_it(tmp_path):
    """未知键：loader 前向兼容不放行激活，preflight 仍以 drift fail-closed。

    键名同样来自 manifest（可能被篡改成敏感串）→ detail 只报数量不回显
    （Issue #17）。
    """
    marker = "unknown-key-secret-marker-9f2a"
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest[marker] = True
    loaded = _load(tmp_path, binary, manifest)
    _write_valid_assets(loaded)
    write_manifest(loaded.codex_home / MANIFEST_FILENAME,
                   build_manifest(loaded) | {marker: True})
    _overall, checks = run_preflight(
        loaded, version_probe=lambda p: "codex-cli 0.159.2")
    assert any(c.status == "FAIL" and "unknown manifest key" in c.detail
               for c in checks)
    assert all(marker not in c.detail for c in checks)


def test_canonical_manifest_path_matches_preflight_read_location(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    assert canonical_manifest_path(profile) == \
        profile.codex_home / MANIFEST_FILENAME


# ---------- certified command helper ----------

def test_certified_command_exact_argv(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    argv = shlex.split(build_certified_model_command(profile))
    assert argv[0] == str(binary)
    assert argv[1] == "exec"
    assert argv[-1] == "-"
    assert "-c" in argv and "orchestrator.skills.enabled=false" in argv
    assert "--skip-git-repo-check" in argv
    for flag in ("--disable apps", "--disable web_search",
                 "--disable web_search_request"):
        assert all(part in argv for part in flag.split())
    assert argv == list(certified_model_argv(profile))


def test_certified_command_is_single_source_for_hostile_argv(tmp_path):
    """hostile runner 与 production command 共用 certified argv。"""
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    captured = {}

    def run_fn(argv, stdin_text):
        captured["argv"] = list(argv)
        return 0, "PARITY-NETOFF-MARKER-Z3K"

    run_hostile_smoke(profile, exec_args=["exec"], run_fn=run_fn)
    assert captured["argv"] == list(certified_model_argv(profile))
    assert captured["argv"][-1] == "-"


def test_certified_command_shell_safe_for_spaced_path(tmp_path):
    spaced = tmp_path / "with space"
    spaced.mkdir()
    binary = stub_binary(spaced)
    profile = make_profile(tmp_path, binary)
    command = build_certified_model_command(profile)
    assert " " in command                       # 需要引号编码
    assert shlex.split(command)[0] == str(binary)


def test_certified_command_has_no_stage_specific_args(tmp_path):
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    argv = shlex.split(build_certified_model_command(profile))
    assert "-m" not in argv and "--model" not in argv


# ---------- R1.3 patch（Issue #17）：manifest error sanitization 矩阵 ----------

#: 每个受校验字段注入唯一 secret marker：错误必须 fail-closed 且零回显。
SECRET_MANIFEST_FIELDS = (
    "manifest_schema_version",
    "provider",
    "certification_state",
    "required_binary_version",
    "binary_kind",
    "network_policy",
    "filesystem_policy",
    "web_apps_policy",
)


def _secret_marker(field: str) -> str:
    return f"Bearer sk-secret-{field.replace('_', '-')}-9f2a"


@pytest.mark.parametrize("field", SECRET_MANIFEST_FIELDS)
def test_loader_error_never_echoes_manifest_raw_value(tmp_path, field):
    marker = _secret_marker(field)
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest[field] = marker
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    detail = str(exc.value)
    assert marker not in detail, detail          # 零回显
    assert field.split("_")[0] in detail or field in detail  # 仍可定位字段
    assert isinstance(detail, str) and len(detail) < 120


@pytest.mark.parametrize("field", SECRET_MANIFEST_FIELDS)
def test_loader_error_still_names_expected_semantics(tmp_path, field):
    """不降低诊断能力：字段名 + 类别 + 固定 expected 值仍在。"""
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest[field] = "wrong-value"
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    detail = str(exc.value)
    if field == "manifest_schema_version":
        assert "expected 1" in detail
    elif field == "required_binary_version":
        assert "expected certified version 0.159.2" in detail
    else:
        assert detail in (
            "provider mismatch", "illegal certification_state",
            "binary_kind mismatch", "network_policy mismatch",
            "filesystem_policy mismatch", "web_apps_policy mismatch")


def test_loader_required_cli_args_drift_never_echoes(tmp_path):
    marker = _secret_marker("required_cli_args")
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest["required_cli_args"] = [marker]
    with pytest.raises(CodexProfileLoadError) as exc:
        _load(tmp_path, binary, manifest)
    assert str(exc.value) == "required_cli_args mismatch"


def test_loader_unknown_manifest_key_name_never_echoed(tmp_path):
    """键名也来自 manifest：P5 只报数量，不回显键名。"""
    marker = _secret_marker("key-name")
    binary = stub_binary(tmp_path)
    _profile, manifest = _manifest_for(tmp_path, binary)
    manifest[marker] = True
    loaded = _load(tmp_path, binary, manifest)
    _write_valid_assets(loaded)
    write_manifest(loaded.codex_home / MANIFEST_FILENAME,
                   build_manifest(loaded) | {marker: True})
    _overall, checks = run_preflight(
        loaded, version_probe=lambda p: "codex-cli 0.159.2")
    assert all(marker not in c.detail for c in checks)


# ---------- R1.4-N1 repair（Issue #21）：deterministic N1 A–F ----------

#: 真实 0.159.2 语义下的 inventory 抽样：apps/standalone web 必须 false；
#: `apply_patch*` / `in_app_*` 等非 gated 能力存在**不影响** N1（名字含 `app`
#: 不再是安全判据；它们不构成 external-context channel）。
CLEAN_FEATURES = (
    "apps                                     stable             false\n"
    "standalone_web_search                    under development false\n"
    "web_search_request                       deprecated         false\n"
    "web_search_cached                        deprecated         false\n"
    "apply_patch_freeform                     removed            false\n"
    "in_app_browser                           stable             true\n"
    "skill_search                             stable             true\n"
    "guardianv2.thread_context                removed            false\n"
)


def _probe_ok(_profile):
    return 0, CLEAN_FEATURES


def stub_version_probe(path):
    """按 stub 文件内容回报版本行（与真实 native binary 行为一致）。"""
    return path.read_bytes()[20:].decode(errors="ignore").strip()


def attest(profile, **kwargs):
    kwargs.setdefault("features_probe", _probe_ok)
    kwargs.setdefault("version_probe", stub_version_probe)
    kwargs.setdefault("env", {})
    kwargs.setdefault("child_env", certified_probe_env(profile))
    checks, attestation = run_external_context_surface_attestation(
        profile, **kwargs)
    return {c.code: c for c in checks}, attestation


# --- parser 严格性 ---

def test_parse_feature_states_reads_real_0_1592_shape():
    states = parse_feature_states(CLEAN_FEATURES)
    assert states["apps"] is False
    assert states["in_app_browser"] is True          # 名字含 app，但不是 apps feature
    assert states["guardianv2.thread_context"] is False   # 点号名可解析


@pytest.mark.parametrize("stdout", [
    "",
    "apps stable maybe\n",
    "apps\n",
    "apps stable false\napps stable true\n",
])
def test_parse_feature_states_fail_closed(stdout):
    with pytest.raises(CodexSurfaceAttestationError):
        parse_feature_states(stdout)


# --- N1-A exact binary identity ---

def test_n1_wrong_version_fail(tmp_path):
    binary = stub_binary(tmp_path, "codex-cli 0.160.0")
    profile = certified_profile(tmp_path, binary)
    checks, attestation = attest(profile)
    assert checks["N1-A-binary"].status == "FAIL"
    assert attestation["verdict"] == "FAIL"


def test_n1_binary_hash_mismatch_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    profile = CodexIsolationProfile(**{**profile.__dict__,
                                      "binary_sha256": "0" * 64})
    checks, _ = attest(profile)
    assert checks["N1-A-binary"].status == "FAIL"


# --- N1-B effective config contract ---

def test_n1_web_search_not_disabled_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    path = profile.codex_home / "config.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        'web_search = "disabled"', 'web_search = "live"'), encoding="utf-8")
    checks, _ = attest(profile)
    assert checks["N1-B-config"].status == "FAIL"
    assert "web-search-not-disabled" in checks["N1-B-config"].detail


def test_n1_custom_mcp_servers_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    path = profile.codex_home / "config.toml"
    path.write_text(path.read_text(encoding="utf-8")
                    + '\n[mcp_servers.victim]\ncommand = "curl"\n',
                    encoding="utf-8")
    checks, _ = attest(profile)
    assert checks["N1-B-config"].status == "FAIL"
    assert "mcp-servers-configured" in checks["N1-B-config"].detail


def test_n1_unknown_config_key_reported_by_count_only(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    path = profile.codex_home / "config.toml"
    path.write_text(path.read_text(encoding="utf-8")
                    + '\n[tools.some_vendor]\nkey = "secret-9f2a"\n',
                    encoding="utf-8")
    checks, _ = attest(profile)
    status, detail = checks["N1-B-config"].status, checks["N1-B-config"].detail
    assert status == "FAIL"
    assert "secret-9f2a" not in detail            # 不回显键名/取值
    assert "unknown-top-level:1" in detail


def test_n1_config_missing_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    (profile.codex_home / "config.toml").unlink()
    checks, _ = attest(profile)
    assert checks["N1-B-config"].status == "FAIL"


# --- N1-C apps effective state ---

def test_n1_apps_effectively_enabled_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, features_probe=lambda p: (
        0, "apps                       stable             true\n"))
    assert checks["N1-C-apps"].status == "FAIL"
    assert "apps-effective-enabled" in checks["N1-C-apps"].detail


def test_n1_standalone_web_search_enabled_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, features_probe=lambda p: (
        0, "apps stable false\nstandalone_web_search under development true\n"))
    assert checks["N1-C-apps"].status == "FAIL"


def test_n1_features_list_unparsable_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, features_probe=lambda p: (0, "garbage output"))
    assert checks["N1-C-apps"].status == "FAIL"
    assert "undeterminable" in checks["N1-C-apps"].detail


def test_n1_features_probe_nonzero_rc_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, features_probe=lambda p: (1, CLEAN_FEATURES))
    assert checks["N1-C-apps"].status == "FAIL"


def test_n1_apps_absent_from_inventory_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, features_probe=lambda p: (
        0, "skill_search stable true\n"))
    assert checks["N1-C-apps"].status == "FAIL"
    assert "apps-absent-from-inventory" in checks["N1-C-apps"].detail


# --- N1-D certified CLI contract ---

def test_n1_disable_apps_missing_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    profile = CodexIsolationProfile(**{
        **profile.__dict__,
        "required_cli_args": ("--disable", "web_search",
                              "--disable", "web_search_request")})
    checks, _ = attest(profile)
    assert checks["N1-D-cli"].status == "FAIL"
    assert "apps" in checks["N1-D-cli"].detail


# --- N1-E isolated substrate ---

@pytest.mark.parametrize("target", ["codex_home", "isolated_home", "workspace"])
def test_n1_agents_md_present_fail(tmp_path, target):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    (getattr(profile, target) / "AGENTS.md").write_text("x", encoding="utf-8")
    checks, _ = attest(profile)
    assert checks["N1-E-substrate"].status == "FAIL"
    assert "agents-md" in checks["N1-E-substrate"].detail


def test_n1_user_skills_in_isolated_home_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    skills = profile.isolated_home / ".agents" / "skills"
    skills.mkdir(parents=True)
    (skills / "mine").mkdir()
    checks, _ = attest(profile)
    assert checks["N1-E-substrate"].status == "FAIL"


def test_n1_non_system_codex_skills_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    (profile.codex_home / "skills" / ".system").mkdir(parents=True)
    (profile.codex_home / "skills" / "user-injected").mkdir()
    checks, _ = attest(profile)
    assert "non-system-skills:1" in checks["N1-E-substrate"].detail


def test_n1_installed_plugin_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    plugins = profile.codex_home / "plugins"
    (plugins / "cache").mkdir(parents=True)
    (plugins / ".remote-plugin-install-staging").mkdir()
    (plugins / "github").mkdir()                 # 已安装插件 = 未受控上下文
    checks, _ = attest(profile)
    assert "installed-plugins:1" in checks["N1-E-substrate"].detail


def test_n1_plugin_cache_only_is_ok(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    plugins = profile.codex_home / "plugins"
    (plugins / "cache" / "openai-curated-remote").mkdir(parents=True)
    checks, _ = attest(profile)
    assert checks["N1-E-substrate"].status == "PASS"


def test_n1_stage_override_present_fail(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, _ = attest(profile, env={"KI_INSIGHT_THINK_CMD": "/bin/echo x"})
    assert "stage-overrides:1" in checks["N1-E-substrate"].detail


@pytest.mark.parametrize("key", ["KI_INSIGHT_MODEL_CMD", "OPENAI_API_KEY",
                                 "GITHUB_TOKEN", "SSH_AUTH_SOCK"])
def test_n1_ambient_secret_in_child_env_fail(tmp_path, key):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    child = dict(certified_probe_env(profile))
    child[key] = "secret-marker-4b8e"
    checks, _ = attest(profile, child_env=child)
    assert checks["N1-E-substrate"].status == "FAIL"
    assert "ambient-child-env:1" in checks["N1-E-substrate"].detail
    assert "secret-marker-4b8e" not in checks["N1-E-substrate"].detail


# --- N1 happy path + 语义边界 ---

def test_n1_clean_exact_profile_pass(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    checks, attestation = attest(profile)
    assert {c.status for c in checks.values()} == {"PASS"}, checks
    assert attestation["verdict"] == "PASS"
    assert attestation["provider_attestation"] == "codex-0.159.2"
    assert attestation["upstream_tag"] == "rust-v0.159.2"
    assert attestation["web_tool_when_disabled"] == "absent"
    assert attestation["hosted_apps_when_apps_disabled"] == "absent"
    assert attestation["features_inventoried"] == 8


def test_n1_apply_patch_presence_does_not_affect_verdict(tmp_path):
    """回归 Issue #21 §2：名字含 `app` 的本地工具不得被当成 apps channel。"""
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    inventory = ("apps stable false\n"
                 "apply_patch_freeform under development true\n"
                 "apply_patch_streaming_events under development true\n")
    checks, attestation = attest(profile, features_probe=lambda p: (0, inventory))
    assert checks["N1-C-apps"].status == "PASS"
    assert attestation["verdict"] == "PASS"
    assert "apply_patch_freeform" in attestation["effective_true_features"]


def test_attestation_records_inventory_as_evidence(tmp_path):
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    _checks, attestation = attest(profile)
    assert "skill_search" in attestation["effective_true_features"]
    assert "apps" not in attestation["effective_true_features"]
    assert attestation["binary_sha256"] == profile.binary_sha256


def test_behavioral_smoke_has_no_n1_of_its_own(tmp_path):
    """N1 只有一个来源：behavioral 层不得再产生 N1 结论。"""
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    results = run_hostile_behavioral_smoke(
        profile, exec_args=["exec"], run_fn=fake_runner_factory(ALL_BEHAVIORAL_PASS))
    assert set(results) == {"N2", "N3", "N4", "N5"}


def test_run_hostile_smoke_n1_never_uses_model_run_fn(tmp_path):
    """N1 不经模型：即使 run_fn 返回完美文本，apps 启用时 N1 仍 FAIL。"""
    binary = stub_binary(tmp_path)
    profile = certified_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"], features_probe=lambda p: (
            0, "apps stable true\n"),
        version_probe=lambda p: "codex-cli 0.159.2", env={},
        child_env=certified_probe_env(profile),
        run_fn=fake_runner_factory(ALL_BEHAVIORAL_PASS))
    assert results["N1"]["pass"] is False
    assert all(results[n]["pass"] for n in ("N2", "N3", "N4", "N5"))
