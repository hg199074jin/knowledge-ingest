"""V3 M4: DeepValueGate — 第二层多维价值门契约。

冻结语义（Review §5 修订）：
- 接口为 evaluate(view, candidate)：**无 personal_preview 参数**——
  完整 Personal Retrieval 只在 DEEP_READ 之后由 M5 执行；
- 输出多维结构化字段 + 显式 decision（四态）；
  无总分阈值；
- contradiction_value 允许 unknown：此时无可靠个人认知证据，
  绝不为填字段推测用户旧认知；
- 契约违约（M10-R3）：首次违约触发一次有界 contract repair，
  两次仍非法 → 类型化可重试错误（BLOCKED_MODEL_BAD_OUTPUT）。
"""

import inspect

import pytest

from knowledge_ingest.insight.model_port import ModelBadOutputError
from knowledge_ingest.insight.models import DeepValueDecision, InsightSourceView
from knowledge_ingest.insight.value_gate import DeepValueGate

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "年入500万的私域算法",
        "visible_text": "1000个群×每天5单×每单5元……",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


CANDIDATE_SUMMARY = {"possible_value": ["BUSINESS_OPPORTUNITY"],
                     "reasons": ["交易结构可检验"], "confidence": 0.7}

GATE_OK = {
    "novelty": "high", "cognitive_delta_potential": "high",
    "business_potential": "medium", "project_relevance": "low",
    "transferability": "high", "evidence_quality": "medium",
    "contradiction_value": "unknown", "thinking_space": "high",
    "decision": "DEEP_READ", "reason": "Excel 乘法≠商业模式，值得拆",
}


class FakePort:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def run(self, stage, payload):
        self.calls.append((stage, payload))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_gate(script):
    port = FakePort(script)
    return DeepValueGate(port), port


def test_full_dimensions_parse_to_decision():
    gate, port = make_gate([dict(GATE_OK)])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert isinstance(decision, DeepValueDecision)
    assert decision.decision == "DEEP_READ"
    assert decision.contradiction_value == "unknown"
    stage, payload = port.calls[0]
    assert stage == "deep_value_gate"
    assert payload["candidate"] == CANDIDATE_SUMMARY
    assert "1000个群" in payload["visible_text"]


def test_poor_writing_but_valuable_structure_still_deep_read():
    gate, _ = make_gate([dict(GATE_OK, evidence_quality="low")])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert decision.decision == "DEEP_READ"   # 文笔差≠价值低（设计 §9.4）


def test_watch_archive_reject_all_valid():
    for state in ("WATCH", "ARCHIVE_ONLY", "REJECT"):
        gate, _ = make_gate([dict(GATE_OK, decision=state)])
        assert gate.evaluate(make_view(), CANDIDATE_SUMMARY).decision == state


def test_missing_dimensions_default_none_not_fabricated():
    gate, _ = make_gate([{"decision": "WATCH", "reason": "证据不足"}])
    decision = gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert decision.novelty is None
    assert decision.contradiction_value is None


def test_contradiction_unknown_is_legal():
    gate, _ = make_gate([dict(GATE_OK, contradiction_value="unknown")])
    assert gate.evaluate(make_view(), CANDIDATE_SUMMARY).contradiction_value \
        == "unknown"


def test_unknown_decision_raises_retryable():
    # M10-R3：首次违约触发一次 contract repair；两次仍非法才 fail-closed
    gate, port = make_gate([dict(GATE_OK, decision="MAYBE"),
                            dict(GATE_OK, decision="MAYBE")])
    with pytest.raises(ModelBadOutputError) as exc:
        gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert len(port.calls) == 2
    assert "BLOCKED_MODEL_BAD_OUTPUT" in str(exc.value)


def test_bad_output_raises_retryable():
    # M10-R3：缺 decision → 一次 repair → 仍缺 → fail-closed
    gate, port = make_gate([{"novelty": "high"}, {"novelty": "high"}])
    with pytest.raises(ModelBadOutputError) as exc:
        gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    assert len(port.calls) == 2
    assert "BLOCKED_MODEL_BAD_OUTPUT" in str(exc.value)


