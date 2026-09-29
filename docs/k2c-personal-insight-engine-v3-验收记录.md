# Knowledge-Ingest V3 Personal Insight Engine — M10 生产 Shadow 验收记录

- 日期：2026-09-29
- 分支：`v3-insight-engine`（HEAD=1e8970a）
- 结论先行：**M10 Verdict = FAIL（P0 修复后需重跑 DEEP_READ 路径复验）**
- 工程边界（幂等 / V2 隔离 / fail-closed / Human Gate）全部成立；失败集中于两处已精确定位、可修复的缺陷（1×P0 代码崩溃 + 1×P1 模型输出契约可靠性）。**不进入 M10-P。**

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

## 12. STOP 声明

按用户 2026-09-29 复核指令：M10 Batch-2 完成即 STOP，**不进入 M10-P**（Cross-Agent Parity）。待 P0/P1 修复并复验后，由用户裁定是否重启 Production Shadow Review，再议 M10-P。
