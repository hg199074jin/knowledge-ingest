# Knowledge-Ingest V3 Personal Insight Engine — 本机审阅意见（Final Implementation Plan v3）

> 状态：**APPROVED FOR IMPLEMENTATION**（ChatGPT Final Plan Review 三轮收敛后正式授权）
> 日期：2026-09-28
> 审阅人：ZCode Plan Mode（本机适配审阅）；ChatGPT（Final Plan Review，12 项修正 + M10-P 追加 + 终裁）
> 执行身份：执行 Agent 必须同时读取以下三份文件，本文不得覆盖前两份的冻结语义：
> 1. `docs/k2c-personal-insight-engine-v3-design.md`（设计，原样入库未改写）
> 2. `docs/k2c-personal-insight-engine-v3-实施方案.md`（实施方案，原样入库未改写）
> 3. 本文（本机适配后的执行计划）
>
> 编号约定：调整决策统一为 **A1–A9**（Final Plan Review 终裁修正，不存在 A0）。

---

## 0. 最终结论

**READY — 已获正式实施授权。**

- M1–M10 结构照实施方案不变；本机事实核对未发现技术冲突；
- 本机精化以 A1–A9 表达，**永不静默写回原实施方案正文**；
- 晋级链：工程正确性（M1–M8）→ 系统质量（M9 Golden Set）→ 最终效果质量（M10 + M10-P）→ SHADOW → PRODUCTION CANDIDATE（硬门）→ 用户最终批准。

## 1. 已确认本机事实（F1–F11，实测证据）

| # | 事实 | 证据 |
|---|---|---|
| F1 | `HEAD = origin/main = 7d172221e8995328d9995288746de228e48c695b`（分支 main），与实施方案基线 SHA 一致；working tree 仅未跟踪用户文件 `.zcodeignore`（保留不动） | git rev-parse |
| F2 | 回归基线 562 tests 绿 + ruff 干净 + CI 双平台（ubuntu/macos）绿；`tests.yml` 已含 `KI_TELEGRAM_ORICO_ROOT` job 级 env | CI run 结论 |
| F3 | `src/knowledge_ingest/insight/` 与 `tests/unit/test_insight*` 不存在，命名空间干净；47 个现有单测文件均为 `test_*.py` + 本地 `make_store/make_config` fixture 惯例 | ls / grep |
| F4 | 接口无漂移：`cli.py:1712 _extract_json_text`、`cli.py:1779 _tg_classifier_channel`；`telegram/event_store.py:18 SCHEMA_VERSION=3`；telegram AIBudgetGuard outcome 仅 `success/empty/rate_limit`（insight 账本含 `error` 为独立新语义，不改 telegram） | grep 行号 |
| F5 | `telegram/notification.py` 中 `NotificationPort`/`WxPusherAdapter`/`load_wxpusher_credentials`/`null_port` 无 Telegram 状态依赖；仅 `notify()`/`build_digest()` 耦合 TelegramEventStore；WxPusher 凭据未投放 | 源码核对 |
| F6 | PDF/video 可读全文稳定链：`source_items.knowledge_ingest_job_id` → `{pipeline_root}/jobs/<job>/job.yaml` 的 `docchunk.corpus_path`（H3-A 真实 job 实测）→ corpus 冻结入口 `index.jsonl + batches/B*.md`（全局 AGENTS.md §17）；TEXT 权威路径 = `source_items.materialized_path` | job.yaml + corpus 目录 |
| F7 | 本机无统一稳定个人知识检索入口（Obsidian 3 vault + `~/.agents/skills` 103 技能，无检索 CLI） | 目录核对 |
| F8 | 模型通道：`codex exec … -` stdin→stdout JSON、exit 0（新鲜复核 + 236 条生产预判实证 high 档 14–19s/条）；输出含杂讯行 → JSON 抽取层必需。不安装新模型、不写 API Key、无自动 provider 切换（全局 AGENTS.md §5） | 实测 |
| F9 | LaunchAgent：现有 `ki-telegram-watch / ki-telegram-digest / ki-resume`；`com.sandro.ki-insight-shadow` 无冲突；`.venv` 解释器已有 FDA/TCC | launchctl list |
| F10 | `AppConfig` 覆盖所需：`pipeline_root=/Volumes/ORICO/KnowledgePipeline`、`docchunk_corpus_root=/Volumes/ORICO/LongDocCorpus`；V3 沿 env+files 惯例，不改 config schema | config.example.yaml |
| F11 | V3 两份文档与 Golden 参照（3 份 ChatGPT 导出、202 条用户精选）均在 `~/Downloads` | ls |

