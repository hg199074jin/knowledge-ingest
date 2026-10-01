"""R1.3: Insight Doctor——generic isolation + Codex preflight 聚合（D1–D6）。

覆盖（Issue #14 §11 doctor）：
1. strict env 缺失 → isolation FAIL；
2. manifest missing/malformed → provider FAIL；
3. preflight BLOCKED auth → doctor 可见 BLOCKED；
4. BLOCKED 与 FAIL 并存 → FAIL 子项不被隐藏；
5. model command 漂移 → command_consistency FAIL；
6. PREFLIGHT_PASS 但未 HOSTILE → activation_ready != PASS；
7. HOSTILE_SMOKE_PASS + evidence + preflight PASS → activation_ready PASS；
8. doctor detail 不泄漏 secret；
外加 manifest location drift 与只读性（体检不得写任何资产）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_ingest.config import AppConfig
from knowledge_ingest.insight.codex_isolation import (
    CertificationState,
    build_certified_model_command,
)
from knowledge_ingest.insight.doctor import (
    collect_isolation_readiness,
    run_insight_doctor,
)
from knowledge_ingest.insight.store import InsightStore

from .conftest import AUTH_SECRET


def _by_name(readiness) -> dict[str, tuple[str, str]]:
    return {c.name: (c.status, c.detail) for c in readiness.checks}


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "m"),
        "docchunk_project": str(tmp_path / "d"),
        "media_output_root": str(tmp_path / "mo"),
        "docchunk_corpus_root": str(tmp_path / "dc"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "b", "quark": "q", "cangjie": "c",
                   "personal_distiller": "p", "k2c": "k2c"},
    })


# ---------- D1：strict env 缺失 → FAIL ----------

def test_strict_isolation_not_required_is_fail():
    readiness = collect_isolation_readiness({}, real_home=Path("/tmp"))
    checks = _by_name(readiness)
    assert checks["D1-require-isolation"][0] == "FAIL"
    assert checks["D1-base-command-absolute"][0] == "FAIL"
    assert readiness.status != "PASS"


def test_stage_override_is_fail_and_names_only_variable(certified):
    assets = certified()
    env = assets.env_with(KI_INSIGHT_THINK_CMD="/bin/echo secret-value-xyz")
    readiness = collect_isolation_readiness(
        env, version_probe=assets.version_probe)
    checks = _by_name(readiness)
    assert checks["D1-stage-override"][0] == "FAIL"
    assert "KI_INSIGHT_THINK_CMD" in checks["D1-stage-override"][1]
    assert "secret-value-xyz" not in checks["D1-stage-override"][1]


# ---------- D2：manifest missing / malformed → provider FAIL ----------

def test_manifest_path_not_configured_is_fail(certified):
    assets = certified()
    env = assets.env_with(KI_INSIGHT_CODEX_MANIFEST="")
    readiness = collect_isolation_readiness(
        env, version_probe=assets.version_probe)
    assert _by_name(readiness)["D2-manifest-path"][0] == "FAIL"
    assert readiness.profile is None


def test_manifest_missing_is_fail(certified):
    assets = certified(manifest=False)
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    assert _by_name(readiness)["D2-manifest-load"][0] == "FAIL"


def test_malformed_manifest_is_fail_and_detail_is_sanitized(certified):
    assets = certified(manifest=False)
    assets.manifest_path.write_text(
        '{"binary_sha256": "leaky-manifest-secret-9f3a"', encoding="utf-8")
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    status, detail = _by_name(readiness)["D2-manifest-load"]
    assert status == "FAIL"
    assert "leaky-manifest-secret-9f3a" not in detail
    assert readiness.profile is None


# ---------- Issue #17：doctor/readiness 侧零回显矩阵 ----------

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


@pytest.mark.parametrize("field", SECRET_MANIFEST_FIELDS)
def test_doctor_d2_never_echoes_manifest_raw_value(certified, field):
    marker = f"Bearer sk-secret-{field.replace('_', '-')}-4b8e"
    assets = certified(tamper=lambda doc: {**doc, field: marker})
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    status, detail = _by_name(readiness)["D2-manifest-load"]
    assert status == "FAIL"
    assert marker not in detail, detail
    assert marker not in readiness.detail
    joined = " ".join(f"{n} {s} {d}" for n, s, d in readiness.rows())
    assert marker not in joined
    assert readiness.profile is None
    assert readiness.status != "PASS"           # fail-closed，不放行激活


def test_doctor_d2_never_echoes_manifest_key_name(certified):
    marker = "Bearer sk-secret-unknown-key-1c3d"
    assets = certified(tamper=lambda doc: {**doc, marker: True})
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    joined = " ".join(f"{n} {s} {d}" for n, s, d in readiness.rows())
    assert marker not in joined
    assert readiness.status != "PASS"


def test_manifest_outside_codex_home_is_fail(certified, tmp_path):
    """manifest 路径必须与 preflight 读取位置一致，否则单一来源断裂。"""
    assets = certified()
    stray = tmp_path / "stray-manifest.json"
    stray.write_bytes(assets.manifest_path.read_bytes())
    readiness = collect_isolation_readiness(
        assets.env_with(KI_INSIGHT_CODEX_MANIFEST=str(stray)),
        version_probe=assets.version_probe)
    assert _by_name(readiness)["D2-manifest-location"][0] == "FAIL"
    assert readiness.status != "PASS"


# ---------- D3：preflight 子项可见性 ----------

def test_preflight_blocked_auth_is_visible(certified):
    assets = certified(auth=False)
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    checks = _by_name(readiness)
    assert checks["D3-overall"][0] == "BLOCKED"
    assert checks["D3-P4-auth"][0] == "BLOCKED"
    assert readiness.status == "BLOCKED"      # 仅 auth 阻塞 → 非 FAIL 条件


def test_blocked_does_not_hide_concurrent_fail(certified):
    assets = certified(auth=False, config=False)
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    checks = _by_name(readiness)
    assert checks["D3-P4-auth"][0] == "BLOCKED"
    assert checks["D3-P3-config"][0] == "FAIL"
    assert readiness.status == "FAIL"          # FAIL 优先于 BLOCKED


def test_preflight_pass_surfaces_overall_only(certified):
    assets = certified()
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    checks = _by_name(readiness)
    assert checks["D3-overall"][0] == "PASS"
    assert not any(n.startswith("D3-P") for n in checks)


# ---------- D4：command consistency ----------

def test_command_drift_missing_disable_flag_is_fail(certified):
    """manifest 安全但 production command 少 disable flags → 必须 FAIL。"""
    assets = certified()
    drifted = build_certified_model_command(assets.profile).replace(
        "--disable web_search_request", "")
    readiness = collect_isolation_readiness(
        assets.env_with(KI_INSIGHT_MODEL_CMD=drifted),
        version_probe=assets.version_probe)
    status, detail = _by_name(readiness)["D4-command-consistency"]
    assert status == "FAIL"
    assert "argv drift" in detail
    assert readiness.status == "FAIL"


def test_command_drift_relative_binary_is_fail(certified):
    assets = certified()
    readiness = collect_isolation_readiness(
        assets.env_with(KI_INSIGHT_MODEL_CMD="codex exec -"),
        version_probe=assets.version_probe)
    assert _by_name(readiness)["D4-command-consistency"][0] == "FAIL"


def test_command_missing_is_fail(certified):
    assets = certified()
    env = assets.env_with(KI_INSIGHT_MODEL_CMD="")
    readiness = collect_isolation_readiness(env,
                                           version_probe=assets.version_probe)
    assert _by_name(readiness)["D4-command-consistency"][0] == "FAIL"


def test_command_unparsable_is_fail_not_exception(certified):
    assets = certified()
    env = assets.env_with(KI_INSIGHT_MODEL_CMD='"/usr/bin/codex exec')
    readiness = collect_isolation_readiness(env,
                                           version_probe=assets.version_probe)
    assert _by_name(readiness)["D4-command-consistency"][0] == "FAIL"


# ---------- D5/D6：certification state 与 activation readiness ----------

def test_preflight_pass_but_not_hostile_is_not_ready(certified):
    assets = certified(state=CertificationState.PREFLIGHT_PASS, evidence="")
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    checks = _by_name(readiness)
    assert checks["D5-certification-state"] == ("FAIL", "PREFLIGHT_PASS")
    assert checks["D5-hostile-smoke-evidence"][0] == "FAIL"
    assert readiness.status == "FAIL"
    assert "certification-hostile-smoke-pass=FAIL" in readiness.detail


def test_prepared_state_is_not_ready(certified):
    assets = certified(state=CertificationState.PREPARED, evidence="")
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    assert readiness.status != "PASS"


def test_hostile_state_without_evidence_is_not_ready(certified):
    assets = certified(state=CertificationState.HOSTILE_SMOKE_PASS,
                       evidence="")
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    assert readiness.status == "FAIL"


def test_all_conditions_pass_is_ready(certified):
    assets = certified()
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    assert readiness.status == "PASS", readiness.detail
    assert readiness.certification_state == "HOSTILE_SMOKE_PASS"
    assert readiness.profile is not None


def test_preflight_fail_keeps_activation_not_ready(certified):
    assets = certified()
    assets.profile.binary_path.write_bytes(b"not-a-macho-binary")
    readiness = collect_isolation_readiness(
        assets.env, version_probe=assets.version_probe)
    assert readiness.status == "FAIL"
    assert _by_name(readiness)["D3-P1-binary"][0] == "FAIL"


# ---------- 只读性 / secret hygiene ----------

def test_doctor_is_read_only(certified, tmp_path):
    assets = certified()
    config = make_config(tmp_path)
    store = InsightStore(tmp_path / "insight" / "state.db")
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in assets.profile.codex_home.rglob("*")
              if path.is_file()}
    rows = run_insight_doctor(config, store, env=assets.env,
                              real_home=assets.real_home,
                              version_probe=assets.version_probe)
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in assets.profile.codex_home.rglob("*")
             if path.is_file()}
    assert before == after
    names = [name for name, _status, _detail in rows]
    assert "insight_root" in names
    assert "activation_ready" in names


def test_doctor_details_never_leak_secret(certified, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_MARKER", "ambient-secret-9c1d")
    assets = certified()
    manifest_doc = json.loads(assets.manifest_path.read_text(encoding="utf-8"))
    manifest_doc["api_key"] = "manifest-secret-4b8e"
    assets.manifest_path.write_text(
        json.dumps(manifest_doc), encoding="utf-8")
    config = make_config(tmp_path)
    store = InsightStore(tmp_path / "insight" / "state.db")
    rows = run_insight_doctor(config, store, env=assets.env,
                              real_home=assets.real_home,
                              version_probe=assets.version_probe)
    joined = " ".join(f"{n} {s} {d}" for n, s, d in rows)
    assert "manifest-secret-4b8e" not in joined
    assert AUTH_SECRET not in joined
    assert "ambient-secret-9c1d" not in joined