def test_interface_has_no_personal_preview_parameter():
    """Review §5：完整 Personal Retrieval 在 DEEP_READ 之后（M5），
    第二层不得依赖它。"""
    params = inspect.signature(DeepValueGate.evaluate).parameters
    assert "personal_preview" not in params
    assert list(params)[1] == "view" and list(params)[2] == "candidate"


def test_prompt_forbids_speculating_past_cognition():
    gate, port = make_gate([dict(GATE_OK)])
    gate.evaluate(make_view(), CANDIDATE_SUMMARY)
    import json
    blob = json.dumps(port.calls[0][1], ensure_ascii=False)
    assert "unknown" in blob and "不要推测" in blob


# ---------- R2 CAL-1（Issue #31 §12）----------

from knowledge_ingest.insight.model_port import (
    ModelCommandFailedError,
    ModelEmptyOutputError,
    ModelNotConfiguredError,
    ModelTimeoutError,
)
from knowledge_ingest.insight.value_gate import (
    CONTRACT_REPAIR_EXHAUSTED,
    DEEP_VALUE_STATES,
    DIMENSION_VALUES,
    GateContractViolation,
    GateContractViolationCode,
    deep_value_output_contract,
)


class R2ScriptedPort:
    """按脚本返回；记录每次 payload 以便断言契约与 repair 语义。"""

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


VALID_R2 = {"decision": "DEEP_READ", "reason": "ok",
            "novelty": "high", "contradiction_value": "unknown"}


def r2_view() -> InsightSourceView:
    return InsightSourceView(
        provider="telegram", source_item_id="r2:1", content_kind="text",
        title="t", visible_text="body", materialized_path=None,
        full_text_available=True, verification_status="source_only",
        content_fingerprint="fp-r2", captured_at=NOW)


# --- §12.1 first payload 已带权威契约 ---

def test_first_attempt_payload_carries_authoritative_contract():
    port = R2ScriptedPort([dict(VALID_R2)])
    DeepValueGate(port).evaluate(r2_view(), {})
    contract = port.payloads[0]["output_contract"]
    assert contract["type"] == "object"
    assert contract["required"] == ["decision"]
    assert contract["decision"] == list(DEEP_VALUE_STATES)
    assert set(contract["dimensions"]) == set(DIMENSION_ENUM_EXPECTED)
    for name, allowed in contract["dimensions"].items():
        assert allowed == [*DIMENSION_VALUES, None], name
    assert "contract_repair" not in port.payloads[0]


DIMENSION_ENUM_EXPECTED = ("novelty", "cognitive_delta_potential",
                           "business_potential", "project_relevance",
                           "transferability", "evidence_quality",
                           "contradiction_value", "thinking_space")


def test_valid_first_attempt_is_single_call():
    port = R2ScriptedPort([dict(VALID_R2)])
    decision = DeepValueGate(port).evaluate(r2_view(), {})
    assert decision.decision == "DEEP_READ"
    assert len(port.payloads) == 1
    assert decision.contract_repair is None      # absence == first-valid (§9)


def test_contract_is_single_shared_source():
    """first prompt / repair / parser 三处必须同源（§5）。"""
    contract = deep_value_output_contract()
    assert contract["decision"] == list(DEEP_VALUE_STATES)
    # parser 接受的值域与契约声明一致
    assert set(DIMENSION_VALUES) <= set(contract["dimensions"]["novelty"])
    port = R2ScriptedPort([{"reason": "no decision"}, {"decision": "WATCH"}])
    DeepValueGate(port).evaluate(r2_view(), {})
    assert port.payloads[0]["output_contract"] == contract
    assert port.payloads[1]["contract_repair"]["output_contract"] == contract


# --- §12.3/§12.4 冻结业务语义未变 ---

def test_missing_dimensions_still_none_and_extra_keys_ignored():
    port = R2ScriptedPort([{"decision": "WATCH",
                            "some_model_extra_field": "ignored"}])
    decision = DeepValueGate(port).evaluate(r2_view(), {})
    assert decision.decision == "WATCH"
    assert decision.novelty is None and decision.thinking_space is None