## 2. 设计冻结要求（实施全程不得擅自修改）

- Telegram V2/V2.2 不推翻；`telegram/state.db`（Source Facts）与 `insight/state.db`（Personal Insight Facts）分离；Insight 为旁路 Shadow Lane；
- High Recall 优先；不自动学习 value-SKIP；`NO_RELEVANT_PERSONAL_CONTEXT` 是合法结果；
- `ResolvedContent.parts` 多 part 契约；长文档逐 part `evidence_extract`，不得无上限拼接、不得静默截断；
- 7-stage Insight AI Budget：`candidate_filter / deep_value_gate / retrieval_query_plan / retrieval_select / evidence_extract / thinking / critic`；
- DeepValueGate 不依赖完整 Personal Retrieval；`contradiction_value` 允许 `unknown`，禁止为填字段推测用户旧认知；
- source identity = `(provider, source_item_id)`；`content_fingerprint` 变 → `source_revision += 1`；所有 candidate/gate/context-ref/run/card/proposal/Human Gate 绑定具体 revision；已 ADOPT 的旧 revision 不得被 EDIT 静默覆盖；
- Personal Thinking Engine 必须经独立 Quality Critic，默认最多 1 次修订；Human Gate 五决策 `ADOPT|EXPERIMENT|WATCH|ARCHIVE|REJECT`，模型不得代行 ADOPT；长期认知写入只发生在真实 Human Gate 后；
- Insight Lane 与 K2C Lane 并行不替代；M10 无真实 Personal Retrieval source → `BLOCKED_EXTERNAL`；
- M10-P 是 SHADOW → PRODUCTION CANDIDATE 硬门；M10-P 后仍 STOP 等用户批准。

## 3. 调整决策（A1–A9）

**A1｜M0 三文档入库**
`docs/k2c-personal-insight-engine-v3-design.md` 与 `docs/k2c-personal-insight-engine-v3-实施方案.md` 自 `~/Downloads` 原样复制（验收=与源 diff 为空）；新增 `docs/k2c-personal-insight-engine-v3-本机审阅意见.md`（本文）。A1–A9 修订永不静默改写前两份正文。Commit：`docs(insight): import approved v3 design, plan, and local review`。

**A2｜ContentResolver 精确映射**
- TEXT：权威路径直接读 `source_items.materialized_path`，文件真实存在才读取；禁止按 item_id 重建路径；
- PDF/video：`knowledge_ingest_job_id → jobs/<job>/job.yaml → docchunk.corpus_path`；corpus 入口 = `index.jsonl + batches/B*.md`（§17 冻结）；**无 `combined.md` fallback**；
- 契约多 part：`ResolvedContent.parts: list[Part]`，每 part 含 `part_id / text / source_ref`。TEXT=1 part；PDF/video 按 verified Corpus batch 成多 part，由 `evidence_extract` 逐批处理；
- 无 job/未下载（含 PENDING_RESOURCE 存量）：注册 SourceView + `WAITING_CONTENT`，不 REJECT 不伪读。

**A3｜PersonalKnowledgePort + Promotion Gate**
V3.0 首版 = `CommandPersonalKnowledgePort` + `ApprovedCognitionKnowledgePort`；不扩展 Obsidian adapter（Deferred）。**Promotion Gate（硬门）**：M9/M10 宣布验收前必须配置至少一个真实非空的 `KI_INSIGHT_RETRIEVAL_CMD` 读取用户既有长期知识；否则 M10 中 `Personal Retrieval Acceptance = BLOCKED_EXTERNAL`，整体 Personal Connection 不得标 PASS。三份 ChatGPT 笔记仅作私人 QA source（`<KnowledgePipeline>/qa/insight-golden/`，不入 Git）。

