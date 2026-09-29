# Knowledge-Ingest V3 Personal Insight Engine — M10 生产 Shadow 验收记录

- 日期：2026-09-29（首次验收同日完成 M10-R 修复与复验）
- 分支：`v3-insight-engine`
- 结论先行：
  - **首次 M10 = FAIL**（验收记录 commit `203862d`；1×P0 代码崩溃 + 1×P1 模型契约可靠性；工程边界全部成立）——历史结论保留于 §1–§12，不覆盖；
  - **M10-R 修复与复验完成后：M10 最终 Verdict = PASS WITH CALIBRATION ITEMS**（3 项校准/可靠性项透明列出，见 §M10-R-8）。
- 不进入 M10-P（等用户对 Production Shadow Review 的最终裁定）。

---

## 1. 验收范围与方法

生产 Shadow Scan：真实 Telegram 生产库（`KnowledgePipeline/telegram/state.db`）只读投影 → V3 全链路（candidate → deep_value_gate → retrieval → evidence → thinking → critic → card），Luna-high 真实调用，临时/生产 InsightStore 落库，零 Telegram 状态写入。

- **Batch-1**（2026-09-29 上午）：`tg_ai_nav` 最近 20 条，结果 `qa/insight-shadow-acceptance.json`。用户复核结论：**Batch-1 = PASS（工程验证）/ M10 Overall = IN PROGRESS**；单频道样本偏斜，要求 Batch-2 跨频道。
- **Batch-2**（2026-09-29 下午）：跨频道自然分布采样（各频道最近 N 条 eligible，不挑内容）：`tg_side_hustle` 8、`tg_chuhai` 8、`tg_it_charge` 8、`tg_vip_docs` 1（该频道仅 1 条 pdf eligible，如实记录）。共 25 条，写入生产 InsightStore（`KnowledgePipeline/insight/`），结果 `qa/insight-shadow-acceptance-batch2.json`。
- Batch-2 附加专项：ModelBadOutputError 逐条跟踪 + 失败条目重试一次 + V2 隔离证据补全。

## 2. 合并真实分布（Batch-1 + Batch-2，45 条 eligible）

| 终态 | 条数 | 占比 |
|---|---|---|
| candidate/rejected | 13 | 28.9% |
| value_gate/WATCH | 18 | 40.0% |
| value_gate/ARCHIVE_ONLY | 5 | 11.1% |
| DEEP_READ 进入 → 卡片写出 | **0** | 0% |
| DEEP_READ 进入 → P0 崩溃（M10-BUG-1） | 4 | 8.9% |
| deep_value_gate 错误未解决（fail-closed，含 Batch-1 未重试 4 条 + Batch-2 重试后仍失败 1 条） | 5 | 11.1% |

- ERROR 与 REJECT 严格分开统计，未混入。
- cognition delta / proposal：0（无卡片即无 proposal）；approved_cognition = 0（未执行任何 ADOPT）。

### 分频道分布

| 频道 | 条数 | rejected | WATCH | ARCHIVE | gate 错误 | DEEP_READ 后崩溃 |
|---|---|---|---|---|---|---|
| tg_ai_nav (B1) | 20 | 10 | 6 | 0 | 4 | 0 |
| tg_side_hustle | 8 | 1 | 5 | 2 | 2（重试后：2 archive） | 0（本轮未过门） |
| tg_chuhai | 8 | 2 | 3 | 0 | 3（重试后：1 watch + 2 崩溃） | 2* |
| tg_it_charge | 8 | 0 | 3 | 0 | 4（重试后：1 watch + 3 崩溃） | 3* |
| tg_vip_docs | 1 | 0 | 1 | 0 | 0 | 0 |

\* DEEP_READ 崩溃共 4 个不重复条目：52258（主扫过门即崩）、11803 / 52262 / 52256（重试过门后崩）。

## 3. ModelBadOutputError 专项（用户指定重点观察项）

**结论：跨频道持续性运行可靠性问题，非单频道偶发波动。**

| 指标 | Batch-1 | Batch-2 |
|---|---|---|
| 首过失败率 | 4/20 = 20% | 9/25 = 36%（按 gate 尝试计 10/31 = 32.3%） |
| 失败阶段 | deep_value_gate | deep_value_gate（9）+ card 写出崩溃（1，另列 P0） |
| 失败类别 | `missing decision field` | 同左，9/9 同类 |
| 是否缺 `decision` | 是 | 是（9/9） |
| 跨频道性 | 单频道 | side_hustle 2/8、chuhai 3/8、it_charge 5/8 —— **三频道齐发** |
| 重试一次恢复率 | （未做） | 9/10 恢复（随机性模型波动，非确定性）；1 条复发 |
| 是否失去潜在 DEEP_READ | 4 次被 fail-closed 吞掉 | 10 次尝试中 4 次在重试后过门（本会通向深读），其余被吞 |

