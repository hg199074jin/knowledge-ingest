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
            "instruction": (
                "从候选个人记录中选出会影响本次判断的条目。每条必须给出"
                "relation_reason——为什么它会改变/条件化当前判断。"
                "主题相似但无判断影响的记录一律不选（可以全不选）。"
                "发现相互冲突的旧认知时在 conflicts 中声明，不要静默取舍。"),
            "output_contract": (
                '只输出一个 JSON 对象：{"selected": [{"record_id": "...", '
                '"relation_reason": "..."}], "conflicts": [{"record_ids": '
                '["A","B"], "reason": "..."}]}。selected 可以为空数组；'
                "record_id 必须来自 candidates；relation_reason 必填。"
                "不要输出 JSON 以外的任何文字。"),
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
        if not isinstance(raw, dict) or "selected" not in raw:
            raise _bad("selector output missing selected")
        return raw

    # ---------- public ----------

    def retrieve(self, view: InsightSourceView, content
                 ) -> tuple[PersonalContextRef, ...]:
        queries, include_history = self._plan(view, content)
        candidates = self._lookup(queries, include_history)
        raw = self._select(queries, candidates, view, content)

        by_id: dict[str, PersonalKnowledgeRecord] = {
            r.record_id: r for r in candidates}

        selected_raw = raw["selected"][:self.max_selected]
        refs: list[PersonalContextRef] = []
        for entry in selected_raw:
            if not isinstance(entry, dict):
                raise _bad(f"selected entry is not an object: {entry!r}")
            record_id = str(entry.get("record_id", "")).strip()
            reason = str(entry.get("relation_reason", "")).strip()
            if not record_id or not reason:
                raise _bad(
                    f"selected entry needs record_id and relation_reason: "
                    f"{entry!r}")
            record = by_id.get(record_id)
            if record is None:
                raise _bad(f"selected unknown record: {record_id}")
            refs.append(PersonalContextRef(
                record_id=record_id, relation_reason=reason,
                state=record.state, source_ref=record.source_ref,
                kind=record.kind, text=record.text))

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
        text=ref.text, conflict_with=conflicts)
