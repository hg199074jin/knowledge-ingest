"""V3 M4: Stage 2 Deep-Value Gate（设计 §9）。

语义：回答"值不值得消耗一次真正昂贵的 Personal Thinking Engine"。
不用总分阈值——多维结构化判断 + 显式 decision
（DEEP_READ | WATCH | ARCHIVE_ONLY | REJECT）。

本机审阅意见 A2/Review §5 冻结：
- 接口 evaluate(view, candidate)，**无 personal_preview**——完整
  Personal Retrieval 只在 DEEP_READ 之后执行；
- contradiction_value 允许 "unknown"：此刻没有可靠个人认知证据时
  绝不为填字段推测用户旧认知；宁可多进 DEEP_READ，不制造 false
  negative。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .model_port import ModelBadOutputError
from .models import DEEP_VALUE_STATES, DeepValueDecision, InsightSourceView

_DIMENSIONS = ("novelty", "cognitive_delta_potential", "business_potential",
               "project_relevance", "transferability", "evidence_quality",
               "contradiction_value", "thinking_space")

CONTRADICTION_UNKNOWN_POLICY = (
    "此刻没有用户的个人认知检索结果（它发生在 DEEP_READ 之后）。"
    "判断 contradiction_value 时不要推测用户过去的认知：没有可靠"
    "依据就填 unknown，绝不为了填字段编造冲突或一致。")

# M10-R3（P1）：一次有界 contract repair——只针对结构化输出违约，
# 不针对 rate limit / 子进程瞬态错误；绝不由 parser 猜 decision。
CONTRACT_REPAIR_INSTRUCTION = (
    "你上一次的回复违反了输出契约（见 previous_violation）。"
    "请只返回一个完整 JSON 对象，必须包含 decision 字段且取值仅限 "
    "decision_states 列表内的字面量，八个小写维度字段取值仅限 "
    "low/medium/high/unknown。不允许散文、不允许省略 decision、"
    "不允许添加 JSON 之外的任何文字。基于同一原始材料重新给出完整判定。")


def _bad(message: str) -> ModelBadOutputError:
    return ModelBadOutputError(f"deep_value_gate: {message}")


@dataclass(frozen=True)
class DeepValueGate:
    model_port: object

    def evaluate(self, view: InsightSourceView,
                 candidate: dict) -> DeepValueDecision:
        payload = {
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
        try:
            raw = self.model_port.run("deep_value_gate", payload)
            return self._parse(raw)
        except ModelBadOutputError as first:
            repair_payload = dict(payload, contract_repair={
                "previous_violation": str(first),
                "repair_instruction": CONTRACT_REPAIR_INSTRUCTION,
            })
            try:
                raw = self.model_port.run("deep_value_gate", repair_payload)
                decision = self._parse(raw)
            except ModelBadOutputError as second:
                raise _bad("BLOCKED_MODEL_BAD_OUTPUT after contract "
                           f"repair; first={first}; second={second}") \
                    from second
            return replace(decision, contract_repair={
                "first_attempt_valid": False, "repair_attempted": True,
                "repair_result": "repaired",
                "first_violation": str(first)})

    @staticmethod
    def _parse(raw: dict) -> DeepValueDecision:
        if not isinstance(raw, dict) or "decision" not in raw:
            raise _bad("missing decision field")
        decision = raw["decision"]
        if decision not in DEEP_VALUE_STATES:
            raise _bad(f"unknown decision: {decision!r}")
        kwargs = {}
        for name in _DIMENSIONS:
            value = raw.get(name)
            if value is not None:
                value = str(value).strip().lower()
                if value not in ("low", "medium", "high", "unknown"):
                    raise _bad(f"unknown {name}: {raw.get(name)!r}")
            kwargs[name] = value
        return DeepValueDecision(
            decision=decision, reason=str(raw.get("reason") or ""), **kwargs)
