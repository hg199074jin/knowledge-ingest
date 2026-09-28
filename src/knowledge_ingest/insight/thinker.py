"""V3 M6: Personal Thinking Engine——五阶段协议 + 结构化契约。

协议顺序冻结（设计 §13.3）：Understand → Challenge → Connect →
Reconstruct → Decide；Evidence before Advice——pack 中源主张/证据/
个人上下文分开送入，禁止把个人主张当源证据。

输出契约（结构化）：bottom_line / source_understanding / mechanism /
challenge / personal_connections[] / project_impacts[] /
business_opportunity|null（market+personal 两拆）| own_version /
cognition_delta / actions[]（≤3）| human_gate_recommendation /
final_verdict（CARD|ARCHIVE|REJECT——深思后允许否决，即使已过 Value Gate）。
"""

from __future__ import annotations

import re

from .model_port import ModelBadOutputError
from .models import (
    COGNITION_DELTA_TYPES,
    HUMAN_GATE_DECISIONS,
    EvidencePack,
)

THINKING_ACTION_TYPES = ("IMMEDIATE", "EXPERIMENT", "WATCH", "NONE")
FINAL_VERDICT_TYPES = ("CARD", "ARCHIVE", "REJECT")

PROTOCOL_DIRECTIVE = (
    "严格按五个阶段思考，顺序不可颠倒：\n"
    "1 Understand（先理解，暂不个性化）：作者真正主张什么？用了什么证据？"
    "哪些是事实/观点/推断？结论依赖哪些前提？缺哪些关键变量？\n"
    "2 Challenge：主动检查幸存者偏差、样本偏差、因果倒置、选择性披露、"
    "营销包装、隐藏成本、忽略竞争……尝试提出最强反例，但不得为反对而反对。\n"
    "3 Connect：把旧认知 A + 新证据 B 转成真实的因果连接——为什么 B 会"
    "影响 A？主题级套话无效。\n"
    "4 Reconstruct：转化为用户自己的版本——不引用原作者金句、不模仿原文"
    "结构、不停留在摘要；从第一性原理重新表达机制，产出未来可直接复用的"
    "判断原则。\n"
    "4.5 机制打捞（mechanism salvage）：在指出来源证据不足、边界和反例"
    "之后，必须仍然回答——即使把夸张部分拿掉，剩下值得保留的机制/假设"
    "是什么？证据不足时提炼'待验证的机制假设'，而不是只剩谨慎提醒；"
    "批判不等于清空。\n"
    "5 Decide：认知变化（新增/强化/修正/推翻/无变化）、商业机会判断、"
    "行动（最多 3 个，允许 ARCHIVE/REJECT）。\n"
    "Evidence before Advice：先证据后建议。旧认知只是个人主张，允许被"
    "新证据修正或推翻。\n\n"
    "行动语义（label 必须与正文行为一致，M6 复核④冻结）：\n"
    "- NONE：无需行动；\n"
    "- WATCH：等待新的外部证据/条件触发，本人暂不主动验证——必须写明"
    "触发条件，禁止编造任何时间点（如'三个月后'）；\n"
    "- EXPERIMENT：主动做一个低成本验证——必须写明验证什么与判停条件；\n"
    "- IMMEDIATE：已有足够条件直接应用。\n"
    "给 WATCH 写'去主动选任务做验证'之类的正文是标签错误。")


def _bad(message: str) -> ModelBadOutputError:
    return ModelBadOutputError(f"thinking: {message}")


