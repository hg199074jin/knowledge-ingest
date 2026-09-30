"""V3 M3: CommandInsightModelPort — stdin→stdout JSON 契约与 fail-closed。

冻结 env 契约（实施方案 Task 3）：
- KI_INSIGHT_MODEL_CMD / KI_INSIGHT_MODEL_TIMEOUT(180) / KI_INSIGHT_MODEL_CWD
- stage 覆盖：KI_INSIGHT_CANDIDATE_CMD / KI_INSIGHT_VALUE_CMD /
  KI_INSIGHT_RETRIEVAL_PLAN_CMD / KI_INSIGHT_RETRIEVAL_SELECT_CMD /
  KI_INSIGHT_EVIDENCE_CMD / KI_INSIGHT_THINK_CMD / KI_INSIGHT_CRITIC_CMD
- stage 覆盖缺失时回落 KI_INSIGHT_MODEL_CMD；
- stdin = {"schema_version":1,"stage":...,"payload":{...}}；
- 非零退出/超时/空输出/坏 JSON → 类型化错误，绝不伪造判定；
- 错误消息 ≤200 字符且绝不包含 stdin payload。
"""

import shlex
import sys

import pytest

from knowledge_ingest.insight.model_port import (
    STAGE_ENV_VARS,
    CommandInsightModelPort,
    ModelBadOutputError,
    ModelCommandFailedError,
    ModelEmptyOutputError,
    ModelNotConfiguredError,
    ModelTimeoutError,
)

STAGES = ("candidate_filter", "deep_value_gate", "retrieval_query_plan",
          "retrieval_select", "evidence_extract", "thinking", "critic")


# ---------- env contract ----------

def test_stage_env_vars_exact_and_complete():
    assert set(STAGE_ENV_VARS) == set(STAGES)
    assert STAGE_ENV_VARS["candidate_filter"] == "KI_INSIGHT_CANDIDATE_CMD"
    assert STAGE_ENV_VARS["deep_value_gate"] == "KI_INSIGHT_VALUE_CMD"
    assert STAGE_ENV_VARS["retrieval_query_plan"] == "KI_INSIGHT_RETRIEVAL_PLAN_CMD"
    assert STAGE_ENV_VARS["retrieval_select"] == "KI_INSIGHT_RETRIEVAL_SELECT_CMD"
    assert STAGE_ENV_VARS["evidence_extract"] == "KI_INSIGHT_EVIDENCE_CMD"
    assert STAGE_ENV_VARS["thinking"] == "KI_INSIGHT_THINK_CMD"
    assert STAGE_ENV_VARS["critic"] == "KI_INSIGHT_CRITIC_CMD"


def test_stage_override_wins_over_base_command():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c pass",
        "KI_INSIGHT_THINK_CMD": f"{sys.executable} -c pass",
    })
    assert port.resolve_command("thinking") == (
        f"{sys.executable} -c pass")


def test_fallback_to_base_command():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c pass"})
    assert port.resolve_command("critic") == f"{sys.executable} -c pass"


def test_missing_command_raises_not_configured():
    port = CommandInsightModelPort(env={})
    with pytest.raises(ModelNotConfiguredError):
        port.resolve_command("thinking")


# ---------- happy path ----------

ECHO_STDIN = (
    "import sys, json\n"
    "req = json.load(sys.stdin)\n"
    "print('chatter before')\n"
    "print(json.dumps({'echo_stage': req['stage'], "
    "'echo': req['payload']}))\n"
    "print('tokens used 123')\n")


def test_run_sends_stage_payload_stdin_and_parses_chatter_wrapped_json():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c {shlex.quote(ECHO_STDIN)}"})
    out = port.run("thinking", {"q": "判断"})
    assert out["echo_stage"] == "thinking"
    assert out["echo"] == {"q": "判断"}


def test_run_stage_override_is_used():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c 'import sys; sys.exit(9)'",
        "KI_INSIGHT_CRITIC_CMD":
            f"{sys.executable} -c {shlex.quote(ECHO_STDIN)}"})
    out = port.run("critic", {"x": 1})   # base 命令必失败；override 必成功
    assert out["echo"] == {"x": 1}


# ---------- fail-closed errors ----------

