"""V3 M5: Personal Knowledge Port——外部命令 + 内部 approved 两个 adapter。

冻结 env 契约（实施方案 Task 5 Step 1）：
- KI_INSIGHT_RETRIEVAL_CMD / KI_INSIGHT_RETRIEVAL_TIMEOUT（默认 60s）/
  KI_INSIGHT_RETRIEVAL_CWD（可选）
- stdin = {"schema_version":1,"queries":[...],"allowed_states":[...],
  "limit":N}
- stdout = {"records":[{record_id,kind,state,text,source_ref,updated_at}]}
- 显式空 records 合法（设计 §10.7）；坏输出 → RetrievalError（可恢复），
  绝不伪造"空成功"。

本机审阅意见 A3：本机无稳定统一检索入口，V3.0 首版=
CommandPersonalKnowledgePort（外部命令）+ ApprovedCognitionKnowledgePort
（Insight Store 内部闭环）；Promotion Gate 见 M10。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .model_port import ModelNotConfiguredError, extract_json_object
from .models import PERSONAL_KNOWLEDGE_STATES, PersonalKnowledgeRecord

if TYPE_CHECKING:
    from .store import InsightStore

REQUIRED_RECORD_FIELDS = ("record_id", "kind", "state", "text",
                          "source_ref", "updated_at")


class RetrievalError(RuntimeError):
    """个人知识检索失败（可恢复；绝不伪造空成功）。"""


@runtime_checkable
class PersonalKnowledgePort(Protocol):
    def search(self, queries: list[str], *, statuses: list[str],
               limit: int) -> list[PersonalKnowledgeRecord]: ...


def _parse_record(raw) -> PersonalKnowledgeRecord | None:
    if not isinstance(raw, dict):
        raise RetrievalError(f"record is not an object: {type(raw).__name__}")
    missing = [f for f in REQUIRED_RECORD_FIELDS if f not in raw]
    if missing:
        raise RetrievalError(f"record missing fields: {missing}")
    if raw["state"] not in PERSONAL_KNOWLEDGE_STATES:
        raise RetrievalError(f"unknown record state: {raw['state']!r}")
    return PersonalKnowledgeRecord(
        record_id=str(raw["record_id"]), kind=str(raw["kind"]),
        state=raw["state"], text=str(raw["text"]),
        source_ref=(str(raw["source_ref"]) if raw["source_ref"] else None),
        updated_at=str(raw["updated_at"]))


class CommandPersonalKnowledgePort:
    """外部检索命令 adapter：部署配置决定接什么知识源。"""

    def __init__(self, env: dict | None = None):
        # env=None → 进程环境；显式传 dict 用于测试注入
        self.env = dict(env) if env is not None else dict(os.environ)

    def timeout_seconds(self) -> float:
        raw = self.env.get("KI_INSIGHT_RETRIEVAL_TIMEOUT", "").strip()
        if not raw:
            return 60.0
        try:
            return float(raw)
        except ValueError as exc:
            raise RetrievalError(
                f"invalid KI_INSIGHT_RETRIEVAL_TIMEOUT: {raw!r}") from exc

    def _command(self) -> str:
        command = self.env.get("KI_INSIGHT_RETRIEVAL_CMD", "").strip()
        if not command:
            raise ModelNotConfiguredError(
                "no personal knowledge retrieval command configured "
                "(set KI_INSIGHT_RETRIEVAL_CMD)")
        return command

    def search(self, queries: list[str], *, statuses: list[str],
               limit: int) -> list[PersonalKnowledgeRecord]:
        request = json.dumps({
            "schema_version": 1, "queries": list(queries),
            "allowed_states": list(statuses), "limit": limit},
            ensure_ascii=False)
        try:
            proc = subprocess.run(
                shlex.split(self._command()), input=request,
                capture_output=True, text=True,
                timeout=self.timeout_seconds(), check=False,
                cwd=self.env.get("KI_INSIGHT_RETRIEVAL_CWD", "").strip()
                or None)
        except subprocess.TimeoutExpired as exc:
            raise RetrievalError("personal knowledge command timed out") \
                from exc
        except OSError as exc:
            raise RetrievalError(
                f"personal knowledge command failed: {exc}") from exc
        if proc.returncode != 0:
            raise RetrievalError(
                f"personal knowledge command exit {proc.returncode}")
        parsed = extract_json_object(proc.stdout)
        if parsed is None or "records" not in parsed:
            raise RetrievalError(
                "personal knowledge output has no records array")
        records = []
        for raw in parsed["records"]:
            record = _parse_record(raw)
            if record is not None:
                records.append(record)
        return records


class ApprovedCognitionKnowledgePort:
    """Insight Store 内已批准认知（仅 CONFIRMED，Human Gate ADOPT 产物）。

    High Recall 语义（设计 §7.2 在检索层的延伸）：返回全部 CONFIRMED
    记录（token 重叠数仅作排序提示，绝不过滤）——本库是人类门控的
    小集合，精确取舍是下游 Selector 的职责；本层绝不因匹配粒度
    （尤其中文整句无分词的天然缺陷）丢掉可能相关的个人证据。
    """

    def __init__(self, store: InsightStore):
        self.store = store

    @staticmethod
    def _tokens(queries: list[str]) -> list[str]:
        tokens: set[str] = set()
        for query in queries:
            for token in re.findall(r"[\w\u4e00-\u9fff]+", query or ""):
                if len(token) >= 2:
                    tokens.add(token.casefold())
        return sorted(tokens)

    def search(self, queries: list[str], *, statuses: list[str],
               limit: int) -> list[PersonalKnowledgeRecord]:
        if "CONFIRMED" not in statuses:
            return []
        tokens = self._tokens(queries)
        scored = []
        for row in self.store.list_approved_cognition():
            text = row["cognition"].casefold()
            overlap = sum(1 for t in tokens if t in text)
            scored.append((overlap, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["approved_at"]))
        records = []
        for _overlap, row in scored:
            records.append(PersonalKnowledgeRecord(
                record_id=row["record_id"], kind="cognition",
                state="CONFIRMED", text=row["cognition"],
                source_ref=f"approved://{row['proposal_id']}",
                updated_at=row["approved_at"]))
            if len(records) >= limit:
                break
        return records


class CompositePersonalKnowledgePort:
    """多源组合；按 record_id 去重，首个 port 优先。"""

    def __init__(self, ports: list):
        self.ports = list(ports)

    def search(self, queries: list[str], *, statuses: list[str],
               limit: int) -> list[PersonalKnowledgeRecord]:
        seen: dict[str, PersonalKnowledgeRecord] = {}
        for port in self.ports:
            for record in port.search(queries, statuses=statuses,
                                      limit=limit):
                if record.record_id not in seen:
                    seen[record.record_id] = record
        return list(seen.values())[:limit]
