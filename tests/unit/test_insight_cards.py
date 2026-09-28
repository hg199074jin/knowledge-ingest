"""V3 M6: Deep Insight Card 渲染——元数据、自适应展示、原子写、稳定 id。

冻结语义（设计 §15；实施方案 Task 6 Step 8）：
- 元数据：source / source_item_id / topics / value_type / cognition_delta /
  action_state / related_knowledge / quality_status / human_gate；
- 展示自适应：无商业机会不渲染商业节；challenge 无实质内容不渲染；
  own_version 与 cognition_delta 对 passed 卡必须可见；
- 原子写（tmp + os.replace）；同输入 → 同稳定 card id。
"""

from pathlib import Path

from knowledge_ingest.insight.cards import (
    card_id_for,
    insight_card_path,
    slugify,
)
from knowledge_ingest.insight.models import (
    EvidencePack,
    InsightSourceView,
    PersonalContextRef,
)

NOW = "2026-09-28T12:00:00+00:00"


def make_view(**overrides) -> InsightSourceView:
    base = {
        "provider": "telegram", "source_item_id": "tg_a:1",
        "content_kind": "text", "title": "Jev 判定模型生态",
        "visible_text": "body", "materialized_path": None,
        "full_text_available": True, "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


def make_thought(**overrides):
    thought = {
        "title": "Jev 范式映射为三级决策结构",
        "bottom_line": "判定模型以两个数量级的成本差承接 Agent 高频小决策，映射为本管道第三层",
        "source_understanding": "作者主张判定模型承接高频小决策",
        "mechanism": "成本差两个数量级",
        "challenge": "误判率数据缺失",
        "personal_connections": [
            {"record_id": "PC-001", "connection": "扩展两级架构为三级"}],
        "project_impacts": [
            {"project": "knowledge-ingest", "impact": "预算压力下降"}],
        "business_opportunity": None,
        "own_version": "规则→判定→生成三级结构，加层取决于调用量",
        "cognition_delta": "REVISE",
        "actions": [{"action": "EXPERIMENT",
                     "detail": "拿 50 条积压样本试判定层"}],
        "human_gate_recommendation": "EXPERIMENT",
        "final_verdict": "CARD",
    }
    thought.update(overrides)
    return thought


def make_pack(**overrides) -> EvidencePack:
    base = {
        "personal_context": (PersonalContextRef(
            record_id="PC-001", relation_reason="r", state="CONFIRMED",
            kind="cognition", text="两级筛选架构"),)}
    base.update(overrides)
    return EvidencePack(**base)


def write_card(tmp_path: Path, view=None, thought=None, *,
               quality_status="passed", pack=None,
               context_pack_id=None):
    from knowledge_ingest.insight.cards import DeepInsightCardWriter
    if thought is None:
        thought = make_thought(
            **({"cognition_delta": "NONE"} if
               quality_status == "needs_review" else {}))
    writer = DeepInsightCardWriter(tmp_path / "insight")
    return writer.write(
        view=view or make_view(), pack=pack or make_pack(),
        thought=thought, quality_status=quality_status,
        now="2026-09-28T12:00:00+00:00",
        context_pack_id=context_pack_id)


# ---------- id / path ----------

def test_card_id_stable_and_distinct():
    a = card_id_for("tg_a:1", 1, "fp-1")
    b = card_id_for("tg_a:1", 1, "fp-1")
    c = card_id_for("tg_a:1", 2, "fp-2")
    assert a == b and a != c
    assert len(a) == 12


def test_insight_card_path_uses_date_and_slug(tmp_path):
    view = make_view()
    path = insight_card_path(tmp_path / "insight", view,
                             card_id="abc123def456",
                             now="2026-09-28T12:00:00+00:00")
    # 中文标题 slugify 只保留 ASCII 部分（不引入拼音依赖）
    assert path == (tmp_path / "insight" / "cards" / "2026" / "09" / "28" /
                    "insight-abc123def456-jev.md")


def test_slugify_strips_non_ascii_to_fallback():
    assert slugify("!!!") == "card"
    assert "jev" in slugify("Jev 判定模型 生态")


# ---------- rendering ----------

def test_frontmatter_metadata_complete(tmp_path):
    path = write_card(tmp_path)
    text = path.read_text(encoding="utf-8")
    for key in ("source: telegram", "source_item_id: tg_a:1",
                "cognition_delta: REVISE", "action_state: EXPERIMENT",
                "quality_status: passed", "human_gate: pending"):
        assert key in text, key
    assert "related_knowledge" in text
    assert "PC-001" in text


def test_h1_is_title_conclusion_is_bottom_line(tmp_path):
    """M6 复核⑤：bottom_line 不得同时充当 H1 与结论正文。"""
    path = write_card(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "# Jev 范式映射为三级决策结构" in text
    assert "判定模型以两个数量级的成本差承接" in text
    title_line = "# Jev 范式映射为三级决策结构"
    assert text.count(title_line) == 1


def test_frontmatter_carries_context_pack_id(tmp_path):
    path = write_card(tmp_path, context_pack_id="ctxpack.abc123def456")
    text = path.read_text(encoding="utf-8")
    assert "context_pack_id: ctxpack.abc123def456" in text


def test_own_version_and_delta_always_visible_for_passed(tmp_path):
    path = write_card(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "规则→判定→生成三级结构" in text
    assert "REVISE" in text


def test_adaptive_no_business_section_when_null(tmp_path):
    path = write_card(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "商业机会" not in text


def test_business_section_rendered_when_present(tmp_path):
    thought = make_thought(business_opportunity={
        "market_opportunity": "判定层工具需求真实",
        "personal_opportunity": "有现成管道做试验场"})
    path = write_card(tmp_path, thought=thought)
    text = path.read_text(encoding="utf-8")
    assert "商业机会" in text
    assert "判定层工具需求真实" in text
    assert "有现成管道做试验场" in text


def test_challenge_section_skipped_when_empty(tmp_path):
    path = write_card(tmp_path, thought=make_thought(challenge=""))
    text = path.read_text(encoding="utf-8")
    assert "质疑与边界" not in text


# ---------- atomic / idempotent ----------

def test_write_is_atomic_no_tmp_left(tmp_path):
    path = write_card(tmp_path)
    assert path.is_file()
    leftovers = list(path.parent.glob("*.tmp*"))
    assert leftovers == []


def test_rewrite_same_inputs_same_path(tmp_path):
    p1 = write_card(tmp_path)
    p2 = write_card(tmp_path)
    assert p1 == p2


# ---------- needs_review ----------

def test_needs_review_card_marked(tmp_path):
    path = write_card(tmp_path, quality_status="needs_review")
    text = path.read_text(encoding="utf-8")
    assert "quality_status: needs_review" in text
