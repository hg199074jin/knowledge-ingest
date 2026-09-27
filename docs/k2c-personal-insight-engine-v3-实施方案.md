# Personal Insight Engine V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏 Telegram Source V2/V2.2 生产链路的前提下，为 `knowledge-ingest` 新增一个源无关的 Personal Insight Engine，使高价值外部内容经过两级筛选、个人认知检索、深度推理、质量复核和 Human Gate 后，形成独立 Deep Insight Card 与可审计的 Cognitive Change Proposal。

**Architecture:** V3 作为 `src/knowledge_ingest/insight/` 下的源无关旁路子系统；Telegram 只提供一个薄 `InsightSourceView` Adapter。Insight 自有 SQLite 事实库、模型端口、个人知识检索端口、卡片/Proposal 投影与 Human Gate；V2 `telegram/state.db`、knowledge-ingest Job OS 和 K2C Registry 的既有职责不改变。初始生产方式为 **shadow mode**：V3 读取 V2 派生事实，不反向修改 Telegram 状态，也不自动写入长期认知。

**Tech Stack:** Python 3.12、stdlib `sqlite3` / `subprocess` / `json` / `dataclasses`、现有 PyYAML / Pydantic（仅在现有模式确有收益时使用）、pytest / pytest-asyncio、ruff；模型与个人知识检索均通过可注入 CLI/Port，V3.0 不新增模型 SDK、向量数据库或云服务硬依赖。

**Spec:** `docs/k2c-personal-insight-engine-v3-design.md`（执行前必须把用户已批准的设计文档原样放入此路径；不得由实现 Agent 自行重写设计）

## Global Constraints

- 当前远端基线：`main @ 7d172221e8995328d9995288746de228e48c695b`；若本机 HEAD 已前进，以本机真实 `main/origin/main` 为准并在执行记录说明，不得强行 reset。
- 当前已知回归基线：562 tests；实施前以本机当前分支实际测试结果为准。
- 不推翻、不重写 Telegram Source V2/V2.2：Source Registry、SQLite Source Facts、Watcher、learned-rules、增量恢复、K2C handoff 均保持既有职责。
- V3 Insight Store 与 `telegram/state.db` 分离；V3 不向 Telegram 事实库写入 Insight 状态。
- 第一层 High-Recall Filter 以 Recall 优先；不得从“无价值”判决自动蒸馏永久 value-SKIP 规则。
- Deep-Value Gate 的最终状态固定为 `DEEP_READ | WATCH | ARCHIVE_ONLY | REJECT`。
- Personal Retrieval 允许 `NO_RELEVANT_PERSONAL_CONTEXT`；禁止为“个性化”硬关联。
- 旧认知只是 Personal Claim，不是事实；Thinking Engine 必须允许新证据修正或推翻旧认知。
- Deep Insight Card 是主产物；日报/周报只是索引和待办入口。
- Human Gate 固定决策：`ADOPT | EXPERIMENT | WATCH | ARCHIVE | REJECT`。
- Human Gate 前绝不自动修改长期认知。
- 强模型/检索模型不可用时 fail closed：保留待恢复状态，不得降级成普通摘要并标记为 Deep Insight Card。
- 默认 Critic 修订上限 = 1；不得无限自我循环。
- V3.0 不要求自动联网事实核验；未外部验证的关键事实必须保留 `source_only / VERIFY_REQUIRED` 语义。
- 生产数据仍遵守 ORICO fail-closed；测试必须通过 env/tmp_path 做 hermetic 隔离，不依赖真实 `/Volumes/ORICO`。
- 不在日志、SQLite audit 字段或模型错误信息中写入整份个人长期知识正文。
- 所有任务按 TDD：先红、再最小实现、再绿、再提交；不得跨 Task “顺手重构”。

## Local Adaptation Gate — ZCode Plan Mode 必做、但不实施

在开始 Task 1 前，本地 ZCode 必须先做一次**针对性本机审阅**，输出“本机适配后的实施计划”，然后 STOP 等待用户批准。

只检查以下与 V3 直接相关的事实：

1. 先读取全局 `AGENTS.md`、项目级 `AGENTS.md`（如有）、README、当前 V2 设计/实施/验收和本 V3 设计/实施文档；对其中稳定事实不要重复全量探测。
2. 核实当前 `HEAD`、`main/origin/main`、working tree；不得擅自清理用户改动。
3. 核实 `src/knowledge_ingest/cli.py`、`telegram/event_store.py`、`telegram/items.py`、`telegram/handoff.py`、`telegram/notification.py` 当前真实接口与本计划是否漂移。
4. 核实当前测试目录命名/fixture 习惯；本计划中的新测试文件名可按现有惯例做**命名级**调整，但不可改变测试语义。
5. 核实 `AppConfig.pipeline_root` 的真实生产路径、`KI_TELEGRAM_ORICO_ROOT` 现有测试覆写方式，以及 V3 新数据根应该如何复用相同 fail-closed 纪律。
6. **重点核实 PDF/video 在当前 Job / Corpus 中如何找到可读全文**：只追踪现有 `job.yaml` / handoff / verified corpus 的真实字段与路径，不重新扫描整个环境。将结果映射到本计划 `ContentResolverPort`。
7. **重点核实个人长期知识现有真实来源**（例如现有 Markdown/Obsidian/其他已知知识目录或已有检索工具）：只识别 V3 可读的稳定入口，不做全盘文件扫描。将结果映射到 `PersonalKnowledgePort`；如没有稳定入口，保留 `CommandPersonalKnowledgePort` 作为 V3.0 首个正式 adapter。
8. 核实本机已有可调用的模型 CLI/worker 能否接受 stdin 并输出 JSON；不要购买/安装新模型，不写入任何 API Key。
9. 只有实施到 digest-agent / launchd Milestone 时，才针对性验证对应 LaunchAgent 路径与权限；不要在审阅阶段做无关全量 launchd 扫描。
10. 输出：`READY / READY WITH ADJUSTMENTS / BLOCKED`，列出**精确修改项**，然后 STOP。不得开始 Task 1。

## Review Focus

以下 5 类风险最容易让“看起来能跑”的 V3 实际伤害用户体验；对应测试已分配到后续 Task：

