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
    assert all(v["pass"] for v in results.values())
    call = fake.calls[0]
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
    binary = stub_binary(tmp_path)
    profile = make_profile(tmp_path, binary)
    results = run_hostile_smoke(
        profile, exec_args=["exec"],
        run_fn=lambda a, s: (1, "无 web 工具"))
    assert "rc=1" in results["N1"]["evidence"]


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
