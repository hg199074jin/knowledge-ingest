"""V3 M7: Human Gate——五决策状态机（设计 §17；用户冻结约束③④）。

语义冻结：
- ADOPT → 唯一允许写 confirmed cognition 的路径（含完整 provenance）；
- EXPERIMENT → 只建 experiment stub；WATCH → 只进观察；
- ARCHIVE/REJECT → 关闭 proposal，不改认知；
- 同决策幂等；异决策 GateConflictError（绝不静默覆盖）；
- decision source 显式记录（resolved_by，默认 'user'）——模型侧模块
  （thinker/critic/cards/evidence/retrieval）不暴露 resolve()，
  长期认知写入只能发生在这里。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .proposals import CognitionProposal
from .store import InsightStore

_GATE_DECISIONS = ("ADOPT", "EXPERIMENT", "WATCH", "ARCHIVE", "REJECT")


class GateConflictError(RuntimeError):
    """同一 proposal 收到与已记录不同的决策（绝不静默覆盖）。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return ascii_part[:40] or "cognition"


@dataclass(frozen=True)
class GateResolution:
    proposal_id: str
    decision: str
    gate_status: str
    resolved_at: str
    resolved_by: str
    outputs: tuple[str, ...]
    already_resolved: bool = False


class InsightHumanGate:
    """长期认知写入的唯一合法入口；CLI 是唯一触发面（M8 接线）。"""

    def __init__(self, store: InsightStore, insight_root: Path):
        self.store = store
        self.insight_root = Path(insight_root)

    # ---------- public ----------

    def resolve(self, proposal_id: str, decision: str, *,
                resolved_by: str = "user") -> GateResolution:
        if decision not in _GATE_DECISIONS:
            raise ValueError(f"unknown gate decision: {decision!r}")
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"proposal not found: {proposal_id}")
        existing = proposal["gate_decision"]
        if existing is not None:
            if existing == decision:
                # 已裁决的重放：outputs 按决策类型重推（幂等只读）
                outputs: tuple[str, ...] = ()
                if existing == "ADOPT":
                    outputs = (str(self.insight_root / "knowledge"
                                   / "confirmed"
                                   / f"cognition-{proposal_id}.md"),)
                elif existing == "EXPERIMENT":
                    outputs = (str(self.insight_root / "experiments" /
                                   f"experiment-{proposal_id}.md"),)
                return GateResolution(
                    proposal_id=proposal_id, decision=decision,
                    gate_status=proposal["gate_status"],
                    resolved_at=proposal["gate_resolved_at"],
                    resolved_by=proposal["gate_decision_source"] or "",
                    outputs=outputs,
                    already_resolved=True)
            raise GateConflictError(
                f"proposal {proposal_id} already resolved as "
                f"{existing}; refusing to overwrite with {decision!r}")

        status_map = {"ADOPT": "adopted", "EXPERIMENT": "experimented",
                      "WATCH": "watched", "ARCHIVE": "archived",
                      "REJECT": "rejected"}
        gate_status = status_map[decision]
        outputs = self._apply(proposal, decision, gate_status)
        self.store.resolve_gate(proposal_id, decision,
                                gate_status=gate_status,
                                resolved_by=resolved_by)
        return GateResolution(proposal_id=proposal_id, decision=decision,
                              gate_status=gate_status,
                              resolved_at=_now_iso(), resolved_by=resolved_by,
                              outputs=outputs)

    # ---------- decision application ----------

    def _apply(self, proposal, decision: str,
               gate_status: str) -> tuple[str, ...]:
        p = CognitionProposal(
            proposal_id=proposal["proposal_id"],
            card_id=proposal["card_id"],
            insight_source_id=proposal["insight_source_id"],
            source_revision=int(proposal["source_revision"]),
            context_pack_id=json.loads(
                proposal["proposal_json"]).get("context_pack_id", ""),
            source_item_id=str(json.loads(
                proposal["proposal_json"]).get("source_item_id", "")),
            change_type=proposal["change_type"],
            proposed_cognition=json.loads(
                proposal["proposal_json"]).get("proposed_cognition", ""),
            old_cognition=tuple(json.loads(proposal["proposal_json"]).get(
                "old_cognition", ())),
            reasons=tuple(json.loads(proposal["proposal_json"]).get(
                "reasons", ())),
            evidence_refs=tuple(json.loads(
                proposal["proposal_json"]).get("evidence_refs", ())),
            related_cognition_refs=tuple(json.loads(
                proposal["proposal_json"]).get("related_cognition_refs", ())),
            card_path=json.loads(
                proposal["proposal_json"]).get("card_path", ""),
            gate_status=gate_status, gate_decision=decision)
        if decision == "ADOPT":
            projection = self._write_confirmed_projection(p)
            self.store.upsert_approved_cognition(
                record_id=f"CG-{p.proposal_id}",
                proposal_id=p.proposal_id,
                cognition=p.proposed_cognition,
                domain=p.domain,
                approved_at=_now_iso(),
                approved_by="human_gate",
                provenance_json=json.dumps({
                    "source_item_id": p.source_item_id,
                    "source_revision": p.source_revision,
                    "context_pack_id": p.context_pack_id,
                    "card_id": p.card_id,
                    "card_path": p.card_path,
                    "evidence_refs": list(p.evidence_refs),
                    "related_cognition_refs": list(
                        p.related_cognition_refs),
                    "old_cognition": list(p.old_cognition),
                    "reasons": list(p.reasons),
                }, ensure_ascii=False))
            return (str(projection),)
        if decision == "EXPERIMENT":
            stub = self._write_experiment_stub(p)
            return (str(stub),)
        if decision == "WATCH":
            self.store.add_watch_signal(
                p.insight_source_id, p.source_revision,
                f"gate:{p.proposal_id}",
                reason=f"Human Gate WATCH: {p.proposed_cognition[:80]}")
            return ()
        return ()

    # ---------- projections ----------

    def _write_confirmed_projection(self, p: CognitionProposal) -> Path:
        directory = self.insight_root / "knowledge" / "confirmed"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"cognition-{p.proposal_id}.md"
        prov = p.provenance
        lines = [
            "---",
            f"proposal_id: {p.proposal_id}",
            f"change_type: {p.change_type}",
            f"source_revision: {p.source_revision}",
            f"source_item_id: {p.source_item_id}",
            f"context_pack_id: {p.context_pack_id}",
            f"card_id: {p.card_id}",
            "---",
            "",
            "## 采纳的认知（我的版本）",
            "",
            p.proposed_cognition,
            "",
            "## 旧认知",
            "",
        ]
        if p.old_cognition:
            for old in p.old_cognition:
                lines.append(f"- **{old.get('record_id')}**："
                             f"{old.get('text')}（关联：{old.get('connection')}）")
        else:
            lines.append("（ADD：全新认知，无旧判断。）")
        lines += ["", "## 形成原因", ""]
        for reason in p.reasons:
            lines.append(f"- {reason}")
        lines += ["", "## 证据与溯源", ""]
        for ref in p.evidence_refs:
            lines.append(f"- 证据：{ref}")
        for rid in p.related_cognition_refs:
            lines.append(f"- 关联认知：{rid}")
        lines += [
            f"- source_revision: {prov['source_revision']}",
            f"- context_pack_id: {prov['context_pack_id']}",
            f"- card: {prov['card_path']}",
            "",
        ]
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _write_experiment_stub(self, p: CognitionProposal) -> Path:
        directory = self.insight_root / "experiments"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"experiment-{p.proposal_id}.md"
        lines = [
            f"# Experiment: {p.proposed_cognition[:80]}",
            "",
            "## 待验证假设",
            "",
            p.proposed_cognition,
            "",
        ]
        if p.old_cognition:
            lines += ["## 被作用的旧认知", ""]
            for old in p.old_cognition:
                lines.append(f"- **{old.get('record_id')}**：{old.get('text')}")
            lines.append("")
        lines += [
            "## 来源",
            "",
            f"- proposal: {p.proposal_id}（change_type={p.change_type}）",
            f"- context_pack: {p.context_pack_id}",
            f"- card: {p.card_path}",
            "",
            "## 验证设计（待填写）",
            "",
            "- 假设：",
            "- 低成本验证动作：",
            "- 判停条件：",
            "- 结果记录：",
            "",
        ]
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, path)
        return path