1. **Source 内容暂时不可读**：PDF/video 尚未有 verified corpus 时应进入可恢复等待态，不得 REJECT 或生成伪深读卡。→ Task 2。
2. **模型输出非法/模型不可用**：Candidate、Value Gate、Thinker、Critic 任一阶段失败都必须 fail closed 且可恢复，不能误判为无价值。→ Task 3 / Task 6。
3. **Personal Retrieval 硬关联或陈旧认知泄漏**：`SUPERSEDED/REJECTED` 不得默认作为当前认知；允许零相关结果。→ Task 5。
4. **Critic 失败后无限循环或“普通摘要冒充深度卡”**：最多一次修订；仍不合格则保持 blocked/needs_review。→ Task 6。
5. **Human Gate 幂等与冲突**：同一 Proposal 重复相同决策必须幂等，不同决策必须显式冲突，绝不静默覆盖。→ Task 7。

---

# Milestone Map

| Milestone | 目标 | 结束时可独立验收 |
|---|---|---|
| M1 | Core contracts + Insight Store | 独立 SQLite 事实层可迁移、可幂等读写 |
| M2 | SourceView + Telegram shadow ingestion | V2 Source Item 可安全投影到 Insight，内容不足可等待 |
| M3 | Model Port + Insight Budget | 各推理阶段有统一 JSON 通道、预算、fail-closed |
| M4 | Two-Stage Value Filter + WATCH/Trend | High Recall 与 Deep Value 两层可独立跑 |
| M5 | Personal Reasoning Retrieval | 能检索“影响判断的个人证据”，支持空结果/状态过滤 |
| M6 | Thinking + Critic + Card | 可生成并复核 Deep Insight Card |
| M7 | Proposal + Human Gate | 认知变更可人工 ADOPT/EXPERIMENT/WATCH/ARCHIVE/REJECT |
| M8 | CLI + Digest + Doctor + Shadow Agent | 可日常运维、批量处理、旁路持续运行 |
| M9 | Golden Set + Real Acceptance + Docs | 用历史高质量样本验证 V3 质量，并形成正式验收记录 |

---

### Task 1: Core Contracts and Independent Insight Store

**Files:**
- Create: `src/knowledge_ingest/insight/__init__.py`
- Create: `src/knowledge_ingest/insight/models.py`
- Create: `src/knowledge_ingest/insight/paths.py`
- Create: `src/knowledge_ingest/insight/store.py`
- Test: `tests/unit/test_insight_models.py`
- Test: `tests/unit/test_insight_store.py`

**Interfaces:**
- Produces:
  - `InsightSourceView`
  - `CandidateDecision`
  - `DeepValueDecision`
  - `PersonalKnowledgeRecord`
  - `PersonalContextRef`
  - `EvidencePack`
  - `CriticResult`
  - `InsightStore`
  - `insight_root(config: AppConfig) -> Path`
- Consumes: only existing `AppConfig.pipeline_root`; no Telegram imports.

- [ ] **Step 1: Write failing model contract tests**

Assert exact enum/domain values:

```python
assert CandidateDecision(candidate=True, possible_value=("COGNITION",),
                         reasons=("new mechanism",), confidence=0.6).candidate
assert DeepValueDecision("DEEP_READ", ...).decision == "DEEP_READ"
```

Tests must reject unknown decision/status values and naïve timestamps where timestamps are part of a record.

- [ ] **Step 2: Run the model tests and confirm RED**

Run:

```bash
uv run pytest tests/unit/test_insight_models.py -q
```

Expected: FAIL because `knowledge_ingest.insight.models` does not exist.

- [ ] **Step 3: Implement `models.py` with immutable dataclasses and exact enums**

Required frozen values:

```text
Candidate value types:
COGNITION
METHOD
BUSINESS_OPPORTUNITY
PROJECT_IMPACT
CONTRARIAN
WEAK_SIGNAL

Deep value states:
DEEP_READ
WATCH
ARCHIVE_ONLY
REJECT

Personal knowledge states:
CONFIRMED
TENTATIVE
SUPERSEDED
REJECTED
EXPERIMENTING
ARCHIVED

Human gate decisions:
ADOPT
EXPERIMENT
WATCH
ARCHIVE
REJECT
```

`InsightSourceView` must include stable identity, provider/source refs, content kind, title, visible text, optional local/materialized path, `full_text_available`, `verification_status`, and provenance fields needed for traceability.

- [ ] **Step 4: Run model tests and confirm GREEN**

Run:

```bash
uv run pytest tests/unit/test_insight_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing InsightStore schema/CRUD tests**

Required DB location under a supplied root:

```text
<root>/insight/state.db
```

Schema version starts at:

```text
INSIGHT_SCHEMA_VERSION = 1
```

Required tables:

```text
insight_sources
insight_candidates
insight_runs
insight_context_refs
insight_cards
cognition_proposals
watch_signals
trend_clusters
approved_cognition
insight_ai_budget
insight_digest_state
```

Required store behavior:
- WAL, foreign_keys ON, busy_timeout 5000ms;
- higher schema version → fail-fast;
- source registration idempotent by `(provider, source_item_id)`;
- repeated same decision idempotent;
- card/proposal/source references retain provenance;
- no Telegram DB mutation.

- [ ] **Step 6: Run store tests and confirm RED**

```bash
uv run pytest tests/unit/test_insight_store.py -q
```

Expected: FAIL because store is not implemented.

- [ ] **Step 7: Implement `InsightStore` and `paths.py`**

Signatures required:

```python
def insight_root(config: AppConfig) -> Path: ...

class InsightStore:
    def __init__(self, db_path: str | Path): ...
    def register_source_view(self, view: InsightSourceView) -> str: ...
    def get_source(self, insight_source_id: str): ...
    def record_candidate(self, insight_source_id: str,
                         decision: CandidateDecision) -> None: ...
    def record_value_gate(self, insight_source_id: str,
                          decision: DeepValueDecision) -> None: ...
    def create_run(self, insight_source_id: str, stage: str,
                   status: str, *, error_code: str | None = None) -> int: ...
    def finish_run(self, run_id: int, status: str,
                   *, output_ref: str | None = None,
                   error_code: str | None = None) -> None: ...