判定：30–40% 的首过失败率下，"fail-closed + 人工重扫"不构成可持续的生产姿态。**必须修 Model Contract / retry-with-backoff / parser**（M9-CAL 已预告该问题，本批升级为 P1 必修项）。按用户指示，本批未改任何 prompt/critic/代码。

## 4. P0 发现：M10-BUG-1（DEEP_READ→卡片通路在生产冷启动下确定性崩溃）

- 症状：`AttributeError: 'NoneType' object has no attribute 'pack_id'`，4/4 个过门条目 100% 复现。
- 根因：`src/knowledge_ingest/insight/service.py:137,143` 直接解引用 `self.store.get_context_pack(sid).pack_id`；而 `store.get_context_pack` 在零 context refs 时按契约返回 `None`（store.py "未确定返回 None"）。生产冷启动（approved_cognition=0）下检索必然返回 0 refs → `replace_context_refs` 空写 → `get_context_pack`=None → 崩溃。
- 讽刺点：`replace_context_refs` 即使空 refs 也返回合法内容寻址 pack_id，但 service 把返回值丢弃了。
- 影响：**每一条通过价值门的条目都在付出 retrieval+evidence+thinking（约 4–6 分钟 Luna-high）之后、在卡片落盘前一刻崩溃，产物全部丢弃。生产冷启动期一张卡都产不出来。**
- 为什么 M9 没发现：Golden 用种子 context 保证 refs 非空，从未走过零 refs 路径。
- 次生缺陷（同路径）：`service.py` thinking 阶段无 `_stage_done` 幂等守卫（evidence 有）：崩溃条目恢复时会重复支付 thinking+critic（runs 表 run2+run7 同一条目两次，104–226s/次）。

## 5. 7-stage 调用 / 延迟 / 失败（Batch-2 实测）

| 阶段 | 调用 | 失败 | 延迟 |
|---|---|---|---|
| candidate_filter | 25 | 0 | ~17s/次（B1 实测均值） |
| deep_value_gate | 31 次尝试（21 次落库 + 10 次失败） | 10（32.3%） | ~30–45s/次 |
| retrieval（query_plan + select） | 5 轮 × 2 = 10 | 0 | ~22s + ~32s/轮（golden 实测） |
| evidence_extract | 4 | 0 | 20–33s |
| thinking + critic | 5 runs（含 1 次重复支付） | 0 | 104–226s/run，status 全 needs_review |
| card | 0 写出 / 5 次崩溃 | 5（P0） | — |

合计约 83+ 次 Luna-high 调用，**产出卡片 0 张**。质量备注：4 个进入深读的 thinking 草稿全部被 critic 判 needs_review（与 M9 critic 门槛偏高发现一致，但卡片未落盘，质量信号一并丢失）。注：扫描 harness 未接 InsightAIBudgetGuard（与 Batch-1 一致），预算账本由生产 shadow agent 接线覆盖，单测已覆盖。

## 6. 幂等性

Batch-2：成功终态条目每频道前 3 条复扫（3 条）→ **新模型调用 0（PASS）**。Batch-1：同 revision 重扫 0 新调用（PASS）。崩溃条目恢复路径存在重复支付（见 §4 次生缺陷）。

## 7. V2 隔离（补全证据）

| 检查 | 结果 |
|---|---|
| telegram/state.db 中 insight 表 | 0（PASS） |
| 扫描前后 source_items 已有条目 diff | **changed = 0**（PASS） |
| 窗口内新增 source_items | +5，全部为 watcher 自然到货（06:19–06:51 UTC：v2ex:10698、chuhai:11821、side_hustle:25085、zaihua:44100/44101），由 V2 管道物化为 materialized —— 非 V3 写入 |
| downloads / handoff_completed | 69→69 / 4→4，无变化 |
| watcher PID | 999 → 999 存活；窗口内日志零新增错误行（03:00 的 proxy 报错早于扫描且已自愈，之后 5 条新消息正常物化为证） |
| telegram doctor（扫描前后各一次） | 两次均 **0 FAIL / 2 WARN**（WARN：REVIEW 积压 145、通知未投递 8 —— 均为既有事项，与 V3 无关），diff 仅为 reconcile 时效与物化计数 +5 |
| K2C handoff / publish / activate 语义 | 零代码变更（git 状态干净，扫描脚本不触碰仓库），handoff 计数不变 |
| Insight error 对 watcher 影响 | 无（扫描期间 watcher 持续 reconcile + 物化新消息） |