**A4｜模型配置 = 部署层**
产品代码只认 `KI_INSIGHT_MODEL_CMD` + stage override（含 `KI_INSIGHT_EVIDENCE_CMD`）+ `KI_INSIGHT_MODEL_CWD`；不写死任何模型名/档位。Shadow 期部署值（部署配置，非代码）：`codex exec --skip-git-repo-check -m gpt-6-luna -c model_reasoning_effort=high -c project_doc_max_bytes=0 -`，CWD 复用 `~/.cache/knowledge-ingest/llm-cwd`，timeout 默认 180s。M9/M10 记录每 stage call count / latency / failure / budget usage；Shadow 验收前不为省 token 降低推理档。

**A5｜Notification = 通用模块抽取（Final Plan Review 已裁定）**
新增 `src/knowledge_ingest/notification.py` 承载 `NotificationPort / WxPusherAdapter / load_wxpusher_credentials / null_port`；`telegram/notification.py` 改为复用通用模块，保留 Telegram 专属 `notify(store, ...)` / `build_digest(...)`；Insight 使用同一通用 Port + 自有 notification 表/幂等/投递状态。现有 telegram notification 测试必须保持绿。属有边界的小型复用抽取，不计入范围扩张。

**A6｜shadow-agent 参数 pin**
label `com.sandro.ki-insight-shadow`；plist pin `--config`（沿 watch-agent 纪律，不复制 digest-agent 的漂移）；日志内盘 `~/Library/Logs/knowledge-ingest/insight-shadow.log`；`StartInterval=300`；hermetic 测试 monkeypatch `run_launchctl`。

**A7｜命令口径（锁定环境）**
全量验收统一：`uv run pytest -q` + `uv run ruff check src tests`（ruff pin 于 dev dependency；不用 `uvx` 临时解析）。insight 不复用 `KI_TELEGRAM_ORICO_ROOT`；insight root 全走 `AppConfig.pipeline_root`（测试 tmp config 天然 hermetic）；生产 ORICO 可用性由 `insight doctor` 检查 root 可写。

**A8｜确认非漂移两点**
① insight 预算 outcome 四值（含 `error`）为独立账本新语义，不改 telegram 三值；② 实施基线 SHA 与本机一致，执行记录无需偏差说明。

**A9｜M9 Golden 材料路径 pin**
三份 ChatGPT 导出复制到 `<KnowledgePipeline>/qa/insight-golden/`（私人，不入库），作 Golden 参照与"历史一对一深读质量基线"。

## 4. Milestone 验收门 / 测试命令 / 提交边界

