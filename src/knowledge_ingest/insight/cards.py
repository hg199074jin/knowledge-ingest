"""V3 M6: Deep Insight Card 渲染——元数据、自适应展示、原子写。

冻结语义（设计 §15）：
- 展示自适应：后台思考契约固定，前台不强制十节八股——无商业机会
  不渲染商业节；challenge 无实质内容不渲染质疑节；own_version 与
  cognition_delta 对 passed 卡必须可见（Stranger/三个月后测试的对象）；
- 路径：<insight_root>/cards/YYYY/MM/DD/insight-<id>-<slug>.md；
  card id 由 (source_item_id, source_revision, content_fingerprint)
  稳定推导 → 重写同路径（幂等）；
- 原子写（tmp + os.replace）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path

from .models import EvidencePack, InsightSourceView


def card_id_for(source_item_id: str, source_revision: int,
                content_fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"{source_item_id}\x00{source_revision}\x00"
        f"{content_fingerprint}".encode()).hexdigest()
    return digest[:12]


def slugify(title: str | None, fallback: str = "card") -> str:
    if not title:
        return fallback
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return ascii_part[:48] or fallback


def insight_card_path(insight_root: Path, view: InsightSourceView, *,
                      card_id: str, now: str) -> Path:
    date = _dt.datetime.fromisoformat(now)
    slug = slugify(view.title)
    return (Path(insight_root) / "cards" / f"{date.year:04d}"
            / f"{date.month:02d}" / f"{date.day:02d}"
            / f"insight-{card_id}-{slug}.md")


def render_card_markdown(view: InsightSourceView, pack: EvidencePack,
                         thought: dict, *, quality_status: str,
                         human_gate: str = "pending") -> str:
    """自适应渲染：后台契约已由 Thinker/Critic 保证，前台按内容取舍。"""
    delta = thought["cognition_delta"]
    actions = thought.get("actions", [])
    action_state = actions[0]["action"] if actions else "NONE"
    connections = thought.get("personal_connections", [])
    impacts = thought.get("project_impacts", [])
    biz = thought.get("business_opportunity")
    challenge = str(thought.get("challenge", "")).strip()
    related = [c["record_id"] for c in connections]

    lines = [
        "---",
        f"source: {view.provider}",
        f"source_item_id: {view.source_item_id}",
        f"topics: {json.dumps(_topics(pack), ensure_ascii=False)}",
        f"value_type: {json.dumps(_topics(pack), ensure_ascii=False)}",
        f"cognition_delta: {delta}",
        f"action_state: {action_state}",
        f"related_knowledge: {json.dumps(related, ensure_ascii=False)}",
        f"quality_status: {quality_status}",
        f"human_gate: {human_gate}",
        "---",
        "",
        f"# {view.title or thought['bottom_line']}",
        "",
        "## 结论",
        "",
        thought["bottom_line"],
        "",
        "## 原文真正说什么",
        "",
        thought["source_understanding"],
        "",
        "## 机制",
        "",
        thought["mechanism"],
        "",
    ]
    if challenge:
        lines += ["## 质疑与边界", "", challenge, ""]
    if connections:
        lines += ["## 与我过去认知的关系", ""]
        for conn in connections:
            reason = _reason_for(pack, conn.get("record_id"))
            lines += [f"- **{conn['record_id']}**：{conn['connection']}"
                      + (f"（旧认知：{reason}）" if reason else "")]
        lines.append("")
    if impacts:
        lines += ["## 对当前项目的影响", ""]
        for impact in impacts:
            project = impact.get("project", "")
            impact_text = impact.get("impact", "")
            lines += [f"- **{project}**：{impact_text}"]
        lines.append("")
    if biz:
        lines += ["## 商业机会", "",
                  f"- 市场机会：{biz.get('market_opportunity', '')}",
                  f"- 个人机会：{biz.get('personal_opportunity', '')}", ""]
    lines += [
        "## 我的版本",
        "",
        thought["own_version"],
        "",
        f"## 认知变化：{delta}",
        "",
    ]
    if actions:
        lines += ["## 行动", ""]
        for action in actions:
            lines.append(f"- [{action['action']}] {action['detail']}")
        lines.append("")
    lines += [f"Human Gate 建议：{thought['human_gate_recommendation']}", ""]
    return "\n".join(lines)


def _reason_for(pack: EvidencePack, record_id: str | None) -> str | None:
    for ref in pack.personal_context:
        if ref.record_id == record_id:
            return ref.text
    return None


def _topics(pack: EvidencePack) -> list[str]:
    """轻量主题标签：从证据变量提取（不完美，仅导航用）。"""
    topics = set()
    blob = " ".join(pack.source_claims + pack.source_inferences).casefold()
    for keyword, topic in (("agent", "ai"), ("jev", "ai"), ("token", "ai"),
                           ("审", "audit"), ("税", "audit"),
                           ("民宿", "minsu"), ("定价", "business"),
                           ("赚钱", "business"), ("获客", "business")):
        if keyword in blob:
            topics.add(topic)
    return sorted(topics)


class DeepInsightCardWriter:
    def __init__(self, insight_root: Path):
        self.insight_root = Path(insight_root)

    def write(self, *, view: InsightSourceView, pack: EvidencePack,
              thought: dict, quality_status: str, now: str,
              human_gate: str = "pending") -> Path:
        if quality_status not in ("passed", "needs_review"):
            raise ValueError(f"invalid quality_status: {quality_status!r}")
        card_id = card_id_for(view.source_item_id, view.source_revision,
                              view.content_fingerprint)
        path = insight_card_path(self.insight_root, view, card_id=card_id,
                                 now=now)
        markdown = render_card_markdown(view, pack, thought,
                                        quality_status=quality_status,
                                        human_gate=human_gate)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(markdown, encoding="utf-8")
        os.replace(tmp, path)
        return path
