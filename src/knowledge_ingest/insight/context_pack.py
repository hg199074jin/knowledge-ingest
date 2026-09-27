"""V3 M5: render_context_pack——导航化证据包渲染（设计 §11）。

冻结语义：
- 分节：Confirmed Cognition / Active-Tentative Cognition /
  Active Decisions-Projects / Discarded-Historical（仅有内容时出现）/
  Experiment History / Conflicts；
- 空节绝不渲染假内容；全部为空 → 单个 NO_RELEVANT_PERSONAL_CONTEXT；
- 每条必须带"为什么相关"（relation_reason）与 source_ref 可追溯。
分节逻辑按 kind + state 判定，与检索器解耦。
"""

from __future__ import annotations

NO_RELEVANT_PERSONAL_CONTEXT = "NO_RELEVANT_PERSONAL_CONTEXT"


def _section(refs, header: str) -> list[str]:
    if not refs:
        return []
    lines = [f"## {header}", ""]
    for ref in refs:
        lines.append(f"### {ref.record_id}（{ref.state}）")
        if ref.text:
            lines.append(f"内容：{ref.text}")
        lines.append(f"为什么相关：{ref.relation_reason}")
        if ref.source_ref:
            lines.append(f"来源：{ref.source_ref}")
        lines.append("")
    return lines


def render_context_pack(refs) -> str:
    refs = list(refs)
    if not refs:
        return (f"# Personal Context Pack\n\n"
                f"{NO_RELEVANT_PERSONAL_CONTEXT}\n"
                f"\n（检索未发现会影响本次判断的个人证据。这可能意味着"
                f"真正的新认知分支——禁止硬关联。）\n")

    def is_discarded(ref) -> bool:
        return (ref.kind == "discarded"
                or ref.state in ("SUPERSEDED", "REJECTED", "ARCHIVED"))

    confirmed = [r for r in refs if r.kind == "cognition"
                 and r.state == "CONFIRMED"]
    active = [r for r in refs if r.kind == "cognition"
              and r.state in ("TENTATIVE", "EXPERIMENTING")]
    decisions = [r for r in refs if r.kind in ("decision", "project")]
    experiments = [r for r in refs if r.kind == "experiment"]
    discarded = [r for r in refs if is_discarded(r)]
    conflicts = [r for r in refs if r.conflict_with]

    lines = ["# Personal Context Pack", ""]
    lines += _section(confirmed, "Confirmed Cognition")
    lines += _section(active, "Active / Tentative Cognition")
    lines += _section(decisions, "Active Decisions / Projects")
    lines += _section(discarded, "Discarded / Historical Context")
    lines += _section(experiments, "Experiment History")

    if conflicts:
        lines += ["## Conflicts", "",
                  "PERSONAL_CONTEXT_CONFLICT —— 以下旧认知相互冲突，",
                  "由主思考模型判断适用边界，不做静默取舍。", ""]
        for ref in conflicts:
            lines.append(f"- {ref.record_id} ↔ "
                         f"{', '.join(ref.conflict_with)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
