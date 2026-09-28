"""V3 M6: Evidence Extract——第 7 个模型 stage 的聚合契约。

冻结语义（Review §6）：
- TEXT 一个 part → 一次结构化 extraction；PDF/video 逐 part extraction
  后确定性聚合/去重——不得无上限拼接、不得静默截断（payload 文本
  = part 原文全文）；
- Evidence Pack 六类分离：claims / evidence / inferences / unknowns /
  personal_context / verification_flags——个人主张绝不混入源证据；
- 模型坏输出 → EvidenceExtractionError（可重试），绝不伪造 Pack；
- part 原文随 pack.source_parts 保留（供 Thinking 全文引用）。
"""

import pytest

from knowledge_ingest.insight.content_resolver import ContentPart, ResolvedContent
from knowledge_ingest.insight.evidence import (
    EvidenceExtractionError,
    build_evidence_pack,
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
        "content_kind": "text", "title": "t", "visible_text": "body",
        "materialized_path": None, "full_text_available": True,
        "verification_status": "source_only",
        "content_fingerprint": "fp-1", "captured_at": NOW}
    base.update(overrides)
    return InsightSourceView(**base)


def make_content(*texts) -> ResolvedContent:
    return ResolvedContent(
        parts=tuple(ContentPart(part_id=f"B{i:04d}", text=t,
                                source_ref=f"/x/B{i:04d}.md")
                    for i, t in enumerate(texts, 1)),
        verification_status="source_only")


EXTRACT = ("import sys, json\n"
           "req = json.load(sys.stdin)\n"
           "print(json.dumps({'claims': [req['part_id'] + ' 主张'],\n"
           "                  'evidence': [req['part_id'] + ' 证据'],\n"
           "                  'inferences': [],\n"
           "                  'unknowns': ['市场规模未验证'],\n"
           "                  'verification_flags': []}))\n")


class FakePort:
    def __init__(self, outputs, fail_times=0):
        self.outputs = list(outputs)
        self.fail_times = fail_times
        self.calls = []

    def run(self, stage, payload):
        self.calls.append((stage, payload))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise AssertionError("forced model failure")
        return self.outputs.pop(0)


def build(script_output_parts, view=None, content=None, refs=(), **kw):
    port = FakePort(script_output_parts, **kw)
    view = view or make_view()
    content = content or make_content("唯一 part 文本")
    return build_evidence_pack(port, view, content, refs), port


def part_output(pid, **overrides):
    out = {"claims": [f"{pid} 主张"], "evidence": [f"{pid} 证据"],
           "inferences": [], "unknowns": [], "verification_flags": []}
    out.update(overrides)
    return out


# ---------- TEXT: 一个 part 一次 extraction ----------

def test_single_part_single_call_and_pack_shape():
    pack, port = build([part_output("B0001", unknowns=["市场规模未验证"])])
    assert len(port.calls) == 1
    stage, payload = port.calls[0]
    assert stage == "evidence_extract"
    assert payload["part_id"] == "B0001"
    assert isinstance(pack, EvidencePack)
    assert pack.source_claims == ("B0001: B0001 主张",)
    assert pack.source_evidence == ("B0001 证据",)
    assert pack.unknown_variables == ("市场规模未验证",)


def test_no_silent_truncation_payload_carries_full_part_text():
    long_text = "长" * 5000
    pack, port = build([part_output("main")],
                       content=make_content(long_text))
    _, payload = port.calls[0]
    assert payload["text"] == long_text          # 全文入参，无截断
    assert len(pack.source_parts[0].text) == 5000


# ---------- PDF/video: 逐 part + 确定性聚合去重 ----------

def test_multi_part_per_part_calls_with_dedup(tmp_path=None):
    outputs = [part_output("B0001"), part_output("B0002")]
    pack, port = build(outputs, content=make_content("批一", "批二"))
    assert len(port.calls) == 2                  # 逐 part，不是一次拼接
    # claims 带 part 前缀：可溯源到具体 batch
    assert pack.source_claims == ("B0001: B0001 主张",
                                  "B0002: B0002 主张")


def test_exact_duplicate_claims_deduped_across_parts():
    outputs = [part_output("B0001", claims=["同一主张", "独有主张"]),
               part_output("B0002", claims=["同一主张"])]
    pack, _ = build(outputs, content=make_content("一", "二"))
    assert pack.source_claims == ("B0001: 同一主张",
                                  "B0001: 独有主张")


# ---------- 六类分离 / personal_context 透传 ----------

def test_personal_context_passthrough_never_mixes_into_source():
    refs = (PersonalContextRef(record_id="PC-001",
                               relation_reason="改变判定层边界",
                               state="CONFIRMED"),)
    pack, _ = build([part_output("main")], refs=refs)
    assert pack.personal_context == refs
    assert all("PC-001" not in c for c in pack.source_claims)
    assert all("PC-001" not in e for e in pack.source_evidence)


def test_verification_flags_preserved():
    pack, _ = build([part_output("main",
                                 verification_flags=["VERIFY_REQUIRED:数据来源"])])
    assert pack.verification_flags == ("VERIFY_REQUIRED:数据来源",)


# ---------- fail-closed ----------

def test_missing_category_key_raises_retryable():
    pack_exc = pytest.raises(EvidenceExtractionError)
    with pack_exc:
        build([{"claims": ["x"]}])               # 缺其余四类


def test_model_failure_propagates_no_pack():
    port = FakePort([], fail_times=1)
    with pytest.raises(AssertionError):
        build_evidence_pack(port, make_view(), make_content("x"))


def test_none_content_raises():
    with pytest.raises(EvidenceExtractionError):
        build_evidence_pack(FakePort([]), make_view(), None)


def test_empty_text_part_raises():
    with pytest.raises(EvidenceExtractionError):
        build_evidence_pack(FakePort([]), make_view(), make_content(""))
