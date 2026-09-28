"""V3 M1: insight core contracts — frozen vocabularies, immutable records.

Unknown decision/status values are rejected at construction time
(fail-fast, no silent coercion — 实施方案 Review Focus #2：绝不伪造判定）。
Timestamps that are part of a record must be timezone-aware ISO-8601;
naive timestamps are rejected (equivalence with store-side discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ---- frozen vocabularies（设计 §7.3/§9.2/§10.5/§13.5/§17.1） ----

CANDIDATE_VALUE_TYPES = (
    "COGNITION",
    "METHOD",
    "BUSINESS_OPPORTUNITY",
    "PROJECT_IMPACT",
    "CONTRARIAN",
    "WEAK_SIGNAL",
)

DEEP_VALUE_STATES = ("DEEP_READ", "WATCH", "ARCHIVE_ONLY", "REJECT")

PERSONAL_KNOWLEDGE_STATES = (
    "CONFIRMED",
    "TENTATIVE",
    "SUPERSEDED",
    "REJECTED",
    "EXPERIMENTING",
    "ARCHIVED",
)

HUMAN_GATE_DECISIONS = ("ADOPT", "EXPERIMENT", "WATCH", "ARCHIVE", "REJECT")

CONTENT_KINDS = ("text", "pdf", "video_transcript", "web", "document")

VERIFICATION_STATUSES = ("source_only", "corpus_verified")

# Deep-Value Gate 维度取值；contradiction_value 允许 unknown（A2/Review §5：
# 无可靠个人认知证据时禁止推测，宁可多进 DEEP_READ）
GATE_DIMENSION_LEVELS = ("low", "medium", "high", "unknown")

CONTEXT_RELATION_TYPES = ("DIRECT", "CONDITIONING", "CONFLICT")

COGNITION_DELTA_TYPES = ("ADD", "REINFORCE", "REVISE", "OVERTURN", "NONE")

ACTION_TYPE_DEFINITIONS = {
    "NONE": "无需行动",
    "WATCH": "等待新的外部证据/条件触发，本人暂不主动验证（须写明触发条件，禁止编造时间点）",
    "EXPERIMENT": "主动进行一个低成本验证（须写明验证什么与判停条件）",
    "IMMEDIATE": "已有足够条件直接应用",
}

THINKING_ACTION_TYPES = ("IMMEDIATE", "EXPERIMENT", "WATCH", "NONE")


def _require_tz_aware_iso(value: str, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name}: not ISO-8601: {value!r}") from exc
    if parsed.utcoffset() is None:
        raise ValueError(
            f"{field_name}: naive timestamp not allowed: {value!r}")
    return value


def _require_choice(value, choices, field_name: str):
    if value not in choices:
        raise ValueError(
            f"{field_name}: unknown value {value!r}; "
            f"expected one of {choices}")
    return value


@dataclass(frozen=True)
class CandidateDecision:
    """Stage 1 High-Recall Filter 输出（设计 §7.5）。"""

    candidate: bool
    possible_value: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    confidence: float = 0.0
    trend_key: str | None = None

    def __post_init__(self):
        for value in self.possible_value:
            _require_choice(value, CANDIDATE_VALUE_TYPES, "possible_value")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")


@dataclass(frozen=True)
class DeepValueDecision:
    """Stage 2 Deep-Value Gate 输出（设计 §9.2）。

    不使用总分阈值：逐维度结构化判断 + 显式最终 decision。
    contradiction_value 允许 "unknown"（本机审阅意见 A2 修订）。
    """

    decision: str
    reason: str = ""
    novelty: str | None = None
    cognitive_delta_potential: str | None = None
    business_potential: str | None = None
    project_relevance: str | None = None
    transferability: str | None = None
    evidence_quality: str | None = None
    contradiction_value: str | None = None
    thinking_space: str | None = None

    def __post_init__(self):
        _require_choice(self.decision, DEEP_VALUE_STATES, "decision")
        for name in ("novelty", "cognitive_delta_potential",
                     "business_potential", "project_relevance",
                     "transferability", "evidence_quality",
                     "contradiction_value", "thinking_space"):
            value = getattr(self, name)
            if value is not None:
                _require_choice(value, GATE_DIMENSION_LEVELS, name)


@dataclass(frozen=True)
class InsightSourceView:
    """源无关输入契约（设计 §6）。

    稳定身份 = (provider, source_item_id)；content_fingerprint 变化由
    Store 侧递增 source_revision（本机审阅意见 A2/Review §4）。
    """

    provider: str
    source_item_id: str
    content_kind: str
    title: str | None
    visible_text: str
    materialized_path: str | None
    full_text_available: bool
    verification_status: str
    content_fingerprint: str
    source_revision: int = 1
    captured_at: str = ""
    author: str | None = None
    source_deleted: bool = False
    original_refs: tuple = ()

    def __post_init__(self):
        if not self.provider or not self.source_item_id:
            raise ValueError("provider/source_item_id required")
        _require_choice(self.content_kind, CONTENT_KINDS, "content_kind")
        _require_choice(self.verification_status, VERIFICATION_STATUSES,
                        "verification_status")
        if not self.content_fingerprint:
            raise ValueError("content_fingerprint required")
        if self.source_revision < 1:
            raise ValueError("source_revision must be >= 1")
        if not self.captured_at:
            raise ValueError("captured_at required")
        _require_tz_aware_iso(self.captured_at, "captured_at")


@dataclass(frozen=True)
class PersonalKnowledgeRecord:
    """Personal Knowledge Port 返回的单条记录（设计 §10）。"""

    record_id: str
    kind: str
    state: str
    text: str
    source_ref: str | None
    updated_at: str

    def __post_init__(self):
        if not self.record_id:
            raise ValueError("record_id required")
        _require_choice(self.state, PERSONAL_KNOWLEDGE_STATES, "state")
        _require_tz_aware_iso(self.updated_at, "updated_at")


@dataclass(frozen=True)
class PersonalContextRef:
    """入选 Personal Context Pack 的证据引用（设计 §10.4）。

    relation_reason 必填：每条关联必须说明"为什么它会影响当前判断"，
    禁止主题级硬关联（设计 §23.3）。
    kind/text/conflict_with 为 M5 检索器回填的可选字段（M1 契约向后
    兼容）：conflict_with 非空 = 该记录与所指记录存在 PERSONAL_CONTEXT_
    CONFLICT（设计 §10.6，冲突不静默消除）。
    """

    record_id: str
    relation_reason: str
    state: str
    source_ref: str | None = None
    kind: str | None = None
    text: str | None = None
    conflict_with: tuple[str, ...] = ()
    relation: str = "DIRECT"

    def __post_init__(self):
        if not self.record_id:
            raise ValueError("record_id required")
        if not self.relation_reason:
            raise ValueError(
                "relation_reason required — topic-level hard links are "
                "invalid (设计 §10.4)")
        _require_choice(self.state, PERSONAL_KNOWLEDGE_STATES, "state")
        _require_choice(self.relation, CONTEXT_RELATION_TYPES, "relation")


@dataclass(frozen=True)
class ContextPack:
    """已持久化的 Personal Context Pack（M6 复核①：稳定 ID 可追溯）。

    source_revision → context_pack_id → refs → card 的追溯链锚点。
    """

    pack_id: str
    insight_source_id: str
    source_revision: int
    refs: tuple = ()


@dataclass(frozen=True)
class EvidencePack:
    """Evidence Pack（设计 §12）：个人主张与源证据严格分离。

    source_parts 保留解析后的原文分片（M6：Thinking 引用全文用），
    不参与六类分离语义。
    """

    source_claims: tuple[str, ...] = ()
    source_evidence: tuple[str, ...] = ()
    source_inferences: tuple[str, ...] = ()
    unknown_variables: tuple[str, ...] = ()
    personal_context: tuple[PersonalContextRef, ...] = ()
    verification_flags: tuple[str, ...] = ()
    source_parts: tuple = ()


@dataclass(frozen=True)
class CriticResult:
    """Quality Critic 输出（设计 §18.2/§18.3；M6 复核新增 mechanism_salvage）。

    mechanism_salvage（M6 复核③冻结）：批判证据不足之后是否仍提炼出
    "即使拿掉夸张部分，剩下值得保留的机制/假设"——防止退化成
    "高级怀疑式摘要器"。
    """

    source_understanding: str = "unknown"
    critical_reasoning: str = "unknown"
    personal_connection: str = "unknown"
    cognition_delta: str = "unknown"
    own_version: str = "unknown"
    actionability: str = "unknown"
    business_rigor: str = "unknown"
    traceability: str = "unknown"
    mechanism_salvage: str = "unknown"
    genericity_detected: bool = False
    revision_required: bool = False
    revision_instructions: tuple[str, ...] = ()
