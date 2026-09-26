"""V2.2: 学习规则层（learned_rules）单元测试。

覆盖：规范化/指纹、索引匹配、classify 短路（先于白名单）、
蒸馏候选收集（长内容门槛 + 人工采信 + 幂等）、schema v3。
"""

import json
from pathlib import Path

from knowledge_ingest.telegram.classify import (
    classify_interest,
    classify_noise,
)
from knowledge_ingest.telegram.event_store import (
    SCHEMA_VERSION,
    TelegramEventStore,
)
from knowledge_ingest.telegram.learned import (
    LearnedRule,
    LearnedRuleIndex,
    collect_skip_candidates,
    fingerprint,
    normalize_text,
)

T0 = "2026-09-18T09:00:00+08:00"


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


def rule(**overrides):
    base = {
        "rule_id": 1, "rule_type": "fingerprint",
        "pattern": fingerprint("老品牌值得信赖"),
        "reason_code": "AD_SPAM_GAMBLING", "decision": "SKIP",
        "source": "llm", "confidence": 0.99, "active": 1,
    }
    base.update(overrides)
    return LearnedRule(**base)


# ---------- 规范化与指纹 ----------

def test_normalize_strips_obfuscation():
    noisy = "😂😂 老 品 牌 ，值得信赖！→ ➡️ #球速体育"
    assert normalize_text(noisy) == normalize_text("老品牌值得信赖球速体育")
    assert normalize_text(None) == ""
    # 全半角与大小写归一
    assert normalize_text("ＵＳＤＴ平台 USDT平台") == \
        normalize_text("usdt平台usdt平台")


def test_fingerprint_stable_across_cosmetic_repost():
    """跨频道复读（一字不差 + 排版微调）→ 同一指纹。"""
    a = ("😂😂😂😂 😂😂😂😂 \n 老品牌，值得信赖、相信品牌的力量\n"
         "➡️ 豪礼大放送、高端嫩模、劳力士手表、奔驰E300 加入 #球速体育 "
         " 等你领取")
    b = ("😂😂😂😂😂😂😂😂  老品牌，值得信赖、相信品牌的力量\n"
         "➡️ 豪礼大放送、高端嫩模、劳力士手表、奔驰E300 加入 #球速体育 "
         "等你领取")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(a + " 微信 xx99 拿货")


# ---------- 索引匹配 ----------

def test_index_matches_fingerprint_and_ignores_inactive():
    active = rule()
    inactive = rule(rule_id=2, pattern=fingerprint("另一个广告"), active=0)
    index = LearnedRuleIndex([active, inactive])
    assert len(index) == 1
    assert index.match("😂😂 老品牌，值得信赖").rule_id == 1
    assert index.match("另一个广告") is None
    assert index.match("") is None
    assert index.match(None) is None


def test_index_keyword_matches_normalized_text():
    kw = rule(rule_id=3, rule_type="keyword",
              pattern=normalize_text("球速体育官网"))
    index = LearnedRuleIndex([kw])
    assert index.match("🌐 球速体育官 网 入口").rule_id == 3


# ---------- classify 短路 ----------

def test_learned_skip_beats_whitelist_with_zero_llm_calls():
    """关键回归：绕过疑似信号的障眼法广告，若无学习规则会掉进
    whitelist KEEP——短路必须先于白名单，且 0 次模型调用。"""
    ad = ("😂😂😂😂 老品牌，值得信赖、相信品牌的力量 ➡️ 豪礼大放送、"
          "高端嫩模 加入 球速体育 等你领取 电子可拉2万一注")
    assert "领取" in ad              # 确认它本是疑似路径样本
    index = LearnedRuleIndex([rule(pattern=fingerprint(ad))])
    llm = object()                   # 若被调用即炸（无 __call__）
    decision = classify_noise(ad, llm=llm, learned=index)
    assert decision.decision == "SKIP"
    assert decision.reason_code == "learned:AD_SPAM_GAMBLING"
    assert decision.confidence == 0.99


def test_learned_miss_falls_through_to_normal_flow():
    index = LearnedRuleIndex([rule()])
    decision = classify_noise("普通知识文本，没有任何信号", llm=None,
                              learned=index)
    assert decision.decision == "KEEP"
    assert decision.reason_code == "whitelist_default"


