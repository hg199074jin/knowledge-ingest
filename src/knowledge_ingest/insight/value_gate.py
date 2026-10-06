"""V3 M4: Stage 2 Deep-Value Gate（设计 §9）+ R2 CAL-1 contract hardening。

语义：回答"值不值得消耗一次真正昂贵的 Personal Thinking Engine"。
不用总分阈值——多维结构化判断 + 显式 decision
（DEEP_READ | WATCH | ARCHIVE_ONLY | REJECT）。

本机审阅意见 A2/Review §5 冻结：
- 接口 evaluate(view, candidate)，**无 personal_preview**——完整
  Personal Retrieval 只在 DEEP_READ 之后执行；
- contradiction_value 允许 "unknown"：此刻没有可靠个人认知证据时
  绝不为填字段推测用户旧认知；宁可多进 DEEP_READ，不制造 false
  negative。

R2（Issue #31 §2 CAL-1）结构性根因与修复：

- **根因**：第一轮 payload 没有正式 output_contract，模型必须"猜结构"；
  真正的严格契约只在首次违约后由 repair instruction 补发 → repair 成了常态。
- **修复**：把权威契约前置到**第一次调用**，parser / first prompt / repair
  共用同一份 contract semantics（`deep_value_output_contract()` +
  `DEEP_VALUE_STATES` + `DIMENSION_VALUES`），使 repair 退化为异常兜底。
- 违规原因变成有限、sanitized、可统计的 `GateContractViolationCode`；**不再**
  把模型原文/自由异常文本写进 repair payload、provenance 或 error_code。
- 业务语义全部冻结未改：四态判定、缺失维度 → None、contradiction unknown 合法、
  repair 最多一次、transient/model execution error 不触发 repair。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from .model_port import ModelBadOutputError
from .models import DEEP_VALUE_STATES, DeepValueDecision, InsightSourceView

# ---------- 单一 contract source（§5）----------

_DIMENSIONS = ("novelty", "cognitive_delta_potential", "business_potential",
               "project_relevance", "transferability", "evidence_quality",
               "contradiction_value", "thinking_space")

#: 维度取值域。`None`（字段缺失或 null）保持合法 → 决策对象里就是 None，
#: 绝不伪造默认值（`test_missing_dimensions_default_none_not_fabricated`）。
DIMENSION_VALUES = ("low", "medium", "high", "unknown")

#: repair 恰好一次（§7：禁止无限重试）。
MAX_CONTRACT_REPAIRS = 1

CONTRACT_REPAIR_EXHAUSTED = "BLOCKED_MODEL_BAD_OUTPUT"


class GateContractViolationCode(str, enum.Enum):
    """有限、sanitized 的违规分类（§6）。R3 将据此区分"判断错"与"契约坏"。"""

    NOT_OBJECT = "NOT_OBJECT"
    MISSING_DECISION = "MISSING_DECISION"
    INVALID_DECISION = "INVALID_DECISION"
    INVALID_DIMENSION_VALUE = "INVALID_DIMENSION_VALUE"


class GateContractViolation(ModelBadOutputError):
    """契约违规：携带 stable code，**不回显模型原文**。

    `field` 只接受冻结的字段名常量（decision / 八个维度名），绝不携带模型
    返回的取值或任何 source 正文。
    """

    def __init__(self, code: GateContractViolationCode | str, *,
                 field: str | None = None) -> None:
        self.code = code.value if isinstance(code, enum.Enum) else str(code)
        self.field = field
        detail = f"deep_value_gate: {self.code}"
        if field:
            detail += f" field={field}"
        super().__init__(detail)


def deep_value_output_contract() -> dict:
    """权威输出契约——first prompt、repair、parser 共用的唯一语义来源。"""
    return {
        "type": "object",
        "required": ["decision"],
        "decision": list(DEEP_VALUE_STATES),
        "dimensions": {name: [*DIMENSION_VALUES, None]
                       for name in _DIMENSIONS},
        "reason": "string (optional, may be empty)",
        "rules": [
            "decision 必须存在，且只能取 decision 列出的字面量",
            "每个维度字段可选：缺失或 null 都合法，表示未判定，不得编造",
            "维度取值只能是 low / medium / high / unknown / null",
            ("只返回一个 JSON 对象：不要散文、不要 markdown fence、"
             "不要 JSON 之外的任何文字"),
        ],
    }


CONTRADICTION_UNKNOWN_POLICY = (
    "此刻没有用户的个人认知检索结果（它发生在 DEEP_READ 之后）。"
    "判断 contradiction_value 时不要推测用户过去的认知：没有可靠"
    "依据就填 unknown，绝不为了填字段编造冲突或一致。")


def build_contract_repair_instruction(contract: dict) -> str:
    """repair 指令由同一份 contract 构造（§5：不留第二份手写四态/八维）。"""
    return (
        "你上一次的回复违反了输出契约（violation_code 已给出）。"
        "请严格按下面这份权威契约重新输出，decision 必须存在且取值仅限："
        + "/".join(contract["decision"])
        + "。维度取值仅限：" + "/".join(DIMENSION_VALUES)
        + "/null；缺失维度保持缺失即可。只返回一个完整 JSON 对象，"
        "不要散文、不要 markdown fence、不要 JSON 之外的任何文字。"
        "基于同一原始材料重新给出完整判定，不要只输出被点名的字段。"
        "\n权威契约：\n" + repr(contract))


def _base_payload(view: InsightSourceView, candidate: dict) -> dict:
    """业务输入 payload——first attempt 与 repair 共用同一份原始输入。"""
    return {
        "dimensions": list(_DIMENSIONS),
        "decision_states": list(DEEP_VALUE_STATES),
        "contradiction_policy": CONTRADICTION_UNKNOWN_POLICY,
        "kind": view.content_kind,
        "title": view.title,
        "visible_text": view.visible_text,
        "candidate": {
            "possible_value": list(candidate.get("possible_value", ())),
            "reasons": list(candidate.get("reasons", ())),
            "confidence": candidate.get("confidence"),
        },
    }


def _classify(exc: BaseException) -> GateContractViolation:
    """把任意"输出不可用"异常归一为 stable violation code。

    port 层 ModelBadOutputError（stdout 无完整 JSON object / 散文 / fence）
    语义上就是 NOT_OBJECT；其余 ModelPortError 子类（timeout / 非零退出 /
    空输出 / 未配置）**不在**此处捕获，不触发 repair（§7）。
    """
    if isinstance(exc, GateContractViolation):
        return exc
    return GateContractViolation(GateContractViolationCode.NOT_OBJECT)


@dataclass(frozen=True)
class DeepValueGate:
    model_port: object

    def evaluate(self, view: InsightSourceView,
                 candidate: dict) -> DeepValueDecision:
        contract = deep_value_output_contract()
        payload = _base_payload(view, candidate)
        # CAL-1 修复：第一次调用就带权威契约。
        payload["output_contract"] = contract

        try:
            return self._parse(self.model_port.run("deep_value_gate", payload))
        except ModelBadOutputError as first:
            violation = _classify(first)
            repair_payload = dict(payload, contract_repair={
                "violation_code": violation.code,
                "violation_field": violation.field or "",
                "output_contract": contract,
                "repair_instruction": build_contract_repair_instruction(contract),
            })
            try:
                decision = self._parse(
                    self.model_port.run("deep_value_gate", repair_payload))
            except ModelBadOutputError:
                # 有界耗尽：stable code，不带任何模型原文。
                raise GateContractViolation(CONTRACT_REPAIR_EXHAUSTED) \
                    from None
            return replace(decision, contract_repair={
                "first_attempt_valid": False,
                "repair_attempted": True,
                "repair_result": "repaired",
                "first_violation_code": violation.code,
            })

    @staticmethod
    def _parse(raw: dict) -> DeepValueDecision:
        if not isinstance(raw, dict):
            raise GateContractViolation(GateContractViolationCode.NOT_OBJECT)
        if "decision" not in raw:
            raise GateContractViolation(
                GateContractViolationCode.MISSING_DECISION, field="decision")
        decision = raw["decision"]
        if decision not in DEEP_VALUE_STATES:
            raise GateContractViolation(
                GateContractViolationCode.INVALID_DECISION, field="decision")
        kwargs = {}
        for name in _DIMENSIONS:
            value = raw.get(name)
            if value is not None:
                normalized = str(value).strip().lower()
                if normalized not in DIMENSION_VALUES:
                    raise GateContractViolation(
                        GateContractViolationCode.INVALID_DIMENSION_VALUE,
                        field=name)
                value = normalized
            kwargs[name] = value
        return DeepValueDecision(
            decision=decision, reason=str(raw.get("reason") or ""), **kwargs)