```

Do not store full Personal Context bodies in generic run/error fields.

- [ ] **Step 8: Run M1 tests**

```bash
uv run pytest tests/unit/test_insight_models.py tests/unit/test_insight_store.py -q
uv run ruff check src/knowledge_ingest/insight tests/unit/test_insight_*.py
```

Expected: all PASS / clean.

- [ ] **Step 9: Commit M1**

```bash
git add src/knowledge_ingest/insight tests/unit/test_insight_models.py \
        tests/unit/test_insight_store.py
git commit -m "feat(insight): add core contracts and independent store"
```

---

### Task 2: SourceView Adapter and Telegram Shadow Ingestion

**Files:**
- Create: `src/knowledge_ingest/insight/source_view.py`
- Create: `src/knowledge_ingest/insight/content_resolver.py`
- Create: `src/knowledge_ingest/telegram/insight_adapter.py`
- Test: `tests/unit/test_insight_source_view.py`
- Test: `tests/unit/test_telegram_insight_adapter.py`

**Interfaces:**
- Consumes:
  - `InsightSourceView`
  - existing `TelegramEventStore`
  - existing Source Item/message/download/job facts
- Produces:
  - `ContentResolverPort.resolve(view: InsightSourceView) -> ResolvedContent`
  - `TelegramInsightAdapter.iter_views(...)`
  - `TelegramInsightAdapter.to_view(item_id: str) -> InsightSourceView | None`

- [ ] **Step 1: Write failing Telegram projection tests**

Cases:
1. finalized text + materialized Markdown → `full_text_available=True`;
2. `skipped_noise`, deleted-only, `PENDING_RESOURCE`, unsupported file → not eligible for Insight source view;
3. PDF/video with visible metadata but no verified readable body → source view exists with `full_text_available=False`;
4. adapter read must not modify Telegram rows;
5. same Telegram item rescanned → same `insight_source_id`.

- [ ] **Step 2: Run and confirm RED**

```bash
uv run pytest tests/unit/test_insight_source_view.py \
                  tests/unit/test_telegram_insight_adapter.py -q
```

- [ ] **Step 3: Implement source-neutral content resolver contracts**

Required:

```python
@dataclass(frozen=True)
class ResolvedContent:
    text: str
    verification_status: str
    content_ref: str | None

class ContentResolverPort(Protocol):
    def resolve(self, view: InsightSourceView) -> ResolvedContent | None: ...
```

Initial deterministic resolver:
- text materialization → read UTF-8 Markdown;
- unavailable PDF/video corpus → return `None`, not empty string.

The ZCode local adaptation review must map the current verified Corpus path into a concrete resolver without inventing paths.

- [ ] **Step 4: Implement `TelegramInsightAdapter`**

Eligibility is downstream of V2 intake; do **not** reimplement noise/interest policy.

For temporarily unreadable documents:
- register source view;
- state remains recoverable (`WAITING_CONTENT`);
- do not run Candidate Filter yet.

- [ ] **Step 5: Add the Review Focus test for unreadable content**

Assert:

```text
PDF/video without readable verified content
→ source retained
→ no REJECT
→ no Deep Insight Card
→ rescan after content appears can proceed
```

- [ ] **Step 6: Run M2 tests + existing Telegram focused tests**

```bash
uv run pytest tests/unit/test_insight_source_view.py \
                  tests/unit/test_telegram_insight_adapter.py -q
uv run pytest tests/unit/test_telegram_learned.py tests/unit/test_tg7_migration.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit M2**

```bash
git add src/knowledge_ingest/insight/source_view.py \
        src/knowledge_ingest/insight/content_resolver.py \
        src/knowledge_ingest/telegram/insight_adapter.py \
        tests/unit/test_insight_source_view.py \
        tests/unit/test_telegram_insight_adapter.py
git commit -m "feat(insight): add telegram shadow source adapter"
```

---

### Task 3: Injectable Model Port and Separate Insight AI Budget

**Files:**
- Create: `src/knowledge_ingest/insight/model_port.py`
- Create: `src/knowledge_ingest/insight/budget.py`
- Test: `tests/unit/test_insight_model_port.py`
- Test: `tests/unit/test_insight_budget.py`

**Interfaces:**
- Produces:
  - `InsightModelPort.run(stage: str, payload: dict) -> dict`
  - `CommandInsightModelPort`
  - `InsightAIBudgetGuard.acquire(stage, request_id)`
  - `InsightAIBudgetGuard.outcome(permit_id, result)`
- Consumes: `InsightStore.insight_ai_budget`.

- [ ] **Step 1: Write failing command-port tests**

Freeze env contract:

```text
KI_INSIGHT_MODEL_CMD
KI_INSIGHT_MODEL_TIMEOUT       default 180
KI_INSIGHT_MODEL_CWD           optional

Stage-specific optional override:
KI_INSIGHT_CANDIDATE_CMD
KI_INSIGHT_VALUE_CMD
KI_INSIGHT_RETRIEVAL_PLAN_CMD
KI_INSIGHT_RETRIEVAL_SELECT_CMD
KI_INSIGHT_THINK_CMD
KI_INSIGHT_CRITIC_CMD
```

Stage-specific command falls back to `KI_INSIGHT_MODEL_CMD`.

Input on stdin is one JSON object:

```json
{"schema_version":1,"stage":"candidate","payload":{...}}
```

Output must contain one complete JSON object. Non-zero exit, timeout, empty stdout, malformed JSON → typed model error; never fabricate a decision.

- [ ] **Step 2: Run and confirm RED**

```bash
uv run pytest tests/unit/test_insight_model_port.py -q
```

- [ ] **Step 3: Implement `CommandInsightModelPort`**

Reuse the current CLI JSON-extraction behavior semantically, but put V3 logic in `insight/model_port.py`; do not import private `_extract_json_text` from `cli.py`.

Sanitize exception text to max 200 chars and never include stdin payload.

- [ ] **Step 4: Write failing separate-budget tests**

Budget dimensions:

```text
candidate_filter
deep_value_gate
retrieval_query_plan
retrieval_select
thinking
critic
```