## 8. Human Gate

- Batch-2 全程：proposals = 0（无卡片即无 proposal 入口）、approved_cognition = 0、**未执行任何真实 ADOPT**。
- needs_review 不产生 proposal、passed+NONE 不产生 cognition proposal、pending ≠ confirmed —— 与设计一致（本轮因 P0 未走到该分叉，机制由 M7 单测覆盖）。

## 9. M10-6 人工抽检（操作员执行；基于自然出现的结果，未人为补齐）

| 类型 | 观察 | 抽检结论 |
|---|---|---|
| passed card | NOT_OBSERVED（0 卡） | — |
| needs_review card | NOT_OBSERVED（0 卡） | — |
| ARCHIVE_ONLY | 25072《未来十年最值钱的15个能力》清单文 | 判定合理（无方法/无出处，泛化索引） |
| REJECT（candidate） | 25069《一眼看穿人心》书籍推广 | 判定合理 |
| WATCH | 3084（vip_docs pdf，仅标题导语）；52255（个人工具静态站简述） | 判定合理（证据不足保留观察） |
| NO_RELEVANT_PERSONAL_CONTEXT | 全批 retrieval 0 refs —— 但系冷启动空库所致，非检索判断 | 如实记录，不算正例 |
| DEEP_READ 触发合理性 | 11803 AiToEarn（9.3k star 自媒体变现开源）、52262 Prompt-Optimizer、52258 Humla 本地会议转写：合理；52256 everycube 魔方索引：偏宽松（高召回哲学内可接受） | 总体合理 |
| cognition delta / business opportunity | NOT_OBSERVED（0 卡） | — |

## 10. Personal Retrieval Acceptance（冷启动）

- 检索命令已配置并可执行，planner 正常产出判断问题，空库返回空 refs 不崩溃（崩溃发生在更下游的卡片写出，见 P0）。
- `approved_cognition = 0` 是真实 day-zero 状态；检索语义"只查 CONFIRMED"验证成立。带个人语境的检索质量已在 M9 Golden（种子 context）验证，本批无新增证据。

## 11. Verdict 与修复清单

**M10 Verdict = FAIL** —— 判据：45 条真实生产条目 0 卡片产出；4/4 进入主价值通路（DEEP_READ）的条目 100% 确定性崩溃；gate 阶段 32% 首过失败率跨频道持续。三条均超出 "PASS WITH CALIBRATION ITEMS" 的边界（校准项=质量调优，本批是功能性断裂）。

**修复清单（按序）：**

1. **[P0] M10-BUG-1**：service.py 卡片写出路径空 pack 崩溃。修法建议：消费 `replace_context_refs` 已返回的 pack_id（空 refs 亦有合法内容寻址 id），并为 `get_context_pack` 空况加显式守卫；补零 refs 冷启动路径的单测（M9 盲区）。
2. **[P0-次生] thinking 阶段补 `_stage_done` 守卫**，消除恢复路径重复支付。
3. **[P1] M10-CAL-01**：deep_value_gate 输出契约可靠性 —— retry-with-backoff 或 contract/parser 修复；修复后以跨频道重扫验证首过失败率降到可运行水位（建议 <10%）再谈生产。
4. 重跑范围：P0 修复后，对 4 个已知 DEEP_READ 条目 + 新到货条目做跨频道补扫，验证卡片真实落盘 + M10-6 补齐卡面抽检，再出 PASS/PASS WITH CALIBRATION ITEMS 终判。

## 12. STOP 声明（首次验收）

按用户 2026-09-29 复核指令：M10 Batch-2 完成即 STOP，**不进入 M10-P**（Cross-Agent Parity）。待 P0/P1 修复并复验后，由用户裁定是否重启 Production Shadow Review，再议 M10-P。

---

# M10-R Production Repair & Re-Acceptance（2026-09-29）

用户正式裁定：**M10 = FAIL 成立，授权进入 M10-R（Production Repair）**；不回退 M1–M9、不重设计、不为验收调 Critic 严格度；修复完成后 STOP，M10 重新 PASS 以前不启动 M10-P。

## M10-R-0｜提交账目（避免"验收记录 SHA / 代码 HEAD"歧义）

