"""V3 M5: Personal Reasoning Retrieval——先提炼判断命题，再检索个人证据。

流程（设计 §10.3）：
  内容 → retrieval_query_plan（3–5 个"判断问题"，非主题关键词）
       → 四层知识检索（默认状态白名单）
       → retrieval_select（可全选零；每条必须带 relation_reason）
       → PersonalContextRef 元组（含冲突标记）

冻结语义：
- 默认状态白名单 = CONFIRMED / TENTATIVE / EXPERIMENTING；
  SUPERSEDED / REJECTED / ARCHIVED 仅当 planner 显式 include_history；
- 硬上限（上下文安全阀，非价值评分）：queries 5 / candidates 20 /
  selected 12；
- Selector 可选零条 = NO_RELEVANT_PERSONAL_CONTEXT（合法，设计 §10.7）；
- 冲突不静默消除：selector 声明的冲突对双方都保留并互标 conflict_with
  （设计 §10.6）；
- 任何模型/检索失败向上抛类型化错误（可恢复），绝不伪造空结果。
"""

from __future__ import annotations

from .model_port import ModelBadOutputError
from .models import (
    InsightSourceView,
    PersonalContextRef,
    PersonalKnowledgeRecord,
)

DEFAULT_STATUSES = ("CONFIRMED", "TENTATIVE", "EXPERIMENTING")
HISTORY_STATUSES = ("SUPERSEDED", "REJECTED", "ARCHIVED")
MAX_QUERIES = 5
MAX_CANDIDATES = 20
MAX_SELECTED = 12
MIN_QUERIES = 3


def _bad(message: str) -> ModelBadOutputError:
    return ModelBadOutputError(f"retrieval: {message}")

SELECTOR_INSTRUCTION = (
    "对下列每一条候选个人记录逐条做结构化裁决。裁决标准：\n"
    "- DIRECT：材料为该记录中的判断提供了新证据、新反例，或直接命中"
    "其主题；\n"
    "- CONDITIONING：材料处于该记录覆盖的问题域内——评估或使用这份"
    "材料时应当带着该记录的立场/约束/教训；\n"
    "- CONFLICT：材料主张与该记录相矛盾；\n"
    "- NONE：该记录的问题域与材料完全不相交（例：材料讲 AI 工具生态，"
    "记录讲民宿定价策略）。\n"
    "校准规则（宁高勿低）：判断时问自己——评估这份材料时是否应该想到"
    "这条记录？是 → 至少 CONDITIONING。只有问题域完全不相交才是 NONE。"
    "同一问题域的多条候选应给出同向裁决，不允许随机翻转。每条必须给出"
    "relation_reason；非 NONE 时说明它具体改变/条件化哪个判断维度。"
    "禁止为凑数硬选明显无关的记录。")

SELECTOR_CONTRACT = (
    '只输出一个 JSON 对象：{"judgements": [{"record_id": "...", '
    '"relation": "DIRECT|CONDITIONING|CONFLICT|NONE", '
    '"relation_reason": "...", "confidence": 0.0-1.0}], '
    '"conflicts": [{"record_ids": ["A","B"], "reason": "..."}]}。'
    "judgements 必须覆盖全部 candidates（每条恰好一次）；"
    "relation 非 NONE 时 relation_reason 必填。"
    "不要输出 JSON 以外的任何文字。")