Required behavior:
- request_id idempotence;
- counters per stage;
- empty/rate-limit breaker semantics;
- reset by stage;
- Insight budget never reads or writes `source_ai_budget`.

- [ ] **Step 5: Implement `InsightAIBudgetGuard`**

Exact outcome values:

```text
success
empty
rate_limit
error
```

Model unavailable/budget denied maps to recoverable stage state, never `REJECT`.

- [ ] **Step 6: Add Review Focus tests for model failure**

Assert Candidate/Value/Thinking/Critic model failure:
- records a failed/blocked run;
- leaves source retryable;
- does not create false `REJECT`;
- does not create a fake card.

- [ ] **Step 7: Run M3 tests**

```bash
uv run pytest tests/unit/test_insight_model_port.py \
                  tests/unit/test_insight_budget.py -q
uv run ruff check src/knowledge_ingest/insight
```

- [ ] **Step 8: Commit M3**

```bash
git add src/knowledge_ingest/insight/model_port.py \
        src/knowledge_ingest/insight/budget.py \
        tests/unit/test_insight_model_port.py \
        tests/unit/test_insight_budget.py
git commit -m "feat(insight): add model port and isolated ai budget"
```

---

### Task 4: Two-Stage Value Filter, WATCH, and Weak-Signal Trend Trigger

**Files:**
- Create: `src/knowledge_ingest/insight/candidate_filter.py`
- Create: `src/knowledge_ingest/insight/value_gate.py`
- Create: `src/knowledge_ingest/insight/trend.py`
- Test: `tests/unit/test_insight_candidate_filter.py`
- Test: `tests/unit/test_insight_value_gate.py`
- Test: `tests/unit/test_insight_trend.py`

**Interfaces:**
- Consumes: `InsightModelPort`, `InsightAIBudgetGuard`, `InsightStore`, readable `InsightSourceView`.
- Produces:
  - `HighRecallCandidateFilter.evaluate(view) -> CandidateDecision`
  - `DeepValueGate.evaluate(view, candidate, personal_preview) -> DeepValueDecision`
  - `TrendAggregator.record_watch_signal(...)`
  - `TrendAggregator.ready_clusters(...)`

- [ ] **Step 1: Write Candidate Filter failing tests**

Required structured output fields:

```json
{
  "candidate": true,
  "possible_value": ["COGNITION","BUSINESS_OPPORTUNITY"],
  "reasons": ["..."],
  "confidence": 0.61,
  "trend_key": "optional-normalized-concept"
}
```

Tests:
- low confidence + plausible cognitive value still passes;
- already-known broad topic does not imply rejection;
- obvious non-value can return false;
- malformed model output → retryable failure;
- no “learned value-SKIP” table or auto-rule creation occurs.

- [ ] **Step 2: Implement `HighRecallCandidateFilter` and pass tests**

Prompt contract must explicitly optimize false negatives over false positives.

- [ ] **Step 3: Write Deep-Value Gate failing tests**

Required multidimensional fields:

```text
novelty
cognitive_delta_potential
business_potential
project_relevance
transferability
evidence_quality
contradiction_value
thinking_space
decision
reason
```

Cases:
- poorly written post with valuable transaction structure → `DEEP_READ`;
- plausible claim but insufficient evidence → `WATCH`;
- interesting but no personal/cognitive increment → `ARCHIVE_ONLY`;
- clearly empty/duplicative after full read → `REJECT`;
- “wrong but instructive cognitive trap” may still → `DEEP_READ`.

- [ ] **Step 4: Implement `DeepValueGate`**

Do not use one numeric total score. Parse named dimensions and explicit final decision.

- [ ] **Step 5: Write and implement TrendAggregator tests**

V3.0 deterministic trigger:

```text
default window = 30 days
default threshold = 3 WATCH signals with same normalized trend_key
```

On threshold:
- create/update one trend cluster;
- emit one re-evaluation candidate;
- repeated scan is idempotent;
- cluster does not claim source independence merely because count >= 3.

Allow config/env override later, but freeze defaults in tests.

- [ ] **Step 6: Run M4**

```bash
uv run pytest tests/unit/test_insight_candidate_filter.py \
                  tests/unit/test_insight_value_gate.py \
                  tests/unit/test_insight_trend.py -q
```

- [ ] **Step 7: Commit M4**

```bash
git add src/knowledge_ingest/insight/candidate_filter.py \
        src/knowledge_ingest/insight/value_gate.py \
        src/knowledge_ingest/insight/trend.py \
        tests/unit/test_insight_candidate_filter.py \
        tests/unit/test_insight_value_gate.py \
        tests/unit/test_insight_trend.py
git commit -m "feat(insight): add two-stage value filter and trend watch"
```

---

### Task 5: Personal Reasoning Retrieval and Context Pack

**Files:**
- Create: `src/knowledge_ingest/insight/knowledge_port.py`
- Create: `src/knowledge_ingest/insight/retrieval.py`
- Create: `src/knowledge_ingest/insight/context_pack.py`
- Test: `tests/unit/test_insight_knowledge_port.py`
- Test: `tests/unit/test_insight_retrieval.py`
- Test: `tests/unit/test_insight_context_pack.py`

**Interfaces:**
- Produces:
  - `PersonalKnowledgePort.search(queries, *, statuses, limit) -> list[PersonalKnowledgeRecord]`
  - `CommandPersonalKnowledgePort`
  - `ApprovedCognitionKnowledgePort`
  - `CompositePersonalKnowledgePort`
  - `PersonalReasoningRetriever.retrieve(view, content) -> tuple[PersonalContextRef, ...]`
  - `render_context_pack(refs) -> str`
- Consumes:
  - model stages `retrieval_query_plan`, `retrieval_select`
  - `InsightStore.approved_cognition`
  - optional external personal knowledge command.

- [ ] **Step 1: Write failing external knowledge-port tests**

Freeze env:

```text
KI_INSIGHT_RETRIEVAL_CMD
KI_INSIGHT_RETRIEVAL_TIMEOUT   default 60
KI_INSIGHT_RETRIEVAL_CWD       optional
```

stdin request:

