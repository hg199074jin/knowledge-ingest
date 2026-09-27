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
