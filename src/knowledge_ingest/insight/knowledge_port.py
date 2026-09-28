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

    High Recall 语义（设计 §7.2 在检索层的延伸；M5 修复②冻结）：
    全部保留 eligible 记录 → CJK-aware relevance hint 排序 →
    再应用 candidate cap → Selector 精确判断。hint 绝不过滤：
    score=0 的记录仍返回（排在命中者之后、按 approved_at 稳定序），
    防止认知库增长后"相关记录排在第 21 条"的 starvation。
    不引入 embedding/向量库——确定性字符 n-gram 粗召回排序。
    """

    def __init__(self, store: InsightStore):
        self.store = store

    @staticmethod
    def _latin_tokens(text: str) -> set[str]:
        return {t.casefold() for t in re.findall(r"[A-Za-z0-9]+", text or "")
                if len(t) >= 2}

    @staticmethod
    def _cjk_bigrams(text: str) -> set[str]:
        """CJK 连续段的相邻字符对（中文无空格分词的确定性近似）。"""
        bigrams: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]+", text or ""):
            for i in range(len(run) - 1):
                bigrams.add(run[i:i + 2])
        return bigrams

    @classmethod
    def _relevance_hint(cls, queries: list[str], text: str) -> int:
        """排序提示（非过滤）：拉丁 token 命中 + CJK bigram 命中。"""
        query_latin = set()
        query_bigrams = set()
        for query in queries or []:
            query_latin |= cls._latin_tokens(query)
            query_bigrams |= cls._cjk_bigrams(query)
        text_latin = cls._latin_tokens(text)
        text_bigrams = cls._cjk_bigrams(text)
        latin_hits = len(query_latin & text_latin)
        bigram_hits = len(query_bigrams & text_bigrams)
        return latin_hits + bigram_hits

    def search(self, queries: list[str], *, statuses: list[str],
               limit: int) -> list[PersonalKnowledgeRecord]:
        if "CONFIRMED" not in statuses:
            return []
        scored = []
        for row in self.store.list_approved_cognition():
            hint = self._relevance_hint(queries or [""], row["cognition"])
            scored.append((hint, row))
        # hint 降序 → approved_at 稳定序；score=0 仍全部保留
        scored.sort(key=lambda pair: (-pair[0], pair[1]["approved_at"]))
        records = []
        for _hint, row in scored:
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