```json
{
  "schema_version":1,
  "queries":["..."],
  "allowed_states":["CONFIRMED","TENTATIVE","EXPERIMENTING"],
  "limit":20
}
```

stdout records must contain:

```text
record_id
kind
state
text
source_ref
updated_at
```

Malformed output → typed retrieval error, not fake empty success.

- [ ] **Step 2: Implement `CommandPersonalKnowledgePort` and internal approved-cognition adapter**

`ApprovedCognitionKnowledgePort` exposes only Human-Gate-approved records from Insight Store.

`CompositePersonalKnowledgePort` deduplicates by stable `record_id`.

- [ ] **Step 3: Write failing Query Planner / Selector tests**

Planner must produce 3–5 judgment questions, not topic keywords.

Selector receives retrieved candidates and may select zero.

Each selected context ref must include:

```text
record_id
relation_reason
state
source_ref
```

- [ ] **Step 4: Implement `PersonalReasoningRetriever`**

Default status policy:
- include: `CONFIRMED`, `TENTATIVE`, `EXPERIMENTING`;
- exclude by default: `SUPERSEDED`, `REJECTED`, `ARCHIVED`;
- query `SUPERSEDED/REJECTED` only when planner explicitly sets `include_history=true`.

Hard caps:
- max planner queries: 5;
- max external candidate snippets before selector: 20;
- max selected context refs: 12.

These are context-safety limits, not value scores.

- [ ] **Step 5: Add Review Focus tests**

Cases:
1. no relevant records → explicit empty tuple / `NO_RELEVANT_PERSONAL_CONTEXT`;
2. only topic similarity with no judgment impact → selector chooses none;
3. `SUPERSEDED` and `REJECTED` do not leak into current context by default;
4. conflicting valid cognitions both survive with conflict marker;
5. selected ref must explain why it changes/conditions current judgment.

- [ ] **Step 6: Implement `render_context_pack`**

Required sections:

```text
Confirmed Cognition
Active/Tentative Cognition
Active Decisions / Projects
Discarded / Historical Context (only when requested)
Experiment History
Conflicts
```

Do not render empty sections as fake content; render one `NO_RELEVANT_PERSONAL_CONTEXT` marker when no refs exist.

- [ ] **Step 7: Run M5**

```bash
uv run pytest tests/unit/test_insight_knowledge_port.py \
                  tests/unit/test_insight_retrieval.py \
                  tests/unit/test_insight_context_pack.py -q
```

- [ ] **Step 8: Commit M5**

```bash
git add src/knowledge_ingest/insight/knowledge_port.py \
        src/knowledge_ingest/insight/retrieval.py \
        src/knowledge_ingest/insight/context_pack.py \
        tests/unit/test_insight_knowledge_port.py \
        tests/unit/test_insight_retrieval.py \
        tests/unit/test_insight_context_pack.py
git commit -m "feat(insight): add personal reasoning retrieval"
```

---

### Task 6: Evidence Pack, Personal Thinking Engine, Quality Critic, and Deep Insight Card

**Files:**
- Create: `src/knowledge_ingest/insight/evidence.py`
- Create: `src/knowledge_ingest/insight/thinker.py`
- Create: `src/knowledge_ingest/insight/critic.py`
- Create: `src/knowledge_ingest/insight/cards.py`
- Test: `tests/unit/test_insight_evidence.py`
- Test: `tests/unit/test_insight_thinker.py`
- Test: `tests/unit/test_insight_critic.py`
- Test: `tests/unit/test_insight_cards.py`

**Interfaces:**
- Produces:
  - `build_evidence_pack(view, resolved_content, context_refs) -> EvidencePack`
  - `PersonalThinkingEngine.think(pack) -> dict`
  - `QualityCritic.review(pack, draft) -> CriticResult`
  - `DeepInsightCardWriter.write(...) -> Path`
- Consumes: `InsightModelPort`, `InsightStore`.

- [ ] **Step 1: Write Evidence Pack failing tests**

Pack must explicitly separate:

```text
source_claims
source_evidence
source_inferences
unknown_variables
personal_context
verification_flags
```

No personal claim may be silently promoted into source evidence.

- [ ] **Step 2: Implement Evidence Pack builder**

For V3.0, claim/evidence extraction is a structured model stage only if needed; if SourceView already contains a prior structured extraction, reuse it. Whatever route is chosen by local adaptation, the persisted pack must keep the categories separate.

- [ ] **Step 3: Write Thinking Engine failing tests**

Freeze output contract as structured content, not Markdown-only:

```text
bottom_line
source_understanding
mechanism
challenge
personal_connections[]
project_impacts[]
business_opportunity | null
own_version
cognition_delta
actions[]
human_gate_recommendation
final_verdict
```

Required semantics:
- max 3 actions;
- actions must be one of `IMMEDIATE | EXPERIMENT | WATCH | NONE`;
- cognition delta one of `ADD | REINFORCE | REVISE | OVERTURN | NONE`;
- business opportunity must separate `market_opportunity` and `personal_opportunity`;
- may return `ARCHIVE` or `REJECT` after full thinking.

- [ ] **Step 4: Implement `PersonalThinkingEngine` Thinking Protocol**

Prompt must preserve the ordered protocol:

```text
Understand
→ Challenge
→ Connect
→ Reconstruct
→ Decide
```

Explicitly instruct:
- critique is required as a check, not as a forced negative conclusion;
- old cognition is revisable;
- “与你关注 X 相关” without causal relation is invalid;
- `own_version` must survive if original article wording is forgotten.

- [ ] **Step 5: Write Critic failing tests**

Critic result fields:

```text
source_understanding
critical_reasoning
personal_connection
cognition_delta
own_version
actionability
business_rigor
traceability
genericity_detected
revision_required
revision_instructions[]
```

Tests must cover:
- generic “对你有启发” draft → fail;
- hard CPA/AI association → fail;
- own_version only paraphrases author → fail;
- no delta but honest `NONE` → may pass;
- well-grounded connection → pass.

- [ ] **Step 6: Implement Critic + one-revision orchestrator**

Required orchestration:

```python
draft = thinker.think(pack)
review = critic.review(pack, draft)
if review.revision_required:
    draft = thinker.revise(pack, draft, review.revision_instructions)
    final_review = critic.review(pack, draft)
```