| MS | 验收条件（门） | 测试命令 | 提交边界 |
|---|---|---|---|
| M0 | 三份文档入库：两份与源 diff 为空 + 本机审阅意见存在 | `diff` 两份复制件 vs `~/Downloads` 源 | `docs(insight): import approved v3 design, plan, and local review` |
| M1 | 契约枚举/幂等/WAL/fail-fast 绿；含 `content_fingerprint` + `source_revision`：同 `(provider, source_item_id)` 同逻辑身份；指纹变 → revision+1；candidate/gate/run 绑定 revision | `uv run pytest tests/unit/test_insight_models.py tests/unit/test_insight_store.py -q` + ruff | `feat(insight): add core contracts and independent store` |
| M2 | 投影只读（V2 行数/状态前后一致）；TEXT 走 `materialized_path`（存在才读）；WAITING_CONTENT 可恢复；重扫同 id | `uv run pytest tests/unit/test_insight_source_view.py tests/unit/test_telegram_insight_adapter.py tests/unit/test_telegram_learned.py tests/unit/test_tg7_migration.py -q` | `feat(insight): add telegram shadow source adapter` |
| M3 | 非零退出/超时/坏 JSON → 类型化错误且 source 可重试；7 个 Insight stage 预算账本独立；含 `KI_INSIGHT_EVIDENCE_CMD` override | `uv run pytest tests/unit/test_insight_model_port.py tests/unit/test_insight_budget.py -q` | `feat(insight): add model port and isolated ai budget` |
| M4 | 高召回（低置信不拒）；Gate 接口无 `personal_preview`——仅用 Source 内容 + CandidateDecision + 轻量画像/active-project tag；`contradiction_value` 可 `unknown`；trend 30 天/3 信号幂等 | `uv run pytest tests/unit/test_insight_candidate_filter.py tests/unit/test_insight_value_gate.py tests/unit/test_insight_trend.py -q` | `feat(insight): add two-stage value filter and trend watch` |
| M5 | 空结果显式化；SUPERSEDED/REJECTED 默认不泄漏；硬关联被 Selector 拒；上限 5/20/12 | `uv run pytest tests/unit/test_insight_knowledge_port.py tests/unit/test_insight_retrieval.py tests/unit/test_insight_context_pack.py -q` | `feat(insight): add personal reasoning retrieval` |
| M6 | `evidence_extract` 独立 stage：TEXT 1 part 1 次、PDF/video 逐 part 后确定性聚合去重；失败→BLOCKED 可重试，不以摘要冒充；Evidence Pack 六类分离；恰好最多 2 次 thinker 调用；终审失败→NEEDS_REVIEW 无 Proposal；卡面自适应 | `uv run pytest tests/unit/test_insight_evidence.py tests/unit/test_insight_thinker.py tests/unit/test_insight_critic.py tests/unit/test_insight_cards.py -q` | `feat(insight): add evidence extract, thinking engine, critic, and cards` |
| M7 | 同决策幂等/异决策 `GateConflictError`/模型侧无 resolve 通路/ADOPT 原子写且绑定 revision | `uv run pytest tests/unit/test_insight_proposals.py tests/unit/test_insight_human_gate.py -q` | `feat(insight): add cognition proposals and human gate` |
| M8 | 全链 restart-safe 且增量约束：每轮仅处理新 SourceView / WAITING→readable / 指纹变更新 revision / BLOCKED 重试 / WATCH 重触发——已完成且 input/version 未变的 stage 不再调模型不扣预算（InsightStore 为幂等权威）；通用 Notification 抽取落地且现有 telegram notification 测试保持绿；V2 全量回归（≥562+N 全绿）+ ruff | `uv run pytest -q && uv run ruff check src tests` | `feat(insight): add shadow runtime, cli, digest, and doctor` |
| M9 | 私人 Golden 不入库；自动指标（valuable_idea_recall / deep_read_precision / false_personal_link_rate / stale_context_leakage / critic_pass_rate / human_quality_pending）与人工评判分离；记录每 stage call count/latency/failure/budget；miss 如实记录 | `uv run pytest tests/unit/test_insight_golden.py -q` | harness+docs only |
| M10 | 真实 shadow 证据齐（分布/blocked/空上下文/Critic 计数/Human Gate pending）；硬门：无真实 `KI_INSIGHT_RETRIEVAL_CMD` → Personal Retrieval Acceptance = `BLOCKED_EXTERNAL`，Personal Connection 不得 PASS；V2 隔离证明；Human Gate 幂等/冲突证明；STOP 等生产晋级批准 | `uv run pytest -q && uv run ruff check src tests` + 真实运行取证 | `docs(insight): record v3 shadow acceptance` |
| **M10-P** | **Cross-Agent Parity Acceptance 全部通过**（规格见 §5） | 真实双 agent 对跑 + 人工裁决（无自动化测试命令；记录文件为验收产物） | `docs(insight): record cross-agent parity acceptance`（脱敏结论入 docs；P1–P5-parity.md 本体落私人 QA 不入 Git） |

## 5. M10-P｜Cross-Agent Parity Acceptance（冻结规格）

**目标**：对同一份真正有价值的信息，V3 的最终深度判断应与"用户把该材料单独发送给 ChatGPT、要求像过去一样深入分析"的结果在核心认知上大体收敛。不要求文风/标题/逐句/字数一致；要求核心判断、真正价值、主要质疑、个人关联、认知变化、行动方向大体一致。

**样本**：固定 5 条真实信息（真实 Telegram / Golden Set 提名，用户最终裁定；禁止执行 Agent 自选易过样本），覆盖 P1 认知提升 / P2 商业机会（市场+个人机会分开检验）/ P3 AI-Agent-技术架构（与 knowledge-ingest/K2C/ZCode 真实关联）/ P4 看似有启发实际价值有限（须敢于 `ARCHIVE/REJECT/NONE`）/ P5 挑战既有认知（触发 reinforce/revise/overturn 或说明为何暂不改变）。禁止五条全选易得正面结论的内容。

