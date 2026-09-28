"""V3 M7: Cognitive Change Proposal——入口门禁与 provenance 继承。

入口门禁（M6 复核后用户冻结）：quality_status == passed 且
cognition_delta != NONE 才生成 Proposal——needs_review 绝不生成；
passed+NONE 也不生成（"没有 Proposal"不是验收失败）。

provenance 完整继承（复核②）：source_revision → context_pack_id →
card_id → evidence refs → related cognition refs——ADOPT 后的
confirmed cognition 必须保留"当初为什么形成"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

_VALID_DELTAS = ("ADD", "REINFORCE", "REVISE", "OVERTURN")


@dataclass(frozen=True)
class InsightCard:
    """M7 Proposal 的输入视图：一张已写入的 Deep Insight Card 元数据。"""

    card_id: str
    insight_source_id: str
    source_revision: int
    context_pack_id: str
    card_path: str
    quality_status: str
    cognition_delta: str
    title: str
    own_version: str
    mechanism: str
    challenge: str
    human_gate_recommendation: str
    evidence_refs: tuple[str, ...]
    related_cognition_refs: tuple[str, ...]
    source_item_id: str = ""
    related_cognitions: tuple = ()   # {"record_id","text","connection"} dicts
    topics: tuple = ()


@dataclass(frozen=True)
class CognitionProposal:
    """认知变更提案：与 Card 分离，Human Gate 前绝不写长期认知。"""

    proposal_id: str
    card_id: str
    insight_source_id: str
    source_revision: int
    context_pack_id: str
    change_type: str                  # ADD | REINFORCE | REVISE | OVERTURN
    proposed_cognition: str
    source_item_id: str = ""
    old_cognition: tuple = ()         # {"record_id","text","connection"}
    reasons: tuple = ()
    evidence_refs: tuple[str, ...] = ()
    related_cognition_refs: tuple[str, ...] = ()
    card_path: str = ""
    gate_status: str = "pending"
    gate_decision: str | None = None

    @property
    def domain(self) -> str:
        return "general"

    @property
    def provenance(self) -> dict:
        """完整溯源链：source_revision → context_pack_id → card_id →
        evidence refs → related cognition refs。"""
        return {
            "source_revision": self.source_revision,
            "context_pack_id": self.context_pack_id,
            "card_id": self.card_id,
            "card_path": self.card_path,
            "evidence_refs": list(self.evidence_refs),
            "related_cognition_refs": list(self.related_cognition_refs),
        }


def card_proposal_id(card_id: str, change_type: str,
                     proposed_cognition: str) -> str:
    digest = hashlib.sha256(
        f"{card_id}\x00{change_type}\x00{proposed_cognition}".encode()).hexdigest()
    return f"cp.{digest[:12]}"


def build_cognition_proposal(card: InsightCard,
                             **overrides) -> CognitionProposal | None:
    """入口门禁：passed + delta≠NONE 才生成；否则 None（幂等合法）。"""
    if card.quality_status not in ("passed", "needs_review"):
        raise ValueError(
            f"invalid quality_status: {card.quality_status!r}")
    if card.quality_status != "passed":
        return None
    if card.cognition_delta == "NONE":
        return None
    if card.cognition_delta not in _VALID_DELTAS:
        raise ValueError(f"invalid cognition_delta: {card.cognition_delta!r}")

    old_cognition = ()
    if card.cognition_delta in ("REVISE", "OVERTURN", "REINFORCE"):
        # ADD = 全新认知（无旧判断可引）；其余类型继承被作用旧认知
        old_cognition = tuple(
            {"record_id": c.get("record_id"), "text": c.get("text"),
             "connection": c.get("connection")}
            for c in card.related_cognitions)
    proposal_id = card_proposal_id(card.card_id, card.cognition_delta,
                                   card.own_version)
    values = {
        "proposal_id": proposal_id,
        "card_id": card.card_id,
        "insight_source_id": card.insight_source_id,
        "source_revision": card.source_revision,
        "context_pack_id": card.context_pack_id,
        "change_type": card.cognition_delta,
        "proposed_cognition": card.own_version,
        "old_cognition": old_cognition,
        "reasons": tuple(r for r in (card.mechanism, card.challenge) if r),
        "evidence_refs": tuple(card.evidence_refs),
        "related_cognition_refs": tuple(card.related_cognition_refs),
        "card_path": card.card_path,
        "gate_status": "pending",
        "gate_decision": None,
    }
    values["source_item_id"] = card.source_item_id
    values.update(overrides)
    return CognitionProposal(**values)