Hard stop after one revision.

If final review still fails:
- state = `NEEDS_REVIEW`;
- no `quality_status=passed`;
- do not create Cognition Proposal.

- [ ] **Step 7: Add Review Focus test for infinite-loop prevention and model failure**

Assert:
- exactly max two thinker calls (initial + one revision);
- Critic failure itself yields recoverable blocked state;
- no fallback “summary card”.

- [ ] **Step 8: Write and implement Deep Insight Card rendering**

Markdown metadata must include:

```text
source
source_item_id
topics
value_type
cognition_delta
action_state
related_knowledge
quality_status
human_gate
```

Display is adaptive:
- no forced Business section when null;
- no forced “我不同意什么” section when challenge finds no substantive flaw;
- `own_version` and cognition delta must always be visible for passed cards.

Card path:

```text
<insight_root>/cards/YYYY/MM/DD/insight-<id>-<slug>.md
```

Atomic temp + replace write.

- [ ] **Step 9: Run M6**

```bash
uv run pytest tests/unit/test_insight_evidence.py \
                  tests/unit/test_insight_thinker.py \
                  tests/unit/test_insight_critic.py \
                  tests/unit/test_insight_cards.py -q
```

- [ ] **Step 10: Commit M6**

```bash
git add src/knowledge_ingest/insight/evidence.py \
        src/knowledge_ingest/insight/thinker.py \
        src/knowledge_ingest/insight/critic.py \
        src/knowledge_ingest/insight/cards.py \
        tests/unit/test_insight_evidence.py \
        tests/unit/test_insight_thinker.py \
        tests/unit/test_insight_critic.py \
        tests/unit/test_insight_cards.py
git commit -m "feat(insight): add thinking engine critic and insight cards"
```

---

### Task 7: Cognitive Change Proposal and Human Gate

**Files:**
- Create: `src/knowledge_ingest/insight/proposals.py`
- Create: `src/knowledge_ingest/insight/knowledge_writer.py`
- Test: `tests/unit/test_insight_proposals.py`
- Test: `tests/unit/test_insight_human_gate.py`

**Interfaces:**
- Produces:
  - `build_cognition_proposal(card) -> CognitionProposal | None`
  - `InsightHumanGate.resolve(proposal_id, decision) -> GateResolution`
  - `ApprovedKnowledgeWriter.write(proposal) -> Path`
- Consumes: passed cards only.

- [ ] **Step 1: Write proposal creation failing tests**

Rules:
- `cognition_delta == NONE` → no cognition proposal;
- failed/needs-review card → no proposal;
- proposal preserves old cognition ref(s), proposed cognition, reason and evidence refs;
- initial state `pending`.

- [ ] **Step 2: Implement proposal builder**

No external knowledge mutation here.

- [ ] **Step 3: Write Human Gate failing tests**

Decision semantics:

```text
ADOPT      → create approved cognition projection
EXPERIMENT → create experiment stub, no confirmed cognition
WATCH      → create/update watch signal, no confirmed cognition
ARCHIVE    → close proposal, no cognition mutation
REJECT     → close proposal, no cognition mutation
```

Approved projection path:

```text
<insight_root>/knowledge/confirmed/cognition-<proposal-id>.md
```

Experiment path:

```text
<insight_root>/experiments/experiment-<proposal-id>.md
```

- [ ] **Step 4: Implement `ApprovedKnowledgeWriter` and gate**

Writing must be atomic and provenance-rich.

`ADOPT` also inserts one `approved_cognition` record so future Personal Retrieval can use the accepted cognition immediately.

- [ ] **Step 5: Add Review Focus idempotence/conflict tests**

Assert:
- same decision twice → idempotent success;
- different second decision → explicit `GateConflictError`;
- model code has no method that can call `resolve()` on behalf of user;
- proposal approval time and decision source are persisted.

- [ ] **Step 6: Run M7**

```bash
uv run pytest tests/unit/test_insight_proposals.py \
                  tests/unit/test_insight_human_gate.py -q
```

- [ ] **Step 7: Commit M7**

```bash
git add src/knowledge_ingest/insight/proposals.py \
        src/knowledge_ingest/insight/knowledge_writer.py \
        tests/unit/test_insight_proposals.py \
        tests/unit/test_insight_human_gate.py
git commit -m "feat(insight): add cognition proposals and human gate"
```

---

### Task 8: Orchestration, CLI, Digest, Doctor, and Shadow Agent

**Files:**
- Create: `src/knowledge_ingest/insight/service.py`
- Create: `src/knowledge_ingest/insight/digest.py`
- Create: `src/knowledge_ingest/insight/doctor.py`
- Create: `src/knowledge_ingest/insight/shadow_agent.py`
- Modify: `src/knowledge_ingest/cli.py`
- Test: `tests/unit/test_insight_service.py`
- Test: `tests/unit/test_insight_cli.py`
- Test: `tests/unit/test_insight_digest.py`
- Test: `tests/unit/test_insight_doctor.py`
- Test: `tests/unit/test_insight_shadow_agent.py`

**Interfaces:**
- Consumes all prior milestone components.
- Produces end-to-end shadow orchestration and operator surface.

- [ ] **Step 1: Write service orchestration failing tests**

`InsightService.process(source_view)` must enforce:

```text
WAITING_CONTENT
→ Candidate
→ Candidate false → terminal archive/skip-for-insight
→ Candidate true → Deep Value
→ WATCH / ARCHIVE_ONLY / REJECT
→ DEEP_READ → Retrieval → Evidence → Thinker → Critic → Card → Proposal
```

Every stage must be restart-safe using InsightStore status, not “rerun everything”.

- [ ] **Step 2: Implement `InsightService`**

Idempotence:
- passed stage is not recharged/recalled on restart;
- blocked stage resumes from that stage;
- card/proposal creation is one-time by stable source + version fingerprint.

Source edits:
- if source content fingerprint changes before passed card → invalidate downstream Insight run and recompute;
- after Human Gate `ADOPT`, source edit must create a new review path, never silently rewrite accepted cognition.

