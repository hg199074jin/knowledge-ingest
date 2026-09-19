"""TG5: 规则优先分类层 + 可注入 LLM（冻结设计 §10/§11/§21.1；方案 §7）。

纪律：
- 普通白名单 TEXT 与明确规则广告 = 0 次模型调用；
- LLM 只接"疑似广告/知识混合"与 PDF Interest，必须可注入
  （不写死任何 provider/SDK）；
- 结构化输出契约（§21）：解析失败 / 未知 enum / 调用失败 /
  预算拒绝 → 一律 REVIEW，绝不因省成本静默 SKIP 知识；
- 兴趣清单唯一规范来源 = 冻结设计 §11.2/§11.3（正例保护随
  prompt 下发）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import uuid4

POLICY_VERSION = "tg-v2.1"
CLASSIFIER_VERSION = "rules+injectable-llm-1"

NOISE_DECISIONS = ("KEEP", "SKIP", "REVIEW")
INTEREST_DECISIONS = ("INCLUDE", "EXCLUDE", "REVIEW")

# ---- 规则层（明确广告 → SKIP，0 次模型调用） ----
_ADULT_RE = re.compile(
    r"【体验细节】|嫩妹|修车|约炮|口爆|上门服务|嫖|淫妻|催情")
_GROUP_SPAM_RE = re.compile(
    r"(进群|拉群|加群|入群|点击进群|进频道).{0,30}"
    r"(资源|福利|交流群|会员|永久)|老色批")
_CONTACT_SALE_RE = re.compile(
    r"(加微信|加vx|vx号|微信号|私信我|联系我).{0,20}"
    r"([a-zA-Z0-9_-]{6,}|领取|优惠|返利|下单|折扣|秒杀)")
_COUPON_RE = re.compile(r"优惠券|返利|佣金|推广费|招代理|招下级")

NOISE_SKIP_RULES = (
    ("adult_service", _ADULT_RE),
    ("group_spam", _GROUP_SPAM_RE),
    ("contact_sale", _CONTACT_SALE_RE),
    ("coupon_or_rebate", _COUPON_RE),
)

# ---- 疑似层（弱信号 → 交给 LLM 边界判断） ----
_SUSPECT_RE = re.compile(
    r"私聊|私信|完整版|加v|领取|免费拿|扫码|客服|下单|拼单|优惠")

# ---- 冻结 §11.3：投资认知正例（防"见股票词即排除"的语义误杀） ----
INVESTMENT_POSITIVE_EXAMPLES = (
    "上市公司商业模式分析",
    "企业基本面分析",
    "长期投资框架",
    "投资纪律",
    "风险管理",
    "估值方法",
    "认知型投资方法论",
)

INCLUDE_HINTS = (
    "AI / 大模型 / Agent / AI 工具 / 编程工具",
    "审计 / CPA / 会计 / 财税 / 合规 / 企业管理",
    "商业 / 创业 / 产品 / 运营",
    "副业 / 赚钱 / 商业模式 / 个人变现",
    "认知提升 / 思维模型 / 决策 / 个人成长",
    "效率 / 工作流 / 知识管理",
    "民宿 / 短租 / OTA / 本地生活",
    "自媒体 / 内容运营 / 抖音 / 直播电商 / 带货",
    "企业分析 / 商业分析 / 投资纪律 / 长期投资方法 / 风险管理",
)

EXCLUDE_HINTS = (
    "个股荐股 / 短线交易 / 行情预测 / 涨停打板炒股技巧",
    "恋爱 / 情感关系 / 婚恋技巧",
    "娱乐八卦",
    "纯热点新闻搬运且无方法论价值",
)


@dataclass(frozen=True)
class NoiseDecision:
    decision: str
    reason_code: str
    confidence: float


@dataclass(frozen=True)
class InterestDecision:
    decision: str
    primary_topic: str
    reason: str
    confidence: float


def parse_noise_json(raw: str) -> NoiseDecision | None:
    """§21 契约：非法 JSON / 未知 enum → None（调用方转 REVIEW）。"""
    try:
        data = json.loads(raw)
        decision = data["decision"]
        if decision not in NOISE_DECISIONS:
            return None
        confidence = float(data.get("confidence", 0.5))
        return NoiseDecision(decision=decision,
                             reason_code=str(data.get("reason_code", "")),
                             confidence=confidence)
    except (ValueError, TypeError, KeyError):
        return None


def parse_interest_json(raw: str) -> InterestDecision | None:
    try:
        data = json.loads(raw)
        decision = data["decision"]
        if decision not in INTEREST_DECISIONS:
            return None
        confidence = float(data.get("confidence", 0.5))
        return InterestDecision(
            decision=decision,
            primary_topic=str(data.get("primary_topic", "")),
            reason=str(data.get("reason", "")),
            confidence=confidence)
    except (ValueError, TypeError, KeyError):
        return None


def _budget_call(budget, source_id, kind):
    """acquire + outcome 包装：返回 (allowed, run)。"""
    if budget is None:
        return True, None
    request_id = uuid4().hex
    status, reason = budget.acquire(source_id, kind, request_id)
    if status == "denied":
        return False, reason
    return True, request_id


def classify_noise(text: str | None, *, llm=None, budget=None,
                   source_id: str = "") -> NoiseDecision:
    """TEXT 噪声判定：规则优先，LLM 只接疑似边界。"""
    content = (text or "").strip()
    if not content:
        return NoiseDecision("REVIEW", "empty_text", 0.0)
    for reason_code, pattern in NOISE_SKIP_RULES:
        if pattern.search(content):
            return NoiseDecision("SKIP", reason_code, 1.0)
    if not _SUSPECT_RE.search(content):
        return NoiseDecision("KEEP", "whitelist_default", 1.0)
    if llm is None:
        return NoiseDecision("REVIEW", "no_llm_channel", 0.0)
    allowed, info = _budget_call(budget, source_id, "noise_classifier")
    if not allowed:
        return NoiseDecision("REVIEW", f"budget_denied:{info}", 0.0)
    prompt = (
        "判断以下 Telegram 消息属于知识内容还是广告。只输出 JSON："
        '{"decision": "KEEP|SKIP|REVIEW", "reason_code": "...", '
        '"confidence": 0.0}\n消息：' + content)
    try:
        raw = llm(prompt)
    except Exception:  # noqa: BLE001 —— 注入通道任意失败→REVIEW
        _settle(budget, source_id, "noise_classifier", info, "empty")
        return NoiseDecision("REVIEW", "llm_failure", 0.0)
    decision = parse_noise_json(raw)
    _settle(budget, source_id, "noise_classifier", info,
            "success" if decision is not None else "empty")
    if decision is None:
        return NoiseDecision("REVIEW", "unparseable_output", 0.0)
    return decision


def _settle(budget, source_id, kind, request_id, outcome) -> None:
    if budget is not None and request_id is not None:
        budget.outcome(source_id, kind, request_id, outcome)


def classify_interest(pdf_meta: dict, *, llm=None, budget=None,
                      source_id: str = "") -> InterestDecision:
    """PDF Interest 判定（下载前，仅凭可见元数据；§11.4）。

    广告预筛直接 EXCLUDE（0 次调用）；信息不足或无通道 → REVIEW，
    不猜测内容。
    """
    caption = (pdf_meta.get("caption") or "").strip()
    filename = (pdf_meta.get("filename") or "").strip()
    source_name = (pdf_meta.get("source_display_name") or "").strip()
    visible = f"{caption}\n{filename}"
    for reason_code, pattern in NOISE_SKIP_RULES:
        if pattern.search(visible):
            return InterestDecision("EXCLUDE", "ad", reason_code, 1.0)
    if not (caption or filename or source_name):
        return InterestDecision("REVIEW", "", "insufficient_metadata", 0.0)
    if llm is None:
        return InterestDecision("REVIEW", "", "no_llm_channel", 0.0)
    allowed, info = _budget_call(budget, source_id,
                                 "pdf_interest_classifier")
    if not allowed:
        return InterestDecision("REVIEW", "",
                                f"budget_denied:{info}", 0.0)
    prompt = (
        "根据元数据判断该 PDF 是否属于用户兴趣。只输出 JSON："
        '{"decision": "INCLUDE|EXCLUDE|REVIEW", "primary_topic": "...", '
        '"reason": "...", "confidence": 0.0}\n'
        "用户兴趣（INCLUDE 参照）：\n- " + "\n- ".join(INCLUDE_HINTS)
        + "\n用户不感兴趣（EXCLUDE 参照）：\n- "
        + "\n- ".join(EXCLUDE_HINTS)
        + "\n注意：以下属于投资认知类内容，必须优先 INCLUDE 或 "
          "REVIEW，绝不能因出现证券词就 EXCLUDE：\n- "
        + "\n- ".join(INVESTMENT_POSITIVE_EXAMPLES)
        + "\n\n来源频道：" + source_name
        + "\n文件名：" + filename
        + "\n说明文字：" + caption)
    try:
        raw = llm(prompt)
    except Exception:  # noqa: BLE001 —— 注入通道任意失败→REVIEW
        _settle(budget, source_id, "pdf_interest_classifier", info, "empty")
        return InterestDecision("REVIEW", "", "llm_failure", 0.0)
    decision = parse_interest_json(raw)
    _settle(budget, source_id, "pdf_interest_classifier", info,
            "success" if decision is not None else "empty")
    if decision is None:
        return InterestDecision("REVIEW", "", "unparseable_output", 0.0)
    return decision
