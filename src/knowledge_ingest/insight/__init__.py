"""V3: Personal Insight Engine — source-independent derivation layer.

设计边界（§4.2/§27）：Insight Engine 不属于 Telegram 事实层；它消费
派生副本（InsightSourceView），产出 Deep Insight Card 与 Cognitive
Change Proposal，长期认知写入必须经过 Human Gate。
Telegram 只提供最薄 Adapter（telegram/insight_adapter.py），核心
不进入 telegram/ 内核。
"""

from knowledge_ingest.insight.models import (
    CANDIDATE_VALUE_TYPES,
    COGNITION_DELTA_TYPES,
    DEEP_VALUE_STATES,
    HUMAN_GATE_DECISIONS,
    PERSONAL_KNOWLEDGE_STATES,
    THINKING_ACTION_TYPES,
    CandidateDecision,
    CriticResult,
    DeepValueDecision,
    EvidencePack,
    InsightSourceView,
    PersonalContextRef,
    PersonalKnowledgeRecord,
)
from knowledge_ingest.insight.paths import insight_root
from knowledge_ingest.insight.store import (
    INSIGHT_SCHEMA_VERSION,
    InsightStore,
    UnsupportedInsightSchemaError,
)

__all__ = [
    "CANDIDATE_VALUE_TYPES",
    "COGNITION_DELTA_TYPES",
    "DEEP_VALUE_STATES",
    "HUMAN_GATE_DECISIONS",
    "INSIGHT_SCHEMA_VERSION",
    "PERSONAL_KNOWLEDGE_STATES",
    "THINKING_ACTION_TYPES",
    "CandidateDecision",
    "CriticResult",
    "DeepValueDecision",
    "EvidencePack",
    "InsightSourceView",
    "InsightStore",
    "PersonalContextRef",
    "PersonalKnowledgeRecord",
    "UnsupportedInsightSchemaError",
    "insight_root",
]