- [ ] **Step 3: Add `insight` CLI parser and command handlers**

Required initial commands:

```bash
knowledge-ingest insight scan --provider telegram [--limit N]
knowledge-ingest insight status
knowledge-ingest insight cards list
knowledge-ingest insight card show ID
knowledge-ingest insight proposals list
knowledge-ingest insight gate resolve ID \
  --decision ADOPT|EXPERIMENT|WATCH|ARCHIVE|REJECT
knowledge-ingest insight watch list
knowledge-ingest insight digest [--send]
knowledge-ingest insight doctor
knowledge-ingest insight budget show
knowledge-ingest insight budget reset [--stage STAGE]
knowledge-ingest insight shadow-agent install|status|uninstall
```

`scan` default is shadow behavior; it does not alter Telegram V2 processing state.

- [ ] **Step 4: Write and implement digest tests**

Digest is navigation, not article rewrite.

Required summary:

```text
cards generated
pending proposals
ADOPT recommendations
EXPERIMENT recommendations
WATCH signals
high-priority project impacts
time-sensitive business opportunities
```

No full card bodies in digest.

- [ ] **Step 5: Implement optional notification path**

Before implementation, ZCode must inspect current notification boundary.

Preferred direction:
- extract/reuse a generic `NotificationPort` without breaking existing Telegram command/tests;
- do not make `insight` import Telegram business objects.

If safe generic extraction would materially expand scope, V3.0 may ship `insight digest` locally first and keep `--send` blocked with explicit `NOT_CONFIGURED`; this adjustment must be surfaced in the local plan review and approved by the user before implementation.

- [ ] **Step 6: Write and implement `insight doctor`**

Checks at minimum:
- insight root writable / ORICO available;
- schema supported;
- model command configured/parseable;
- retrieval command or internal approved cognition source available;
- pending WAITING_CONTENT count;
- blocked model runs;
- critic fail rate/count;
- pending Human Gate count;
- card/proposal file consistency;
- shadow-agent state when installed.

Doctor is read-only in V3.0.

- [ ] **Step 7: Write and implement Shadow Agent**

Default:

```text
StartInterval = 300 seconds
```

Program:

```text
knowledge-ingest insight scan --provider telegram
```

Rules:
- separate LaunchAgent from Telegram watcher;
- no V2 state mutation;
- crash/restart safe because InsightStore is authoritative;
- tests hermetic, no real launchctl.

- [ ] **Step 8: Run M8 focused tests**

```bash
uv run pytest tests/unit/test_insight_service.py \
                  tests/unit/test_insight_cli.py \
                  tests/unit/test_insight_digest.py \
                  tests/unit/test_insight_doctor.py \
                  tests/unit/test_insight_shadow_agent.py -q
```

- [ ] **Step 9: Run full regression before commit**

```bash
uv run pytest -q
uv run ruff check src tests
```

Expected: all tests green; no regression in existing Telegram V2.

- [ ] **Step 10: Commit M8**

```bash
git add src/knowledge_ingest/insight src/knowledge_ingest/cli.py \
        tests/unit/test_insight_*.py
git commit -m "feat(insight): add shadow runtime cli digest and doctor"
```

---

### Task 9: Golden Set Harness and Quality Metrics

**Files:**
- Create: `src/knowledge_ingest/insight/golden.py`
- Create: `tests/unit/test_insight_golden.py`
- Create: `docs/insight-golden-set.md`
- Modify: `src/knowledge_ingest/cli.py`

**Interfaces:**
- Produces:
  - `knowledge-ingest insight golden run --manifest PATH`
  - metric report without committing private source text.

- [ ] **Step 1: Define private Golden Manifest contract**

Golden source material and user historical high-quality ChatGPT answers must **not** be committed to public repo.

Manifest schema:

```yaml
schema_version: 1
cases:
  - case_id: G1
    category: cognition_upgrade
    source_path: /absolute/or/qa-relative/path
    reference_path: /path/to/past-approved-response
    expected:
      candidate: true
      deep_value: DEEP_READ
      requires_personal_connection: true
```

Recommended private location:

```text
<KnowledgePipeline>/qa/insight-golden/
```

- [ ] **Step 2: Write failing harness tests with synthetic fixtures**

Metrics produced:

```text
valuable_idea_recall
deep_read_precision
false_personal_link_rate
stale_context_leakage
critic_pass_rate
human_quality_pending
```

No BLEU/ROUGE requirement.

- [ ] **Step 3: Implement golden runner**

Runner:
- can run stage-only or full pipeline;
- writes JSON + Markdown report under QA;
- never copies private source content into repo;
- clearly distinguishes automatic metrics from human quality judgment.

- [ ] **Step 4: Add initial real Golden Set from user-approved historical examples**

At minimum categories:

```text
G1 cognition upgrade
G2 business-model challenge
G3 AI/Agent architecture
G4 archive/reject despite novelty
G5 no personal context
G6 conflict with confirmed cognition
G7 weak signals → trend
G8 market opportunity but poor personal fit
```

Use the user-provided historical ChatGPT notes as reference **only in private QA storage**.

- [ ] **Step 5: Run Golden shadow acceptance**

Do not tune to a fixed number of cards.

Primary gates:
- manually tagged valuable cases must not be lost by Stage 1;
- Personal Retrieval must not invent relation;
- passed cards must pass Critic;
- Human Gate remains pending.

Record misses explicitly; do not “fix labels” to make metrics green.

- [ ] **Step 6: Commit harness/docs only**

```bash
git add src/knowledge_ingest/insight/golden.py \
        tests/unit/test_insight_golden.py \
        docs/insight-golden-set.md src/knowledge_ingest/cli.py
git commit -m "test(insight): add private golden-set harness"
```

Do not add private Golden materials.

---

### Task 10: Real Shadow Acceptance, Documentation, and V3 Freeze Candidate

