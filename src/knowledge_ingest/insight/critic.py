"""V3 M6: Quality Critic——独立复核（设计 §18）。

不重新做完整内容分析，只检查成品是否退化。8 个质量维度
（pass/partial/fail）+ genericity_detected（陌生人测试）+
traceability（三个月后测试）+ revision_required / revision_instructions。

两个硬质量测试随 prompt 下发：
- 陌生人测试：遮掉用户身份后卡片能否原封不动发给任何 AI/商业爱好者
  → personalization=FAIL；
- 三个月后测试：旧判断/新证据/为何改变/新判断/当时决定是否可回溯
  → cognition_traceability=FAIL。

坏输出 → ModelBadOutputError（可恢复重试），绝不静默 PASS。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model_port import ModelBadOutputError
from .models import CriticResult, EvidencePack

_DIMENSIONS = ("source_understanding", "critical_reasoning",
               "personal_connection", "cognition_delta", "own_version",
               "actionability", "business_rigor", "traceability",
               "mechanism_salvage")
_BOOL_FIELDS = ("genericity_detected", "revision_required")

CRITIC_DIRECTIVE = (
    "你是独立质量复核员，不重新分析内容，只检查这份初稿是否退化。"
    "逐项检查：\n"
    "1 是否主要在复述原文？2 是否存在泛泛的'对你有启发'？\n"
    "3 是否存在硬关联（没有因果说明的个人关联）？\n"
    "4 是否把作者观点误当事实？5 是否真正形成认知 Delta？\n"
    "6 '我的版本'是否只是换句话说？7 行动是否是假行动"
    "（持续关注/深入学习/积极探索）？\n"
    "8 商业机会是否只看收入案例？9 是否忽视相关旧认知状态？\n"
    "10 是否为凑模板强行制造反对意见？\n"
    "11 机制打捞（mechanism_salvage）：在指出来源证据不足之后，是否仍然"
    "提炼出了'即使拿掉夸张部分，剩下值得保留的机制/假设'？如果批判之后"
    "只剩谨慎提醒、没有榨出任何待验证的机制假设 → mechanism_salvage=fail"
    "（防止退化成'高级怀疑式摘要器'）。\n"
    "12 行动标签与正文行为一致：WATCH 不得安排主动验证动作、不得编造"
    "时间点（如'三个月后'，应改条件触发）；EXPERIMENT 必须是主动低成本"
    "验证；不一致 → actionability=fail 并给出修正指令。\n"
    "两个硬测试：\n"
    "【陌生人测试】遮掉用户身份后，这份卡片能否原封不动发给任何一个"
    "喜欢 AI/商业的人？能 → genericity_detected=true（个性化失败）。\n"
    "【三个月后测试】三个月后的用户能否知道：旧判断是什么、新证据是什么、"
    "为什么改变、新判断是什么、当时决定做什么？不能 → traceability=fail。\n"
    "诚实优先：没有实质问题的初稿应当 PASS，不要为凑严格而强行挑刺；"
    "但泛泛而谈必须打回。")


def _bad(message: str) -> ModelBadOutputError:
    return ModelBadOutputError(f"critic: {message}")


class QualityCritic:
    def __init__(self, port):
        self.port = port

    def review(self, pack: EvidencePack, draft: dict) -> CriticResult:
        payload = {
            "directive": CRITIC_DIRECTIVE,
            "output_contract": (
                '只输出一个 JSON 对象：{"source_understanding": '
                '"pass|partial|fail", "critical_reasoning": ..., '
                '"personal_connection": ..., "cognition_delta": ..., '
                '"own_version": ..., "actionability": ..., '
                '"business_rigor": ..., "traceability": ..., '
                '"mechanism_salvage": ..., '
                '"genericity_detected": bool, "revision_required": bool, '
                '"revision_instructions": ["..."]}。9 个维度取值只能是 '
                "pass/partial/fail。不要输出 JSON 以外的任何文字。"),
            "source_material": {
                "source_claims": list(pack.source_claims),
                "source_evidence": list(pack.source_evidence),
                "unknown_variables": list(pack.unknown_variables),
                "personal_context": [
                    {"record_id": r.record_id, "state": r.state,
                     "text": r.text} for r in pack.personal_context],
                "source_parts": [
                    {"part_id": p.part_id, "text": p.text}
                    for p in pack.source_parts],
            },
            "draft": draft,
        }
        raw = self.port.run("critic", payload)
        return self.parse(raw)

    def parse(self, raw) -> CriticResult:
        if not isinstance(raw, dict):
            raise _bad("output is not an object")
        missing = [f for f in _DIMENSIONS + _BOOL_FIELDS if f not in raw]
        if missing:
            raise _bad(f"missing fields: {missing}")
        kwargs = {}
        for name in _DIMENSIONS:
            value = str(raw[name]).strip().lower()
            if value not in ("pass", "partial", "fail"):
                raise _bad(f"unknown {name}: {raw[name]!r}")
            kwargs[name] = value
        for name in _BOOL_FIELDS:
            if not isinstance(raw[name], bool):
                raise _bad(f"{name} must be bool, got {raw[name]!r}")
            kwargs[name] = raw[name]
        instructions = raw.get("revision_instructions") or []
        if not isinstance(instructions, list) or not all(
                isinstance(i, str) for i in instructions):
            raise _bad("revision_instructions must be a list of strings")
        if kwargs["revision_required"] and not instructions:
            raise _bad("revision_required=true requires instructions")
        return CriticResult(revision_instructions=tuple(instructions),
                            **kwargs)


@dataclass(frozen=True)
class ThinkingOutcome:
    """深思编排结果：passed / needs_review / blocked（blocked 可恢复）。

    M6 复核②：保留完整审计链——initial_draft / initial_review /
    revision_instructions / revised draft / final_review，needs_review
    必须可解释。
    """

    draft: dict
    final_review: CriticResult | None
    status: str
    initial_draft: dict | None = None
    initial_review: CriticResult | None = None
    revision_instructions: tuple[str, ...] = ()


class ThinkingOrchestrator:
    """think → review →（最多一次修订）→ 终审。

    硬停语义（Review Focus #4）：恰好最多 2 次 thinker 调用；终审仍
    revision_required → needs_review（无 passed、无 Proposal）；
    Critic 自身失败 → blocked（草稿保留，可恢复），绝不降级成
    普通摘要卡。thinker 首次调用的错误向上传播（服务层重试）。
    """

    def __init__(self, thinker, critic, *, max_revisions: int = 1):
        self.thinker = thinker
        self.critic = critic
        self.max_revisions = max_revisions

    def run(self, pack: EvidencePack) -> ThinkingOutcome:
        initial_draft = self.thinker.think(pack)
        draft = initial_draft
        try:
            initial_review = self.critic.review(pack, draft)
        except ModelBadOutputError:
            return ThinkingOutcome(draft=draft, final_review=None,
                                   status="blocked",
                                   initial_draft=initial_draft)
        instructions: tuple[str, ...] = ()
        revisions_left = self.max_revisions
        while initial_review.revision_required and revisions_left > 0:
            revisions_left -= 1
            instructions = tuple(initial_review.revision_instructions)
            draft = self.thinker.revise(pack, draft, list(instructions))
            try:
                final_review = self.critic.review(pack, draft)
            except ModelBadOutputError:
                return ThinkingOutcome(
                    draft=draft, final_review=None, status="blocked",
                    initial_draft=initial_draft,
                    initial_review=initial_review,
                    revision_instructions=instructions)
            break
        else:
            final_review = initial_review
        if final_review.revision_required:
            return ThinkingOutcome(
                draft=draft, final_review=final_review,
                status="needs_review", initial_draft=initial_draft,
                initial_review=initial_review,
                revision_instructions=instructions)
        return ThinkingOutcome(
            draft=draft, final_review=final_review, status="passed",
            initial_draft=initial_draft, initial_review=initial_review,
            revision_instructions=instructions)


# ---------- M10-R2：ThinkingOutcome 产物持久化（restart-safe） ----------

def outcome_to_json(outcome: ThinkingOutcome) -> str:
    """完整审计链序列化：draft / final_review / initial_draft /
    initial_review / revision_instructions / status。"""
    import json
    from dataclasses import asdict

    def _review(value):
        return None if value is None else asdict(value)

    payload = {
        "schema_version": 1,
        "status": outcome.status,
        "draft": outcome.draft,
        "initial_draft": outcome.initial_draft,
        "initial_review": _review(outcome.initial_review),
        "revision_instructions": list(outcome.revision_instructions),
        "final_review": _review(outcome.final_review),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def outcome_from_json(blob: str) -> ThinkingOutcome:
    import json

    data = json.loads(blob)

    def _review(value):
        if value is None:
            return None
        fields = {
            "source_understanding", "critical_reasoning",
            "personal_connection", "cognition_delta", "own_version",
            "actionability", "business_rigor", "traceability",
            "mechanism_salvage", "genericity_detected",
            "revision_required", "revision_instructions"}
        kwargs = {k: v for k, v in value.items() if k in fields}
        kwargs["revision_instructions"] = tuple(
            kwargs.get("revision_instructions") or ())
        return CriticResult(**kwargs)

    return ThinkingOutcome(
        status=data["status"],
        draft=data["draft"],
        initial_draft=data.get("initial_draft"),
        initial_review=_review(data.get("initial_review")),
        revision_instructions=tuple(
            data.get("revision_instructions") or ()),
        final_review=_review(data.get("final_review")))