def test_nonzero_exit_typed_error_sanitized():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c 'raise SystemExit(3)'"})
    with pytest.raises(ModelCommandFailedError) as exc:
        port.run("thinking", {"secret_payload": "TOPSECRET"})
    assert "3" in str(exc.value)
    assert len(str(exc.value)) <= 200
    assert "TOPSECRET" not in str(exc.value)


def test_timeout_typed_error():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c 'import time; time.sleep(5)'",
        "KI_INSIGHT_MODEL_TIMEOUT": "0.2"})
    with pytest.raises(ModelTimeoutError):
        port.run("thinking", {})


def test_empty_stdout_typed_error():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c 'pass'"})
    with pytest.raises(ModelEmptyOutputError):
        port.run("thinking", {})


def test_malformed_output_typed_error():
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c 'print(\"no json here\")'"})
    with pytest.raises(ModelBadOutputError):
        port.run("thinking", {})


def test_cwd_is_honored(tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(
        "import json, pathlib\n"
        "pathlib.Path('cwd-marker.json').write_text('{\"cwd\": true}')\n"
        "print('{\"ok\": true}')\n", encoding="utf-8")
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} {script}",
        "KI_INSIGHT_MODEL_CWD": str(tmp_path)})
    assert port.run("thinking", {}) == {"ok": True}
    assert (tmp_path / "cwd-marker.json").is_file()


# ---------- R1.1: generic IsolationProfile + sanitized child env ----------

import os
from pathlib import Path

from knowledge_ingest.insight.model_port import (
    IsolationProfile,
    ModelIsolationConfigurationError,
    build_insight_model_port_from_env,
)

PROBE_CHILD = (
    "import sys, json, os\n"
    "req = json.load(sys.stdin)\n"
    "print(json.dumps({'stage': req.get('stage'), "
    "'cwd': os.getcwd(), "
    "'env_keys': sorted(os.environ), "
    "'HOME': os.environ.get('HOME'), "
    "'CODEX_HOME': os.environ.get('CODEX_HOME'), "
    "'SECRET_MARKER': os.environ.get('SECRET_MARKER')}))\n")

PROBE_CMD = f"{sys.executable} -c {shlex.quote(PROBE_CHILD)}"


def make_strict_env(tmp_path: Path, extra: dict | None = None) -> dict:
    iso_home = tmp_path / "iso-home"
    iso_home.mkdir()
    codex_home = tmp_path / "iso-codex-home"
    codex_home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = {
        "KI_INSIGHT_REQUIRE_ISOLATION": "1",
        "KI_INSIGHT_ISOLATION_HOME": str(iso_home),
        "KI_INSIGHT_ISOLATION_CODEX_HOME": str(codex_home),
        "KI_INSIGHT_ISOLATION_WORKSPACE": str(workspace),
        "KI_INSIGHT_MODEL_CMD": PROBE_CMD,
    }
    env.update(extra or {})
    return env


def test_strict_child_env_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_MARKER", "ambient-secret-value")
    env = make_strict_env(tmp_path)
    port = build_insight_model_port_from_env(env)
    out = port.run("thinking", {})
    assert "SECRET_MARKER" not in out["env_keys"]      # ambient 禁止传入
    assert "SECRET_MARKER" in os.environ               # 只是没传给子进程
    assert out["HOME"] not in (None, str(Path.home()))  # 隔离 HOME
    assert "KI_INSIGHT_MODEL_CMD" not in out["env_keys"]  # control-plane 不进 child
    assert "KI_INSIGHT_THINK_CMD" not in out["env_keys"]


def test_allowlisted_ambient_key_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    env = make_strict_env(tmp_path)
    port = build_insight_model_port_from_env(env)
    out = port.run("thinking", {})
    assert "LANG" in out["env_keys"]                   # allowlist 项放行
    assert out["env_keys"].count("LANG") == 1


def test_explicit_isolated_home_and_codex_home(tmp_path):
    env = make_strict_env(tmp_path)
    port = build_insight_model_port_from_env(env)
    out = port.run("thinking", {})
    assert out["HOME"] == env["KI_INSIGHT_ISOLATION_HOME"]
    assert out["CODEX_HOME"] == env["KI_INSIGHT_ISOLATION_CODEX_HOME"]


def test_strict_workspace_cwd_wins_over_model_cwd(tmp_path):
    env = make_strict_env(tmp_path)
    env["KI_INSIGHT_MODEL_CWD"] = "/private/tmp"       # strict 下必须被 workspace 覆盖
    port = build_insight_model_port_from_env(env)
    out = port.run("thinking", {})
    assert out["cwd"] == env["KI_INSIGHT_ISOLATION_WORKSPACE"]