**Files:**
- Create: `docs/k2c-personal-insight-engine-v3-验收记录.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: approved implementation plan only if actual deviations require an explicit addendum; never silently rewrite history.

**Interfaces:**
- Consumes the full implemented system.
- Produces auditable real acceptance and operator docs.

- [ ] **Step 1: Run full test + lint baseline**

```bash
uv run pytest -q
uv run ruff check src tests
```

Record exact pass count; do not pre-write “expected 562 + N”.

- [ ] **Step 2: Run real shadow mode against current Telegram production data**

Required evidence:
- V2 watcher/K2C behavior unchanged;
- Insight source count;
- Candidate count;
- Deep Read / WATCH / Archive / Reject distribution;
- model blocked/error counts;
- Personal Retrieval empty-context count;
- Critic first-pass / revision / unresolved counts;
- pending Human Gate count.

No `ADOPT` is performed merely for acceptance unless the user explicitly approves a real proposal.

- [ ] **Step 3: Manually review a representative card sample**

At least:
- one cognition case;
- one business opportunity;
- one AI/Agent project-impact case;
- one archive/reject case;
- one no-personal-context case.

Compare against historical “single article → ChatGPT deep reply” quality baseline.

- [ ] **Step 4: Prove V2 isolation**

Verify:
- no Insight columns/tables added to Telegram state DB;
- V2 Telegram doctor remains healthy;
- existing source item statuses were not changed by Insight scan;
- K2C Publish/Activate semantics unchanged.

- [ ] **Step 5: Prove Human Gate**

Verify:
- pending proposal does not appear in approved cognition;
- explicit user ADOPT is the only path that writes confirmed cognition;
- repeated same decision idempotent;
- conflicting decision fails visibly.

- [ ] **Step 6: Write real acceptance record**

Use statuses:

```text
PASS
FAIL
BLOCKED_EXTERNAL
NOT_EXERCISED
```

Never mark external/model/real-user steps PASS without evidence.

- [ ] **Step 7: Update README**

Document:
- V2 Source Lane vs V3 Insight Lane;
- Quick Start commands;
- shadow mode;
- model/retrieval port envs;
- Human Gate;
- private Golden Set;
- privacy boundary.

- [ ] **Step 8: Final whole-branch review**

Use `superpowers:requesting-code-review` or the project’s approved independent review flow.

Review focus:
- no V2 regressions;
- no silent value loss;
- no auto cognition mutation;
- no sensitive context leakage;
- no fake “personalization”.

- [ ] **Step 9: Commit docs / acceptance**

```bash
git add docs README.md README.zh-CN.md
git commit -m "docs(insight): record v3 shadow acceptance"
```

- [ ] **Step 10: STOP before production promotion**

Do not:
- disable old TG 深读;
- change existing digest schedule;
- auto-ADOPT pending proposals;
- enable new LaunchAgent on production by assumption.

Present the real acceptance results to the user and wait for explicit approval of promotion.

---

# Implementation Order and Dependency Rules

The required order is:

```text
M1 Store/Contracts
  ↓
M2 Source Projection
  ↓
M3 Model/Budget
  ↓
M4 Value Filters/Trend
  ↓
M5 Personal Retrieval
  ↓
M6 Thinking/Critic/Card
  ↓
M7 Human Gate
  ↓
M8 Runtime/CLI/Ops
  ↓
M9 Golden Set
  ↓
M10 Real Acceptance
```

Do not parallelize M4–M7 across independent agents before their upstream interfaces are committed; their data contracts depend on each other.

Safe parallelism after upstream contracts are frozen:
- within M6, Evidence Pack tests and Card renderer tests can be prepared independently;
- within M8, Digest and Doctor can be developed independently after `InsightService` interface is fixed;
- documentation can be drafted in parallel with M10 acceptance, but acceptance facts must only be filled from real evidence.

---

# Explicit Deferred Scope

The following are intentionally deferred beyond V3.0 unless the user opens a new design cycle:

1. external Web fact-check/research worker for every Insight;
2. vector DB / embedding infrastructure;
3. semantic graph UI;
4. automatic editing of existing historical ChatGPT note files;
5. automatic Obsidian bidirectional sync if no current stable writer already exists;
6. auto-learned “value skip” rules;
7. recommendation ranking by engagement/click behavior;
8. multi-user profiles;
9. cloud dashboard;
10. automatic monetization execution / auto-purchase / auto-posting.

---

# Self-Review Results

## Spec coverage

Covered:
- source-independent Insight Lane → M1/M2;
- two-stage filter → M4;
- weak-signal trend → M4;
- Personal Reasoning Retrieval → M5;
- Evidence Pack → M6;
- Thinking Protocol → M6;
- Business Opportunity Lens → M6;
- Quality Critic + one revision → M6;
- Deep Insight Card → M6;
- Cognitive Change Proposal → M7;
- Human Gate → M7;
- batch digest / operator surface → M8;
- separate Insight budget → M3;
- fail-closed → M2/M3/M6;
- shadow migration → M2/M8/M10;
- Golden quality baseline → M9;
- real acceptance and V2 isolation → M10.

No design requirement is intentionally omitted.

## Type consistency

The downstream interface names used by later Tasks are defined upstream:
- `InsightSourceView` → M1;
- `ContentResolverPort` → M2;
- `InsightModelPort` / `InsightAIBudgetGuard` → M3;
- `CandidateDecision` / `DeepValueDecision` → M1, implemented by M4;
- `PersonalKnowledgePort` / `PersonalContextRef` → M1/M5;
- `EvidencePack` / `CriticResult` → M1/M6;
- `InsightStore` → M1;
- Human Gate → M7;
- orchestration → M8.

## Proportion / scope

The plan deliberately does not prescribe model prompt prose word-for-word or SQLite SQL bodies where tests/interfaces already determine behavior. It does freeze the decisions an implementer cannot safely invent: state values, failure semantics, Human Gate behavior, stage boundaries, commands, default trend thresholds, context caps and migration safety.

---

# Execution Handoff

This plan is **not authorization to implement**.

The next required action is:

```text
Local ZCode Plan Mode
→ read AGENTS / project docs / approved V3 design / this plan
→ targeted machine/repo verification only
→ produce local-adapted Final Plan
→ STOP
→ user copies plan back to ChatGPT for Final Plan Review
```

ChatGPT Final Plan Review should return only precise modifications and a verdict; it should not rewrite the entire plan.

Only after the user explicitly approves the locally adapted plan may ZCode execute Milestones M1 → M10.
