"""V3 M5: render_context_pack — 导航化证据包渲染。

冻结语义（实施方案 Task 5 Step 6）：
- 分节：Confirmed Cognition / Active-Tentative Cognition /
  Active Decisions & Projects / Discarded-Historical（仅按请求时出现）/
  Experiment History / Conflicts；
- 空节绝不渲染假内容；全部为空时输出单个
  NO_RELEVANT_PERSONAL_CONTEXT 标记；
- 每条必须带"为什么相关"（relation_reason）。
"""

from knowledge_ingest.insight.context_pack import render_context_pack
from knowledge_ingest.insight.models import PersonalContextRef


def ref(record_id="PC-001", kind="cognition", state="CONFIRMED",
        text="认知内容", relation_reason="影响判断的理由",
        conflict_with=()):
    return PersonalContextRef(
        record_id=record_id, relation_reason=relation_reason, state=state,
        source_ref=f"src://{record_id}", kind=kind, text=text,
        conflict_with=tuple(conflict_with))


def test_empty_refs_render_single_marker():
    pack = render_context_pack(())
    assert "NO_RELEVANT_PERSONAL_CONTEXT" in pack
    assert "Confirmed Cognition" not in pack


def test_confirmed_cognition_section():
    pack = render_context_pack((ref(),))
    assert "## Confirmed Cognition" in pack
    assert "PC-001" in pack
    assert "认知内容" in pack
    assert "影响判断的理由" in pack


def test_tentative_cognition_in_active_section():
    pack = render_context_pack((ref(state="TENTATIVE"),))
    assert "## Active / Tentative Cognition" in pack
    assert "## Confirmed Cognition" not in pack


def test_decisions_and_projects_sections():
    pack = render_context_pack((
        ref(record_id="DEC-1", kind="decision", text="已决定走低成本路径"),
        ref(record_id="PROJ-1", kind="project", text="knowledge-ingest")))
    assert "## Active Decisions / Projects" in pack
    assert "已决定走低成本路径" in pack
    assert "knowledge-ingest" in pack


def test_discarded_section_only_when_present():
    pack = render_context_pack((ref(record_id="DIS-1", kind="discarded",
                                    state="ARCHIVED", text="已放弃方向X"),))
    assert "## Discarded / Historical Context" in pack
    assert "已放弃方向X" in pack
    # 无 discarded 时不渲染空节
    plain = render_context_pack((ref(),))
    assert "## Discarded / Historical Context" not in plain


def test_experiment_history_section():
    pack = render_context_pack((ref(record_id="EXP-1", kind="experiment",
                                    state="EXPERIMENTING", text="实验中"),))
    assert "## Experiment History" in pack


def test_conflicts_section_renders_pairs():
    pack = render_context_pack((
        ref(record_id="PC-001", text="要轻量判定层",
            conflict_with=("PC-002",)),
        ref(record_id="PC-002", text="只要生成模型",
            conflict_with=("PC-001",))))
    assert "## Conflicts" in pack
    assert "PC-001" in pack and "PC-002" in pack
    assert "PERSONAL_CONTEXT_CONFLICT" in pack


def test_every_entry_shows_why_relevant():
    pack = render_context_pack((ref(relation_reason="因为它改变判定层边界"),))
    assert "因为它改变判定层边界" in pack


def test_source_ref_traceability():
    pack = render_context_pack((ref(),))
    assert "src://PC-001" in pack
