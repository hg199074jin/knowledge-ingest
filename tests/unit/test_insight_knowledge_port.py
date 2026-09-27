"""V3 M5: Personal Knowledge Port 契约（外部命令 + 内部 approved）。

冻结 env 契约（实施方案 Task 5 Step 1）：
- KI_INSIGHT_RETRIEVAL_CMD / KI_INSIGHT_RETRIEVAL_TIMEOUT(60) /
  KI_INSIGHT_RETRIEVAL_CWD
- stdin = {"schema_version":1,"queries":[...],"allowed_states":[...],"limit":20}
- stdout 记录字段：record_id / kind / state / text / source_ref / updated_at
- 坏输出 → RetrievalError（可恢复），绝不伪造"空成功"。
"""

import shlex
import sys

import pytest

from knowledge_ingest.insight.knowledge_port import (
    ApprovedCognitionKnowledgePort,
    CommandPersonalKnowledgePort,
    CompositePersonalKnowledgePort,
    PersonalKnowledgePort,
    RetrievalError,
)
from knowledge_ingest.insight.model_port import ModelNotConfiguredError
from knowledge_ingest.insight.models import PersonalKnowledgeRecord
from knowledge_ingest.insight.store import InsightStore

NOW = "2026-09-28T12:00:00+00:00"

REC = {"record_id": "PC-001", "kind": "cognition", "state": "CONFIRMED",
       "text": "Agent 效率=模型×上下文×流程×反馈",
       "source_ref": "notes://agent", "updated_at": NOW}


def make_port(script):
    return CommandPersonalKnowledgePort(env={
        "KI_INSIGHT_RETRIEVAL_CMD":
            f"{sys.executable} -c {shlex.quote(script)}"})


ECHO = "\n".join([
    "import sys, json",
    "req = json.load(sys.stdin)",
    f"rec = {REC!r}",
    "rec['query_echo'] = req['queries']; rec['states_echo'] = req['allowed_states']",
    "print(json.dumps({'records': [rec]}))",
])

# ---------- protocol / env ----------

def test_personal_knowledge_port_is_runtime_protocol():
    assert issubclass(CommandPersonalKnowledgePort, PersonalKnowledgePort)
    assert issubclass(ApprovedCognitionKnowledgePort, PersonalKnowledgePort)


def test_default_timeout_is_60s():
    port = CommandPersonalKnowledgePort(env={
        "KI_INSIGHT_RETRIEVAL_CMD": f"{sys.executable} -c pass"})
    assert port.timeout_seconds() == 60.0


def test_missing_command_raises_model_not_configured():
    port = CommandPersonalKnowledgePort(env={})
    with pytest.raises(ModelNotConfiguredError):
        port.search(["q"], statuses=["CONFIRMED"], limit=5)


# ---------- happy path ----------

def test_search_sends_queries_states_limit_and_parses_records():
    port = make_port(ECHO)
    records = port.search(["用户如何判断 Agent 架构？"],
                          statuses=["CONFIRMED"], limit=7)
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, PersonalKnowledgeRecord)
    assert rec.record_id == "PC-001"
    assert rec.state == "CONFIRMED"


# ---------- fail-closed ----------

def test_malformed_output_raises_retrieval_error():
    port = make_port("print('no json')")
    with pytest.raises(RetrievalError):
        port.search(["q"], statuses=["CONFIRMED"], limit=5)


def test_record_missing_field_raises_retrieval_error():
    port = make_port(
        "import sys, json\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'records': [{'record_id': 'x'}]}))")
    with pytest.raises(RetrievalError):
        port.search(["q"], statuses=["CONFIRMED"], limit=5)


def test_invalid_state_raises_retrieval_error():
    bad = dict(REC, state="TRUE")
    port = make_port("\n".join([
        "import sys, json",
        "req = json.load(sys.stdin)",
        f"print(json.dumps({{'records': [{bad!r}]}}))"]))
    with pytest.raises(RetrievalError):
        port.search(["q"], statuses=["CONFIRMED"], limit=5)


def test_explicit_empty_records_is_valid():
    port = make_port(
        "import sys, json\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'records': []}))")
    assert port.search(["q"], statuses=["CONFIRMED"], limit=5) == []


# ---------- approved cognition internal port ----------

def test_approved_cognition_port_returns_confirmed_records(tmp_path):
    store = InsightStore(tmp_path / "insight" / "state.db")
    store.upsert_approved_cognition(
        "PC-101", "cp-1", "规则→判定→生成三级决策结构",
        domain="AI.Agent", approved_at=NOW)
    port = ApprovedCognitionKnowledgePort(store)
    records = port.search(["三级决策"], statuses=["CONFIRMED"], limit=5)
    assert len(records) == 1
    assert records[0].kind == "cognition"
    assert records[0].state == "CONFIRMED"
    assert "三级决策" in records[0].text


def test_approved_cognition_port_high_recall_ordering(tmp_path):
    """High Recall：token 重叠只影响排序，绝不过滤——精确取舍是
    Selector 的职责（中文整句无分词，子串过滤会系统性漏召回）。"""
    store = InsightStore(tmp_path / "insight" / "state.db")
    store.upsert_approved_cognition("PC-201", "cp-2", "民宿定价靠转化率",
                                    domain="minsu", approved_at=NOW)
    store.upsert_approved_cognition("PC-202", "cp-3", "审计底稿要可追溯",
                                    domain="audit", approved_at=NOW)
    port = ApprovedCognitionKnowledgePort(store)
    hits = port.search(["审计"], statuses=["CONFIRMED"], limit=5)
    # 命中 token 的排前，未命中的仍返回（不丢召回）
    assert [r.record_id for r in hits] == ["PC-202", "PC-201"]


def test_approved_cognition_respects_statuses_filter(tmp_path):
    store = InsightStore(tmp_path / "insight" / "state.db")
    store.upsert_approved_cognition("PC-301", "cp-4", "认知A", domain="d",
                                    approved_at=NOW)
    port = ApprovedCognitionKnowledgePort(store)
    assert port.search(["认知A"], statuses=["TENTATIVE"], limit=5) == []


# ---------- composite ----------

def test_composite_dedupes_by_record_id(tmp_path):
    store = InsightStore(tmp_path / "insight" / "state.db")
    store.upsert_approved_cognition("PC-001", "cp-9", "共享认知",
                                    domain="d", approved_at=NOW)
    approved = ApprovedCognitionKnowledgePort(store)
    external = StaticPort([PersonalKnowledgeRecord(
        record_id="PC-001", kind="cognition", state="CONFIRMED",
        text="外部重复", source_ref="x", updated_at=NOW)])
    composite = CompositePersonalKnowledgePort([approved, external])
    records = composite.search(["共享"], statuses=["CONFIRMED"], limit=5)
    assert len(records) == 1
    assert records[0].text == "共享认知"     # 首个 port 优先


class StaticPort:
    def __init__(self, records):
        self._records = records

    def search(self, queries, *, statuses, limit):
        return [r for r in self._records if r.state in statuses][:limit]