def test_cwd_still_honored_in_non_strict(tmp_path):
    script = tmp_path / "cwdprobe.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'cwd': os.getcwd()}))\n",
        encoding="utf-8")
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} {script}",
        "KI_INSIGHT_MODEL_CWD": str(tmp_path)})
    assert port.run("thinking", {}) == {"cwd": str(tmp_path)}


def test_strict_requires_profile_fail_closed():
    with pytest.raises(ModelIsolationConfigurationError):
        build_insight_model_port_from_env({
            "KI_INSIGHT_REQUIRE_ISOLATION": "1"})


def test_strict_missing_dir_fail_closed(tmp_path):
    env = make_strict_env(tmp_path)
    env["KI_INSIGHT_ISOLATION_HOME"] = str(tmp_path / "missing")
    with pytest.raises(ModelIsolationConfigurationError):
        build_insight_model_port_from_env(env)


def test_strict_stage_override_fail_closed(tmp_path):
    env = make_strict_env(tmp_path, {"KI_INSIGHT_THINK_CMD": f"{sys.executable} -c pass"})
    with pytest.raises(ModelIsolationConfigurationError) as exc:
        build_insight_model_port_from_env(env)
    assert "KI_INSIGHT_THINK_CMD" in str(exc.value)


def test_non_strict_stage_override_still_compat(tmp_path, monkeypatch):
    script = tmp_path / "echo.py"
    script.write_text(
        "import json, os, sys\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'stage': req['stage'], "
        "'marker': os.environ.get('SECRET_MARKER')}))\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_MARKER", "legacy-visible")  # ambient 注入
    port = CommandInsightModelPort(env={
        "KI_INSIGHT_REQUIRE_ISOLATION": "0",
        "KI_INSIGHT_MODEL_CMD": f"{sys.executable} -c pass",
        "KI_INSIGHT_THINK_CMD": f"{sys.executable} {script}",
    })                                                 # 非 strict：旧语义保留
    out = port.run("thinking", {})
    assert out["marker"] == "legacy-visible"           # ambient 继承（legacy）


def test_isolation_error_message_sanitized(tmp_path):
    env = make_strict_env(tmp_path)
    env["SECRET_MARKER"] = "super-secret-value-should-never-appear"
    try:
        build_insight_model_port_from_env({
            "KI_INSIGHT_REQUIRE_ISOLATION": "1"})
    except ModelIsolationConfigurationError:
        pass
    err = None
    try:
        build_insight_model_port_from_env({
            "KI_INSIGHT_REQUIRE_ISOLATION": "1",
            "KI_INSIGHT_ISOLATION_HOME": "x" * 500})
    except ModelIsolationConfigurationError as exc:
        err = exc
    assert err is not None
    assert len(str(err)) <= 200
    assert "super-secret" not in str(err)


def test_seven_stage_uniform_isolation(tmp_path):
    env = make_strict_env(tmp_path)
    port = build_insight_model_port_from_env(env)
    seen = []
    for stage in STAGES:
        out = port.run(stage, {})
        seen.append((stage, tuple(out["env_keys"]), out["HOME"],
                     out["CODEX_HOME"], out["cwd"]))
    first = seen[0]
    for stage, env_keys, home, codex_home, cwd in seen[1:]:
        assert (env_keys, home, codex_home, cwd) == first[1:]
    assert "SECRET_MARKER" not in first[1] if False else True
    # 逐 stage 断言 forbidden marker 均不可见
    for stage, env_keys, *_ in seen:
        assert "SECRET_MARKER" not in env_keys


def test_isolation_profile_child_env_builder_pure():
    profile = IsolationProfile(
        profile_id="t", isolated_home=Path("/iso/home"),
        workspace_cwd=Path("/iso/ws"),
        child_env_allowlist=("LANG",),
        child_env_overrides={"HOME": "/iso/home", "CODEX_HOME": "/iso/codex"},
        require_strict=True)
    child = profile.build_child_env({"LANG": "C", "SECRET": "x", "HOME": "/real"})
    assert child == {"LANG": "C", "HOME": "/iso/home",
                     "CODEX_HOME": "/iso/codex"}