**输入公平**：ChatGPT 侧=新对话+原始材料+Personal Context Pack+固定任务说明（像过去一对一深聊一样分析：真正说对了什么、漏洞、与旧认知及当前项目的关系、是否改变认知、商业机会、"我的版本"）；不得先给 V3 结论。V3 侧=同一原始材料走正式 Pipeline（Candidate→Deep Value→Personal Retrieval→Evidence Extract→Thinking→Critic→Card），不得人工修改输出后参赛。

**两层 Parity**：
- A. **Controlled Parity（必须通过的主测试）**：ChatGPT 与 V3 用**同一份 Personal Context Pack**（直接导出 V3 该 case 的 M5 真实渲染产物，可审计）→ 判断 Thinking Engine 本身；
- B. **End-to-End Parity**：ChatGPT 用用户正常上下文、V3 用自己的 Retrieval → 允许合理差异，但必须单独检查差异来自"思考质量"还是"V3 没检索到关键旧认知"；不得把 Retrieval 失败误判成 Thinking Engine 失败。
- 诊断规则：Controlled 不一致 → Thinking/Evidence/Critic 层问题；Controlled 接近而 End-to-End 不一致 → Personal Retrieval 层问题。

**比较维度（8 项，每条 MATCH/PARTIAL/MISMATCH）**：Core Understanding / Mechanism / Critical Reasoning / Personal Connection / Project Impact / Own Version / Cognition Delta（ADD/REINFORCE/REVISE/OVERTURN/NONE 接近度）/ Action-Opportunity。

**禁止文本相似度**：BLEU/ROUGE/embedding cosine/字数/标题匹配不作主要结论。验收对象 = semantic judgment convergence。

**允许的差异**（不自动 FAIL）：行动条数 2 vs 3、表达不同、一方更保守、次要风险排序不同、非核心项目关联差异。
**实质性不一致**：商业基本不成立 vs 强烈推荐立即做；无认知增量 vs 重大 revision；漏掉最关键商业约束；硬关联而 ChatGPT 同 Context 下认为无关；漏识旧认知冲突；V3 仍是摘要而 ChatGPT 已完成机制抽象与"我的版本"。

**记录**：`<KnowledgePipeline>/qa/insight-parity/P{1..5}-parity.md`（私人，不入 Git），格式含 Case / ChatGPT Result / V3 Result / 8 维度判定 / Major Divergence / Root Cause（Retrieval-Evidence-Thinking-Critic-Reasonable Difference）/ Human Verdict（PASS/NEEDS_TUNING/FAIL）。Git 中只保存脱敏结论与统计。

**通过标准（冻结）**：5 条全部不得出现严重方向性误判；至少 4/5 整体 PASS、剩余最多 NEEDS_TUNING；Personal Connection 不得 fabricated/hard-link；Cognition Delta 不得严重反向；商业 case 不得"市场成立=用户适合"；P4 必须证明 V3 敢说"不值得深思/不值得改变认知"。严重 MISMATCH 时禁止调 Prompt 迎合 ChatGPT，先定位根因层再修对应层。ChatGPT 结果=高质量参考基线而非绝对标准；最终 Human Acceptance 归用户。

**晋级规则**：原 M10 全部验收通过 ∧ M10-P 通过 → SHADOW → PRODUCTION CANDIDATE → **仍 STOP 等用户最终批准**。

## 6. 执行纪律

- 顺序 `M0 → M1 → … → M10 → M10-P`，不跳步、不合并提交；
- TDD：先写失败测试 → 确认 RED → 最小实现 → GREEN → 回归 → commit；
- 每 Milestone：focused tests → regression → 对应范围 review → commit → 下一个；
- 设计级冲突：停该 Milestone，回报（真实事实 / 冲突位置 / 最小调整建议 / git 状态）后 STOP；普通 bug 与接口细节按计划自行修复；
- **强制人工复核点（用户指定）**：① M5 完成后呈 Personal Retrieval 真实输出样例；② M6 完成后呈 3–5 张真实 Deep Insight Card（重点检查"高级摘要"退化）；③ M10-P 用户作为对照 Agent 参加正式验收；
- Deferred Scope 十项（Web 核验/向量库/语义图 UI/改写历史 ChatGPT 笔记/Obsidian 双向同步/auto value-SKIP/行为排序/多用户/云看板/自动执行）不进入 V3.0。
