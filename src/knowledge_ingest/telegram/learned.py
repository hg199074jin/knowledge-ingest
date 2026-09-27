"""V2.2: 学习规则层——把高置信 SKIP 判决蒸馏为规则，0 模型调用拦重复广告。

纪律（§10"规则优先、0 次调用"精神的延伸）：
- 蒸馏只收 SKIP（拦已知广告），永不蒸馏 KEEP/INCLUDE——
  误放一条广告进知识库的代价远大于多问一次模型或人工；
- 指纹 = 规范化文本的 sha256，只做"见过就认得"，不做泛化；
  emoji/变体符号/空白/标点归一化，跨频道一字不差的复读直接命中；
- 规则只对短内容生效（与静态规则同门槛，KNOWLEDGE_BODY_FLOOR_CHARS），
  长正文按 §10.3 走疑似/LLM 路径；
- 规则必须显式激活（rules-distill --apply），审计 reason_code 带
  learned: 前缀可回溯；rules-disable 可逐条停用。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

# \w 已含 unicode 字母数字；补 CJK 基本区与扩展 A。
# 其余（emoji/标点/空白/全半角符号）全部剔除后 casefold，
# 使排版障眼法失效：😂😂老品牌，值得信赖 → 老品牌值得信赖
_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff\u3400-\u4dbf]+", re.UNICODE)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    # NFKC 先折叠全角→半角（ＵＳＤＴ→USDT），再剔除障眼法符号
    return _STRIP_RE.sub(
        "", unicodedata.normalize("NFKC", text)).casefold()


def fingerprint(text: str | None) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LearnedRule:
    rule_id: int
    rule_type: str        # 'fingerprint' | 'keyword'
    pattern: str          # 指纹 hex，或（规范化的）关键词字面量
    reason_code: str      # 蒸馏来源的原始 reason_code
    decision: str         # 目前仅 'SKIP'
    source: str           # 'llm' | 'human'
    confidence: float
    active: int

    @classmethod
    def from_row(cls, row) -> LearnedRule:
        return cls(rule_id=int(row["rule_id"]),
                   rule_type=row["rule_type"],
                   pattern=row["pattern"],
                   reason_code=row["reason_code"],
                   decision=row["decision"],
                   source=row["source"],
                   confidence=float(row["confidence"]),
                   active=int(row["active"]))


class LearnedRuleIndex:
    """进程内活跃学习规则索引；classify 层短路匹配用。

    fingerprint 走规范化后精确命中；keyword 在规范化文本上做
    字面子串匹配（pattern 入库前须先 normalize_text）。
    """

    def __init__(self, rules=()):
        self._fingerprints = {
            rule.pattern: rule for rule in rules
            if rule.active and rule.rule_type == "fingerprint"}
        self._keywords = tuple(
            rule for rule in rules
            if rule.active and rule.rule_type == "keyword")

    @classmethod
    def load(cls, store) -> LearnedRuleIndex:
        return cls(LearnedRule.from_row(row)
                   for row in store.list_learned_rules(active_only=True))

    def __len__(self) -> int:
        return len(self._fingerprints) + len(self._keywords)

    def match(self, text: str | None) -> LearnedRule | None:
        if not text:
            return None
        rule = self._fingerprints.get(fingerprint(text))
        if rule is not None:
            return rule
        normalized = normalize_text(text)
        for rule in self._keywords:
            if rule.pattern and rule.pattern in normalized:
                return rule
        return None


# ---- 蒸馏：从审计与人工裁决收集指纹候选 ----

def collect_skip_candidates(store, pipeline, *,
                            min_confidence: float = 0.9) -> list[dict]:
    """汇总 LLM 审计与已决人工 review 中的 SKIP 判决为候选规则。

    返回按指纹聚合的候选列表（新出现在审计/review 中的证据），
    每项含 fingerprint / reason_code / source('llm'|'human') /
    confidence / evidence(item_ids) / preview。长内容与已入库
    指纹不产生候选（与规则层同门槛，去重）。
    """
    from .classify import KNOWLEDGE_BODY_FLOOR_CHARS

    existing = {
        (row["rule_type"], row["pattern"])
        for row in store.list_learned_rules(active_only=False)}

    def item_text(item_id: str, kind: str) -> str:
        # 审计 kind 是 noise/interest；review kind 是 item 类型
        # （text/pdf/video/file）——text 与 noise 同义：正文即判定对象
        if kind in ("noise", "text"):
            return pipeline.live_text(item_id) or ""
        message = store.get_message_for_item(item_id)
        if message is None:
            return ""
        return "\n".join(
            part for part in (message["text"],
                              message["document_name"]) if part)

    candidates: dict[str, dict] = {}
    for row in store.list_latest_skip_audits(min_confidence):
        raw = item_text(row["item_id"], row["kind"])
        if not raw or len(raw) >= KNOWLEDGE_BODY_FLOOR_CHARS:
            continue
        fp = fingerprint(raw)
        if (not normalize_text(raw)) or ("fingerprint", fp) in existing:
            continue
        cand = candidates.setdefault(fp, {
            "fingerprint": fp,
            "reason_code": row["reason_code"] or "unknown",
            "source": "llm",
            "confidence": float(row["confidence"] or 0.0),
            "evidence": [],
            "preview": raw[:48],
        })
        cand["evidence"].append(row["item_id"])
        cand["confidence"] = max(cand["confidence"],
                                 float(row["confidence"] or 0.0))

    for row in store.list_items_with_resolved_skip():
        raw = item_text(row["item_id"], row["kind"] or "noise")
        if not raw or len(raw) >= KNOWLEDGE_BODY_FLOOR_CHARS:
            continue
        fp = fingerprint(raw)
        if (not normalize_text(raw)) or ("fingerprint", fp) in existing:
            continue
        cand = candidates.setdefault(fp, {
            "fingerprint": fp,
            "reason_code": "human_skip",
            "source": "human",
            "confidence": 1.0,
            "evidence": [],
            "preview": raw[:48],
        })
        # 人工裁决置信最高：覆盖来源与理由
        cand["source"] = "human"
        cand["reason_code"] = "human_skip"
        cand["confidence"] = 1.0
        cand["evidence"].append(row["item_id"])

    return sorted(candidates.values(),
                  key=lambda c: (-len(c["evidence"]), c["fingerprint"]))
