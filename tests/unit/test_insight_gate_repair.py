"""V3 M10-R3: deep_value_gate 一次有界 contract-repair retry（P1）。

生产证据（M10 验收 §3）：`missing decision field` 跨频道 32–36% 首过
失败，重试 9/10 可恢复——是模型契约可靠性问题。冻结语义（M10-R R3）：
- 仅 contract 失败（缺必填字段 / enum 非法 / 结构坏）触发恰好一次
  repair：同输入 + 明确告知违反了哪个 schema 字段；
- 第二次仍非法 → `BLOCKED_MODEL_BAD_OUTPUT` fail-closed；
- 绝不由 parser 推测/补造 decision；
- rate limit / 子进程瞬态错误不走 repair（不把 schema 违规当网络错误）；
- 记录 first_attempt_valid / repair_attempted / repair_result /
  first_violation 以便 M10-R 统计。
"""

import pytest

from knowledge_ingest.insight.model_port import ModelBadOutputError, ModelTimeoutError
from knowledge_ingest.insight.models import InsightSourceView
from knowledge_ingest.insight.value_gate import DeepValueGate

NOW = "2026-09-28T12:00:00+00:00"
VALID = {"decision": "WATCH", "reason": "弱信号",
         "novelty": "low", "contradiction_value": "unknown"}
MISSING_DECISION = {"reason": "r", "novelty": "low"}
BAD_ENUM = dict(VALID, decision="SURE")


def make_view() -> InsightSourceView:
    return InsightSourceView(
        provider="telegram", source_item_id="tg_a:1", content_kind="text",
        title="t", visible_text="body", materialized_path=None,
        full_text_available=True, verification_status="source_only",
        content_fingerprint="fp-1", captured_at=NOW)


class ScriptedPort:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.payloads = []

    def run(self, stage, payload):
        assert stage == "deep_value_gate"
        self.payloads.append(payload)
        out = self.scripts.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_first_attempt_valid_no_repair_metadata():
    port = ScriptedPort([dict(VALID)])
    decision = DeepValueGate(port).evaluate(make_view(), {})
    assert decision.decision == "WATCH"
    assert len(port.payloads) == 1
    assert "contract_repair" not in port.payloads[0]
    assert decision.contract_repair is None   # first_attempt_valid=True


def test_missing_decision_repaired_on_second_attempt():
    port = ScriptedPort([dict(MISSING_DECISION), dict(VALID)])
    decision = DeepValueGate(port).evaluate(make_view(), {})
    assert decision.decision == "WATCH"
    assert len(port.payloads) == 2             # 恰好一次有界 repair
    repair = port.payloads[1]["contract_repair"]
    assert "decision" in repair["previous_violation"]
    assert repair["repair_instruction"]
    assert decision.contract_repair == {
        "first_attempt_valid": False, "repair_attempted": True,
        "repair_result": "repaired",
        "first_violation": "deep_value_gate: missing decision field"}


def test_invalid_enum_repaired_on_second_attempt():
    port = ScriptedPort([dict(BAD_ENUM), dict(VALID)])
    decision = DeepValueGate(port).evaluate(make_view(), {})
    assert decision.decision == "WATCH"
    assert decision.contract_repair["first_violation"].endswith(
        "unknown decision: 'SURE'")


def test_second_contract_failure_fails_closed_bounded():
    port = ScriptedPort([dict(MISSING_DECISION), dict(BAD_ENUM)])
    with pytest.raises(ModelBadOutputError) as exc:
        DeepValueGate(port).evaluate(make_view(), {})
    assert len(port.payloads) == 2             # 有界：绝不过度重试
    assert "BLOCKED_MODEL_BAD_OUTPUT" in str(exc.value)


def test_port_level_bad_output_also_repaired():
    port = ScriptedPort([
        ModelBadOutputError("deep_value_gate: invalid JSON object"),
        dict(VALID)])
    decision = DeepValueGate(port).evaluate(make_view(), {})
    assert decision.decision == "WATCH"
    assert decision.contract_repair["repair_result"] == "repaired"


def test_transient_error_not_routed_into_repair():
    port = ScriptedPort([ModelTimeoutError("codex exec timeout")])
    with pytest.raises(ModelTimeoutError):
        DeepValueGate(port).evaluate(make_view(), {})
    assert len(port.payloads) == 1             # 瞬态错误不消耗 repair