| 对象 | SHA |
|---|---|
| 首次 FAIL 验收记录 commit | `203862d` |
| lint 机械修复（F841/RUF059，无行为变化） | `7c9a502` |
| M10-R 修复主体（R1+R2+R3） | `8867558` |
| M10-R4 追加修复（needs_review 产物重启恢复） | `fa37bd3` |
| 复验扫描运行时的代码 HEAD | `fa37bd3`（812 tests 绿 + ruff 净） |
| 本验收记录更新 commit | 见最终报告（记录本身提交后的新 SHA） |

## M10-R-1｜P0 修复方式（M10-BUG-1）

- **schema v1→v2**：新增 `insight_context_packs` 元数据表（`(insight_source_id, source_revision) PK`，`ref_count` 可为 0）。全部 DDL `IF NOT EXISTS`，旧库打开时前向自动补建，`user_version` 只升不降。
- `replace_context_refs`：pack 元数据行与 refs **同事务** upsert——`refs=[]`（NO_RELEVANT_PERSONAL_CONTEXT）也是一等持久化对象；无 sentinel 假 cognition ref。
- `get_context_pack`：改由元数据表权威读取，不再从 ref 行反推；检索未发生返回 None（语义保留）。
- `service.py`：retrieval 守卫改为 pack 行判定（零 refs 条目重启不再重检索）；卡片写出路径取一次性 pack 引用 + 显式不变量错误，杜绝 `.pack_id` 空解引用。
- TDD 六条验收全绿：零 refs→合法 pack_id；close/reopen 后 `revision→pack_id→refs=[]` 成立；未检索=None 保留；新 revision 不继承旧 pack；DEEP_READ 全链不崩；卡面带 `ctxpack.*` provenance。

## M10-R-2｜昂贵阶段 restart guard 全清点

| 阶段 | 机制 | 状态 |
|---|---|---|
| candidate / value_gate | 决策按 (source, revision) 持久化，幂等复用 | 原有 ✓ |
| retrieval（query_plan+select） | pack 元数据行存在即跳过 | **R1 修复** ✓ |
| evidence_extract | `_stage_done` + EvidencePack 全量 JSON 持久化 | 原有 ✓ |
| thinking + critic | ThinkingOutcome 完整审计链持久化（`thought:<sid>:<revision>`，run 行带 output_ref）+ 终态守卫 | **R2 新增** ✓ |
| card write | 幂等原子写 + **卡片行落库 `insight_cards`**（`ON CONFLICT` 覆盖；顺带修复 digest/doctor 数据源空缺） | **R2 补齐** ✓ |

- **复验实抓缺口（fa37bd3）**：thinking run 以质量状态落库，守卫最初只认 "passed"——needs_review 卡片重启会重跑 thinking+critic（生产实测 52258 thinking×2）。已修：needs_review 也是成功完成的终态，恢复产物不重跑；blocked 仍可重试。
- 回归测试：卡崩→重启→零重复模型调用→卡片恰一张；needs_review 重扫零新增；outcome JSON 往返保留完整审计链。
- **诚实记录**：Batch-2 的 4 个历史崩溃条目无产物可恢复（旧代码未持久化 thinking），本次复验前重置其下游状态全链重跑（重付一次 thinking，如实计入成本）；修复后的未来崩溃可从产物直续。

## M10-R-3｜P1 修复方式（M10-CAL-01）

- `DeepValueGate.evaluate`：**一次有界 contract repair**——仅契约违约（缺必填字段/enum 非法/结构坏）触发；同输入 + `previous_violation` 明确告知违反字段 + 完整 schema 重申；第二次仍非法 → `BLOCKED_MODEL_BAD_OUTPUT` fail-closed，且 service 落一条 `deep_value_gate/blocked` run 行供统计；parser 绝不猜测 decision；rate limit / 子进程瞬态错误不走 repair。
- 审计四元组 `first_attempt_valid / repair_attempted / repair_result / first_violation` 持久化于 `decision_json.contract_repair`。

## M10-R-4｜复验扫描（真实生产 Shadow，16 条 / 7 频道）

样本（按用户 R4 规定，不换困难样本）：4 已知崩溃条目（重置下游全链重跑）+ 1 gate 两次未决（52261）+ 4 个 Batch-1 坏输出条目（tg_ai_nav 3216/3247/3253/3258）+ 2 终态抽查（25080/11817）+ 5 个 watcher 自然新到货（v2ex:10698、chuhai:11821、side_hustle:25085、zaihua:44100/44101）。耗时 1906s。

