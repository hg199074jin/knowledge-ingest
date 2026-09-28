"""V3 M6: Evidence Extract——第 7 个模型 stage（Review §6 冻结）。

TEXT 一个 part → 一次结构化 extraction；corpus 逐 part extraction 后
确定性聚合/去重。payload 携带 part 原文全文（不得静默截断）；
聚合按精确文本去重（跨 part）。个人上下文只透传，绝不混入源三类。
模型坏输出 → EvidenceExtractionError（可重试），绝不伪造 Pack。
"""

from __future__ import annotations

from .model_port import ModelPortError
from .models import EvidencePack, InsightSourceView

REQUIRED_KEYS = ("claims", "evidence", "inferences", "unknowns",
                 "verification_flags")


class EvidenceExtractionError(ModelPortError):
    """evidence_extract 失败（可恢复重试；绝不以摘要冒充 Pack）。"""


def _bad(message: str) -> EvidenceExtractionError:
    return EvidenceExtractionError(f"evidence_extract: {message}")


def _parse_part(raw, part_id: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise _bad(f"part {part_id}: output is not an object")
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise _bad(f"part {part_id}: missing keys {missing}")
    parsed = {}
    for key in REQUIRED_KEYS:
        value = raw[key]
        if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value):
            raise _bad(f"part {part_id}: {key} must be a list of strings")
        parsed[key] = value
    return parsed


def build_evidence_pack(model_port, view: InsightSourceView, content,
                        context_refs=()) -> EvidencePack:
    """逐 part extraction → 聚合（跨 part 按原文判重）→ 六类分离 Pack。

    claims 保留 `part_id: ` 前缀以溯源到具体 batch；其余四类裸文本。
    """
    if content is None or not content.parts:
        raise _bad("no readable content parts (should be WAITING_CONTENT)")
    buckets = {"claims": [], "evidence": [], "inferences": [],
               "unknowns": [], "verification_flags": []}
    seen: dict[str, set[str]] = {k: set() for k in buckets}
    for part in content.parts:
        if not part.text.strip():
            raise _bad(f"part {part.part_id} has empty text")
        raw = model_port.run("evidence_extract", {
            "instruction": (
                "把该文本片段拆成四类：作者的主张(claims)、作者给出的"
                "证据(evidence)、作者的推断(inferences)、缺失或未验证的"
                "关键变量(unknowns)。同时列出需要外部核验的标志"
                "(verification_flags，如 VERIFY_REQUIRED:xxx)，没有则空。"
                "区分事实与观点；不要添加文本中没有的内容。"),
            "output_contract": (
                '只输出一个 JSON 对象：{"claims": ["..."], '
                '"evidence": ["..."], "inferences": ["..."], '
                '"unknowns": ["..."], "verification_flags": ["..."]}。'
                "五个键必须存在，值都是字符串数组（可以为空数组）。"
                "不要输出 JSON 以外的任何文字。"),
            "part_id": part.part_id,
            "source_ref": part.source_ref,
            "text": part.text,          # 全文入参，绝不截断
        })
        parsed = _parse_part(raw, part.part_id)
        for key, items in parsed.items():
            for item in items:
                dedupe_key = item.strip().casefold()
                if not dedupe_key or dedupe_key in seen[key]:
                    continue
                seen[key].add(dedupe_key)
                if key == "claims":     # 主张溯源到具体 batch
                    buckets[key].append(f"{part.part_id}: "
                                        f"{item.strip()}")
                else:
                    buckets[key].append(item.strip())
    return EvidencePack(
        source_claims=tuple(buckets["claims"]),
        source_evidence=tuple(buckets["evidence"]),
        source_inferences=tuple(buckets["inferences"]),
        unknown_variables=tuple(buckets["unknowns"]),
        personal_context=tuple(context_refs),
        verification_flags=tuple(buckets["verification_flags"]),
        source_parts=tuple(content.parts))