class PersonalThinkingEngine:
    def __init__(self, model_port):
        self.model_port = model_port

    def think(self, pack: EvidencePack) -> dict:
        return self._call(pack, extra=None)

    def revise(self, pack: EvidencePack, draft: dict,
               instructions: list[str]) -> dict:
        return self._call(pack, extra={"draft": draft,
                                       "revision_instructions": list(
                                           instructions)})

    def _call(self, pack: EvidencePack, *, extra: dict | None) -> dict:
        payload = {
            "protocol": PROTOCOL_DIRECTIVE,
            "output_contract": (
                '只输出一个 JSON 对象，字段与类型：'
                '{"title": str（≤40 字的认知命题，作卡片标题，不得与 '
                'bottom_line 全文相同）, "bottom_line": str, '
                '"source_understanding": str, "mechanism": str, '
                '"challenge": str, '
                '"personal_connections": [{"record_id": str, '
                '"connection": str}], "project_impacts": '
                '[{"project": str, "impact": str}], '
                '"business_opportunity": null 或 {"market_opportunity": str, '
                '"personal_opportunity": str}, "own_version": str, '
                '"cognition_delta": "ADD|REINFORCE|REVISE|OVERTURN|NONE", '
                '"actions": [{"action": "IMMEDIATE|EXPERIMENT|WATCH|NONE", '
                '"detail": str}]（最多 3 个）, '
                '"human_gate_recommendation": "ADOPT|EXPERIMENT|WATCH|'
                'ARCHIVE|REJECT", "final_verdict": "CARD|ARCHIVE|REJECT"}。'
                "不要输出 JSON 以外的任何文字。无商业机会则 "
                "business_opportunity=null；challenge 无实质内容就给空串。"),
            "kind_notes": (
                "personal_connections 必须覆盖 pack.personal_context 中的"
                "每一条记录（每条恰好一个条目）：能建立真实因果就写它"
                "具体改变/条件化哪个判断维度；确实不受影响也要写明为什么"
                "不受影响——禁止只字不提（这是上下文丢失）。"
                "connection 只能引用 pack.personal_context 中的 record_id，"
                "主题级硬关联无效。批判之后仍须完成机制打捞：指出证据不足"
                "的同时，把值得保留的机制/假设放进 own_version。"
                "market_opportunity 与 personal_opportunity 必须分开判断。"),
            "pack": {
                "source_claims": list(pack.source_claims),
                "source_evidence": list(pack.source_evidence),
                "source_inferences": list(pack.source_inferences),
                "unknown_variables": list(pack.unknown_variables),
                "verification_flags": list(pack.verification_flags),
                "personal_context": [
                    {"record_id": r.record_id, "state": r.state,
                     "kind": r.kind, "text": r.text,
                     "relation_reason": r.relation_reason}
                    for r in pack.personal_context],
                "source_parts": [
                    {"part_id": p.part_id, "text": p.text,
                     "source_ref": p.source_ref} for p in pack.source_parts],
            },
        }
        if extra is not None:
            payload["revision"] = extra
        raw = self.model_port.run("thinking", payload)
        thought = self._parse(raw)
        self._validate_connections(thought, pack)
        self._validate_watch_no_timepoints(thought)
        return thought

    @staticmethod
    def _parse(raw) -> dict:
        if not isinstance(raw, dict):
            raise _bad("output is not an object")
        required = ("title", "bottom_line", "source_understanding",
                    "mechanism", "challenge", "personal_connections",
                    "project_impacts", "business_opportunity",
                    "own_version", "cognition_delta", "actions",
                    "human_gate_recommendation", "final_verdict")
        missing = [k for k in required if k not in raw]
        if missing:
            raise _bad(f"missing fields: {missing}")
        if raw["cognition_delta"] not in COGNITION_DELTA_TYPES:
            raise _bad(f"unknown cognition_delta: {raw['cognition_delta']!r}")
        if raw["final_verdict"] not in FINAL_VERDICT_TYPES:
            raise _bad(f"unknown final_verdict: {raw['final_verdict']!r}")
        if raw["human_gate_recommendation"] not in HUMAN_GATE_DECISIONS:
            raise _bad("unknown human_gate_recommendation: "
                       f"{raw['human_gate_recommendation']!r}")
        actions = raw["actions"]
        if not isinstance(actions, list) or len(actions) > 3:
            raise _bad("actions must be a list with at most 3 items")
        for action in actions:
            valid_type = (isinstance(action, dict)
                          and action.get("action") in THINKING_ACTION_TYPES
                          and str(action.get("detail", "")).strip())
            if not valid_type:
                raise _bad(f"invalid action entry: {action!r}")
        connections = raw["personal_connections"]
        if not isinstance(connections, list):
            raise _bad("personal_connections must be a list")
        for conn in connections:
            if (not isinstance(conn, dict) or not conn.get("record_id")
                    or not str(conn.get("connection", "")).strip()):
                raise _bad(f"invalid personal_connection: {conn!r}")
        impacts = raw["project_impacts"]
        if not isinstance(impacts, list):
            raise _bad("project_impacts must be a list")
        biz = raw["business_opportunity"]
        biz_valid = (isinstance(biz, dict)
                     and str(biz.get("market_opportunity", "")).strip()
                     and str(biz.get("personal_opportunity", "")).strip())
        if biz is not None and not biz_valid:
            raise _bad(
                "business_opportunity requires market_opportunity and "
                "personal_opportunity")
        for key in ("title", "bottom_line", "source_understanding",
                    "mechanism", "own_version"):
            if not str(raw.get(key, "")).strip():
                raise _bad(f"{key} must be non-empty")
        if str(raw["title"]).strip() == str(raw["bottom_line"]).strip():
            raise _bad("title must differ from bottom_line "
                       "(renderer splits H1 vs conclusion)")
        return dict(raw)

    @staticmethod
    def _validate_connections(raw: dict, pack: EvidencePack) -> None:
        """连接完整性强制（M6 复核①的 Thinker 侧修复）：
        pack 中每条 ref 恰好一个连接条目——缺条即上下文丢失。"""
        expected = {r.record_id for r in pack.personal_context}
        got = [c.get("record_id") for c in raw["personal_connections"]]
        missing = sorted(expected - set(got))
        if missing:
            raise _bad(
                "personal_connections must cover every context ref; "
                f"missing: {missing}")
        extra = sorted(set(got) - expected)
        if extra:
            raise _bad(f"connections reference unknown records: {extra}")
        seen = set()
        for conn in raw["personal_connections"]:
            rid = conn.get("record_id")
            if rid in seen:
                raise _bad(f"duplicate connection for {rid!r}")
            seen.add(rid)

    @staticmethod
    def _validate_watch_no_timepoints(raw: dict) -> None:
        """M6 复核④：WATCH 禁止编造时间点（如'三个月后'）。"""
        pattern = re.compile(r"[一二三四五六七八九十\d]+\s*(?:天|周|个?月|年)后")
        for action in raw["actions"]:
            if action["action"] == "WATCH" and pattern.search(
                    action["detail"]):
                raise _bad(
                    "WATCH actions must use condition triggers, not "
                    f"fabricated timepoints: {action['detail']!r}")
