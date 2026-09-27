"""V3 M4: Stage 1 High-Recall Candidate Filter（设计 §7）。

语义：只回答"有没有合理可能值得进一步思考"。Recall 优先于
Precision——低置信 + 可能认知价值仍然 pass；明显无价值由模型
判 false（本层零硬编码规则）。

冻结纪律：
- 输出契约 {candidate, possible_value, reasons, confidence, trend_key}；
- 未知枚举/坏输出 → ModelBadOutputError（可重试），绝不伪造 false；
- 不持有 store、不产生任何 value-SKIP 学习规则（设计 §7.6）；
- 只用轻量主题画像（taste_profile），不运行 Personal Retrieval。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .model_port import ModelBadOutputError
from .models import CANDIDATE_VALUE_TYPES, CandidateDecision, InsightSourceView

HIGH_RECALL_DIRECTIVE = (
    "这是高召回筛选层：宁可多收候选，绝不漏掉可能产生认知增量、"
    "赚钱机会或项目影响的内容。主题常见或已熟悉不构成拒绝理由；"
    "只要存在值得思考的可能性就返回 candidate=true。只有明显属于"
    "广告、纯线报、无信息量的寒暄才返回 candidate=false。"
)


def _bad(message: str) -> ModelBadOutputError:
    return ModelBadOutputError(f"candidate_filter: {message}")


@dataclass(frozen=True)
class HighRecallCandidateFilter:
    model_port: object
    taste_profile: str = ""

    def evaluate(self, view: InsightSourceView) -> CandidateDecision:
        payload = {
            "directive": HIGH_RECALL_DIRECTIVE,
            "taste_profile": self.taste_profile,
            "value_types": list(CANDIDATE_VALUE_TYPES),
            "output_contract": {
                "candidate": "bool",
                "possible_value": "list of value types (may be empty)",
                "reasons": "list of short strings",
                "confidence": "0.0-1.0",
                "trend_key": "optional normalized concept key for weak signals",
            },
            "kind": view.content_kind,
            "title": view.title,
            "visible_text": view.visible_text,
        }
        raw = self.model_port.run("candidate_filter", payload)
        return self._parse(raw)

    @staticmethod
    def _parse(raw: dict) -> CandidateDecision:
        if not isinstance(raw, dict) or "candidate" not in raw:
            raise _bad("missing candidate field")
        candidate = raw["candidate"]
        if not isinstance(candidate, bool):
            raise _bad(f"candidate must be bool, got {candidate!r}")
        values = []
        for value in raw.get("possible_value") or []:
            normalized = str(value).strip().upper()
            if normalized not in CANDIDATE_VALUE_TYPES:
                raise _bad(f"unknown value type: {value!r}")
            values.append(normalized)
        reasons = [str(r) for r in (raw.get("reasons") or [])]
        confidence = raw.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            raise _bad(f"confidence out of range: {confidence!r}")
        trend_key = raw.get("trend_key") or None
        if trend_key is not None and not isinstance(trend_key, str):
            raise _bad("trend_key must be string or null")
        return CandidateDecision(
            candidate=candidate, possible_value=tuple(values),
            reasons=tuple(reasons), confidence=float(confidence),
            trend_key=trend_key)


def candidate_payload_digest(decision: CandidateDecision) -> str:
    """调试辅助：决策内容的稳定 JSON（用于审计对比）。"""
    return json.dumps({
        "candidate": decision.candidate,
        "possible_value": list(decision.possible_value),
        "confidence": decision.confidence,
    }, ensure_ascii=False, sort_keys=True)