def test_contradiction_unknown_still_legal():
    port = R2ScriptedPort([dict(VALID_R2)])
    assert DeepValueGate(port).evaluate(
        r2_view(), {}).contradiction_value == "unknown"


# --- §12.6-9 violation taxonomy → repair ---

@pytest.mark.parametrize("bad,code,field", [
    ({"reason": "r"}, GateContractViolationCode.MISSING_DECISION, "decision"),
    ({"decision": "MAYBE"}, GateContractViolationCode.INVALID_DECISION, "decision"),
    ({"decision": "WATCH", "novelty": "extreme"},
     GateContractViolationCode.INVALID_DIMENSION_VALUE, "novelty"),
    ("not-a-dict", GateContractViolationCode.NOT_OBJECT, None),
])
def test_first_attempt_violations_map_to_stable_codes(bad, code, field):
    port = R2ScriptedPort([bad, dict(VALID_R2)])
    decision = DeepValueGate(port).evaluate(r2_view(), {})
    assert decision.contract_repair["first_violation_code"] == code.value
    repair = port.payloads[1]["contract_repair"]
    assert repair["violation_code"] == code.value
    assert repair["violation_field"] == (field or "")


def test_port_level_bad_output_is_not_object_code():
    port = R2ScriptedPort([ModelBadOutputError("stdout has no JSON object"),
                           dict(VALID_R2)])
    decision = DeepValueGate(port).evaluate(r2_view(), {})
    assert decision.contract_repair["first_violation_code"] == "NOT_OBJECT"


# --- §12.10-12 repair payload 内容 ---

def test_repair_payload_has_no_raw_model_output():
    secret_marker = "MODEL-RAW-SECRET-9f2a"
    # 首轮必须真的违约（缺 decision）才会 repair；marker 藏在首轮输出里
    port = R2ScriptedPort([{"reason": secret_marker}, dict(VALID_R2)])
    DeepValueGate(port).evaluate(r2_view(), {})
    repair = port.payloads[1]["contract_repair"]
    assert secret_marker not in str(repair)
    assert "previous_violation" not in repair          # 旧自由文本字段已移除
    assert repair["output_contract"]["required"] == ["decision"]
    # 同一份原始业务输入仍在
    assert port.payloads[1]["visible_text"] == port.payloads[0]["visible_text"]
    assert port.payloads[1]["candidate"] == port.payloads[0]["candidate"]


def test_repair_instruction_built_from_same_contract():
    port = R2ScriptedPort([{"reason": "r"}, dict(VALID_R2)])
    DeepValueGate(port).evaluate(r2_view(), {})
    instruction = port.payloads[1]["contract_repair"]["repair_instruction"]
    for state in DEEP_VALUE_STATES:
        assert state in instruction
    for value in DIMENSION_VALUES:
        assert value in instruction


def test_second_malformed_is_bounded_fail_closed_with_stable_code():
    port = R2ScriptedPort([{"reason": "r"}, {"decision": "NOPE"}])
    with pytest.raises(GateContractViolation) as exc:
        DeepValueGate(port).evaluate(r2_view(), {})
    assert len(port.payloads) == 2                     # 恰好两次调用
    assert exc.value.code == CONTRACT_REPAIR_EXHAUSTED
    assert str(exc.value) == "deep_value_gate: BLOCKED_MODEL_BAD_OUTPUT"


# --- §12.16/17 transient / execution error 绝不 repair ---

@pytest.mark.parametrize("exc_type", [
    ModelTimeoutError, ModelCommandFailedError, ModelEmptyOutputError,
    ModelNotConfiguredError,
])
def test_transient_errors_never_trigger_repair(exc_type):
    port = R2ScriptedPort([exc_type("boom")])
    with pytest.raises(exc_type):
        DeepValueGate(port).evaluate(r2_view(), {})
    assert len(port.payloads) == 1                     # 只调用一次，不 repair


def test_violation_message_never_echoes_model_value():
    marker = "LEAKED-MODEL-VALUE-4b8e"
    port = R2ScriptedPort([{"decision": marker}, dict(VALID_R2)])
    decision = DeepValueGate(port).evaluate(r2_view(), {})
    assert marker not in str(decision.contract_repair)
    assert marker not in str(port.payloads[1]["contract_repair"])