**P0 验收：4/4 已知 DEEP_READ 条目全链完成 → 卡片真实落盘（5 张含 44100）**；空 pack 全链走通；跨进程重启幂等 **5/5 = 0 新模型调用**（2 张 needs_review 卡从产物恢复 + 3 终态短路）。

**P1 验收（分层，不隐藏）**：

| 口径 | 首过尝试 | 修复触发 | 修复成功 | 终态 blocked |
|---|---|---|---|---|
| 全样本 | 11 | 4 | 4 | **0** |
| 其中：故意保留的历史坏输出条目（tg_ai_nav×4 + 52261） | 5 | 4 | 4 | 0 |
| **自然条目（5 新到货 + 2 终态抽查首评）** | 7 | **0** | — | 0 |

- 4 次修复全部来自 Batch-1 已知坏输出条目（同类 `missing decision field`），修复后终态 WATCH——**这些 payload 的首过违约是持续性的模型契约弱点**（保留为生产可靠性观察项）；自然分布本批 0/7 首过合法，远优于 <10% 目标，但样本小，继续观察。
- 卡片总账：5 张全部 `needs_review`、`cognition_delta=NONE` → **0 proposal**（设计正确：needs_review 与 NONE 都不得产生 proposal）；`approved_cognition=0`（无 ADOPT）。

## M10-R-5｜R5 卡片验收类型（自然出现，不人工造）

| 类型 | 结果 |
|---|---|
| passed card | **NOT_OBSERVED**（5/5 needs_review——critic 严格度与 M9 一致） |
| needs_review card | ✓ 5 张（抽检 11803 AiToEarn 卡：H1=标题≠结论、provenance 带 `ctxpack.*`、诚实空连接、机制保留+证据要求清晰） |
| WATCH / ARCHIVE_ONLY | ✓ 7 条 + REJECT NOT_OBSERVED（本批样本全是历史 candidate-true 或问题条目，自然不出现） |
| NO_RELEVANT_PERSONAL_CONTEXT | ✓ 5/5 深读条目（诚实空 pack，非假连接） |
| cognition delta / business opportunity / proposal | NOT_OBSERVED（delta 全 NONE，0 proposal） |

## M10-R-6｜Personal Retrieval Acceptance（R6）

- 已配置并实际使用**真实、非空**个人知识源：`KI_INSIGHT_RETRIEVAL_CMD` → `KnowledgePipeline/insight/retrieval_cmd.py`（approved_cognition + Obsidian 442 篇 .md 只读检索，笔记诚实标 TENTATIVE，不写生产 cognition，两个数据源零写入）。直接查询实测非空（如"民宿 定价"命中开业 SOP V1.1 与能力卡片）。
- 生产实测：检索层正常返回候选，但 5/5 深读条目 selector 判定全 NONE → 卡片诚实呈现"无个人背景记录"。**按用户硬门：源已真实参与，不再 BLOCKED_EXTERNAL；但"生产个人连接质量"仍属 NOT_YET_OBSERVED**——selector 对弱相关 GitHub_KB 条目拒绝强连，是精度优先行为，校准项保留。

## M10-R-7｜V2 边界快速复核（R7）

doctor 0 FAIL / 2 WARN（两条均既有事项）；watcher PID 999 存活持续收消息；telegram 库 insight 表 0；downloads 69 / handoff 4 不变；K2C 语义零代码变更。

## M10-R-8｜M10 最终 Verdict

**PASS WITH CALIBRATION ITEMS**——P0 根因修复并在生产实证（4/4 出卡、重启零重复付费、空 pack 一等持久化）；P1 修复后终态错误 0、自然流 0/7 首过合法；工程边界（幂等/隔离/Human Gate/fail-closed）在修复后代码上全部保持。三项透明校准/可靠性项：

1. **CAL-1（可靠性观察）**：历史坏输出 payload（tg_ai_nav 类）首过违约仍持续，repair 100% 兜底但生产需持续跟踪首过率（目标 <10%）；
2. **CAL-2**：生产个人连接质量 NOT_YET_OBSERVED（5/5 诚实空连接；随 approved_cognition 增长与笔记匹配度提升再验）；
3. **CAL-3**：passed card 与 proposal/Human Gate 全链在生产 NOT_OBSERVED（critic 严格度沿 M9 结论，待自然出现或专项校准）。

**STOP：不进入 M10-P**，等用户对 Production Shadow Review 的最终裁定。
