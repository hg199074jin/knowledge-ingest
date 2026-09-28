"""V3 M8: Insight Digest——导航索引，非全文重写（设计 §21）。"""

from __future__ import annotations

from .store import InsightStore


def build_insight_digest(store: InsightStore) -> str:
    lines = ["# Insight Digest", ""]
    cards = store._conn.execute(
        "SELECT COUNT(*) FROM insight_cards").fetchone()[0]
    proposals = store._conn.execute(
        "SELECT COUNT(*) FROM cognition_proposals "
        "WHERE gate_status = 'pending'").fetchone()[0]
    adopted = store._conn.execute(
        "SELECT COUNT(*) FROM cognition_proposals "
        "WHERE gate_status = 'adopted'").fetchone()[0]
    watch = store._conn.execute(
        "SELECT COUNT(*) FROM watch_signals WHERE status = 'WATCH'"
    ).fetchone()[0]
    blocked = store._conn.execute(
        "SELECT COUNT(*) FROM insight_runs "
        "WHERE status = 'blocked'").fetchone()[0]
    lines.append(f"Cards generated: {cards}")
    lines.append(f"Pending proposals: {proposals}")
    lines.append(f"Adopted: {adopted}")
    lines.append(f"Watch signals: {watch}")
    lines.append(f"Blocked runs: {blocked}")
    lines.append("")
    if proposals:
        lines.append("## 等待 Human Gate")
        lines.append("")
        for row in store._conn.execute(
            "SELECT proposal_id, change_type, gate_status "
            "FROM cognition_proposals WHERE gate_status = 'pending' "
            "LIMIT 10"):
            lines.append(f"- {row['proposal_id']} ({row['change_type']})")
        lines.append("")
    return "\n".join(lines)