class PersonalReasoningRetriever:
    def __init__(self, model_port, knowledge_port, *,
                 max_queries: int = MAX_QUERIES,
                 max_candidates: int = MAX_CANDIDATES,
                 max_selected: int = MAX_SELECTED):
        self.model_port = model_port
        self.knowledge_port = knowledge_port
        self.max_queries = max_queries
        self.max_candidates = max_candidates
        self.max_selected = max_selected

    # ---------- stage 1: query plan ----------

    def _plan(self, view: InsightSourceView, content) -> tuple[list[str], bool]:
        payload = {
            "instruction": (
                "把这篇内容提炼成 3-5 个【判断问题】——每个问题指向"
                "一段会影响本次判断的用户个人证据（旧认知/决策/项目/实验）。"
                "不要输出主题关键词。"),
            "output_contract": (
                '只输出一个 JSON 对象：{"queries": ["问题1", ...], '
                '"include_history": false}。queries 至少 3 条、最多 5 条；'
                "include_history=true 仅当需要核查用户已否决/已废弃的方向。"
                "不要输出 JSON 以外的任何文字。"),
            "kind": view.content_kind,
            "title": view.title,
            "visible_text": content.parts[0].text[:4000] if content.parts
            else view.visible_text[:4000],
        }
        raw = self.model_port.run("retrieval_query_plan", payload)
        if not isinstance(raw, dict) or "queries" not in raw:
            raise _bad("planner output missing queries")
        queries = [str(q).strip() for q in raw["queries"] if str(q).strip()]
        if len(queries) < MIN_QUERIES:
            raise _bad(
                f"planner produced {len(queries)} queries; need >= "
                f"{MIN_QUERIES}")
        clamped = queries[:self.max_queries]
        include_history = bool(raw.get("include_history", False))
        return clamped, include_history

    # ---------- stage 2: candidate lookup ----------

    def _lookup(self, queries: list[str],
                include_history: bool) -> list[PersonalKnowledgeRecord]:
        statuses = list(DEFAULT_STATUSES)
        if include_history:
            statuses += list(HISTORY_STATUSES)
        return self.knowledge_port.search(
            queries, statuses=statuses, limit=self.max_candidates)

    # ---------- stage 3: selection ----------

    def _select(self, queries: list[str], candidates, view: InsightSourceView,
                content):
        payload = {
            "instruction": SELECTOR_INSTRUCTION,
            "output_contract": SELECTOR_CONTRACT,
            "queries": queries,
            "kind": view.content_kind,
            "visible_text": content.parts[0].text[:4000] if content.parts
            else view.visible_text[:4000],
            "candidates": [
                {"record_id": r.record_id, "kind": r.kind, "state": r.state,
                 "text": r.text, "source_ref": r.source_ref}
                for r in candidates],
        }
        raw = self.model_port.run("retrieval_select", payload)
        if not isinstance(raw, dict) or "judgements" not in raw:
            raise _bad("selector output missing judgements")
        return raw

    # ---------- public ----------

    def select_refs(self, queries: list[str], candidates, view,
                    content) -> tuple[PersonalContextRef, ...]:
        """Selector 逐候选裁决 + 确定性过滤（probe/M8 复用入口）。"""
        raw = self._select(queries, candidates, view, content)

        by_id: dict[str, PersonalKnowledgeRecord] = {
            r.record_id: r for r in candidates}

        # 逐候选结构化裁决（M5 修复①）：完整性强制——空结果只能来自
        # 模型显式把每条都判 NONE，绝不能来自"少输出一个元素"
        judgements = raw["judgements"]
        if not isinstance(judgements, list):
            raise _bad("judgements must be a list")
        judged: dict[str, dict] = {}
        for entry in judgements:
            if not isinstance(entry, dict):
                raise _bad(f"judgement is not an object: {entry!r}")
            record_id = str(entry.get("record_id", "")).strip()
            relation = str(entry.get("relation", "")).strip().upper()
            reason = str(entry.get("relation_reason", "")).strip()
            confidence = entry.get("confidence")
            if record_id not in by_id:
                raise _bad(f"judged unknown record: {record_id!r}")
            if record_id in judged:
                raise _bad(f"duplicate judgement for {record_id!r}")
            if relation not in ("DIRECT", "CONDITIONING", "CONFLICT", "NONE"):
                raise _bad(f"unknown relation: {entry.get('relation')!r}")
            if relation != "NONE" and not reason:
                raise _bad(
                    f"relation={relation} for {record_id!r} needs "
                    f"relation_reason")
            if (not isinstance(confidence, (int, float))
                    or not 0.0 <= confidence <= 1.0):
                raise _bad(f"confidence out of range for {record_id!r}")
            judged[record_id] = {"relation": relation, "reason": reason,
                                 "confidence": float(confidence)}
        missing = sorted(set(by_id) - set(judged))
        if missing:
            raise _bad(
                "judgements incomplete; candidates never judged: "
                f"{missing}")

        refs: list[PersonalContextRef] = []
        for record_id, verdict in judged.items():
            if verdict["relation"] == "NONE":
                continue                    # 确定性过滤：显式 NONE 才排除
            record = by_id[record_id]
            refs.append(PersonalContextRef(
                record_id=record_id, relation_reason=verdict["reason"],
                state=record.state, source_ref=record.source_ref,
                kind=record.kind, text=record.text,
                relation=verdict["relation"]))
        refs = refs[:self.max_selected]     # 上下文安全阀（非价值评分）

        # 冲突标记（设计 §10.6：冲突不静默消除）
        pairs = raw.get("conflicts") or []
        conflict_map: dict[str, set[str]] = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            ids = [str(i) for i in pair.get("record_ids", [])]
            known = [i for i in ids
                     if i in {r.record_id for r in refs}]
            for record_id in known:
                conflict_map.setdefault(record_id, set()).update(
                    i for i in known if i != record_id)
        if conflict_map:
            refs = [dataclasses_replace_with_conflicts(r, conflict_map)
                    for r in refs]
        return tuple(refs)

    def retrieve(self, view: InsightSourceView, content
                 ) -> tuple[PersonalContextRef, ...]:
        queries, include_history = self._plan(view, content)
        candidates = self._lookup(queries, include_history)
        return self.select_refs(queries, candidates, view, content)


def dataclasses_replace_with_conflicts(ref: PersonalContextRef,
                                       conflict_map: dict[str, set[str]]
                                       ) -> PersonalContextRef:
    """冻结 ref 附加冲突标记（冲突双方都保留，互指对方）。"""
    conflicts = tuple(sorted(conflict_map.get(ref.record_id, ())))
    if not conflicts:
        return ref
    return PersonalContextRef(
        record_id=ref.record_id, relation_reason=ref.relation_reason,
        state=ref.state, source_ref=ref.source_ref, kind=ref.kind,
        text=ref.text, conflict_with=conflicts, relation=ref.relation)