def test_learned_interest_exclude_with_zero_llm_calls():
    ad = "新用户注册送38🧧 8G国际娱乐 华人最稳首选"
    index = LearnedRuleIndex([rule(pattern=fingerprint(ad),
                                   reason_code="AD_SPAM_GAMBLING")])
    llm = object()
    decision = classify_interest(
        {"caption": ad, "filename": "promo.mp4", "size_bytes": 1,
         "source_display_name": "x"}, llm=llm, learned=index)
    assert decision.decision == "EXCLUDE"
    assert decision.reason == "learned:AD_SPAM_GAMBLING"


# ---------- schema v3 与 store CRUD ----------

def test_schema_v3_and_unique_fingerprint(tmp_path):
    store = make_store(tmp_path)
    assert store.user_version() == SCHEMA_VERSION == 3
    first = store.create_learned_rule(
        "fingerprint", "abc123", reason_code="ADVERTISEMENT",
        source="llm", confidence=0.95, evidence=["i1"])
    assert first is not None
    dup = store.create_learned_rule(
        "fingerprint", "abc123", reason_code="ADVERTISEMENT",
        source="llm", confidence=0.95)
    assert dup is None                      # 幂等：同指纹不重复入库
    rules = store.list_learned_rules()
    assert len(rules) == 1
    assert json.loads(rules[0]["evidence_json"]) == ["i1"]
    store.set_learned_rule_active(first, False)
    assert store.list_learned_rules(active_only=True) == []
    assert len(store.list_learned_rules()) == 1


def test_learned_rule_from_row_roundtrip():
    row = {"rule_id": "7", "rule_type": "keyword", "pattern": "abc",
           "reason_code": "X", "decision": "SKIP", "source": "human",
           "confidence": "1.0", "active": "1"}
    parsed = LearnedRule.from_row(row)
    assert parsed.rule_id == 7 and parsed.confidence == 1.0


# ---------- 蒸馏候选收集 ----------

class _FakePipeline:
    """live_text 桩：distill 只依赖这一个 pipeline 方法。"""

    def __init__(self, texts):
        self._texts = texts

    def live_text(self, item_id):
        return self._texts.get(item_id)


def test_collect_candidates_mines_llm_and_human(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="s", chat_id=-100999, start_at=T0)
    ad = "😂😂 老品牌，值得信赖 ➡️ 球速体育 等你领取"
    long_body = "真知识" * 200 + " 领取"
    for item_id in ("t:1", "t:2", "t:3", "t:4"):
        store.create_source_item(item_id, "s", "text", [1])
    store.create_classifier_audit(
        "t:1", "noise", "SKIP", reason_code="AD_SPAM_GAMBLING",
        confidence=0.99, policy_version="tg-v2.1",
        classifier_version="rules+learned-1")
    store.create_classifier_audit(
        "t:2", "noise", "SKIP", reason_code="ADVERTISEMENT",
        confidence=0.95, policy_version="tg-v2.1",
        classifier_version="rules+learned-1")
    store.create_classifier_audit(
        "t:3", "noise", "SKIP", reason_code="ADVERTISEMENT",
        confidence=0.97, policy_version="tg-v2.1",
        classifier_version="rules+learned-1")
    store.create_classifier_audit(
        "t:4", "noise", "KEEP", reason_code="whitelist_default",
        confidence=1.0, policy_version="tg-v2.1",
        classifier_version="rules+learned-1")
    store.create_review("t:2", "noise_uncertain", kind="text")
    store.resolve_review(1, "SKIP")
    texts = {"t:1": ad, "t:2": ad + "（同文案复读）", "t:3": long_body,
             "t:4": "普通内容"}
    pipeline = _FakePipeline(texts)

    candidates = collect_skip_candidates(store, pipeline,
                                         min_confidence=0.9)
    fingerprints = {c["fingerprint"] for c in candidates}
    assert fingerprint(ad) in fingerprints           # t:1 蒸馏候选
    assert fingerprint(long_body) not in fingerprints  # 长内容门槛
    human = [c for c in candidates if c["source"] == "human"]
    assert len(human) == 1 and human[0]["confidence"] == 1.0

    # 应用后幂等：已入库指纹不再产生候选
    for cand in candidates:
        store.create_learned_rule(
            "fingerprint", cand["fingerprint"],
            reason_code=cand["reason_code"], source=cand["source"],
            confidence=cand["confidence"], evidence=cand["evidence"])
    assert collect_skip_candidates(store, pipeline,
                                   min_confidence=0.9) == []
