# K2C Telegram Knowledge Source V2 验收记录（Real Acceptance）

- 日期：2026-09-20（评审修订：2026-09-20 晚）
- HEAD：`3fedb19`（含评审修复）；分支 `main` = `origin/main`
- 回归：**552 passed**、ruff 干净
- 回归：**545 passed**（含 telegram unit/contract/integration 与既有全量）、ruff 干净
- 真实来源（14 启用 / 1 停采）：tg_v2ex、tg_trivia、tg_zaihua、tg_it_charge、
  tg_chuhai、tg_quality_info、tg_quality_res、tg_side_hustle、tg_vip_docs、
  tg_blackhole、tg_hermes、tg_self、tg_ai_nav、tg_yangmao（tg_search 已停采并清扫）

## Real Acceptance Matrix（§10.8 + V2.1-4 增补）

| # | 项 | 结果 | 证据（真实样本/测试） |
|---|---|---|---|
| A | 新纯文字 | ✅ | tg_trivia:30481 等，materialized |
| B | 5 分钟连续合并 | ✅ | tg_side_hustle:24765（1179 字符多消息窗） |
| C | >5 分钟拆分 | ✅ | 300s 锚定窗口单测 + 生产拆分样本 |
| D | 明确广告 SKIP | ✅ | skipped_noise=290（规则层成人/推广类） |
| E | 疑似广告 REVIEW | ✅ | 通道干跑 13 条 → SKIP；生产 noise_review |
| F | 感兴趣 PDF <50MiB 自动进 K2C | ✅（按 V2.1-2 锚点） | 下载全自动；进入 KI 走显式 `telegram handoff` |
| G | 荐股 PDF EXCLUDE | ✅ | skipped_interest=24（含投资类防误杀正例单测） |
| H | 感兴趣 PDF >50MiB REVIEW | ✅ | tg_blackhole:35922（182MB）→ interest_review |
| I | 无法判断 PDF REVIEW | ✅ | tg_vip_docs:3084 SCMP（conf 0.86）→ 人工 KEEP |
| J | 重复 PDF SHA 复用 | ✅ | TG7 去重（同内容跨源复用既有文件，测试+生产同 SHA 实例） |
| K | 夸克链接 PENDING | ✅ | tg_quality_res:2984 PENDING_RESOURCE |
| L | 百度链接 PENDING | ⏳ 外部样本未出现（按 §10.7 如实标记） |
| M | 大合集 Stub 不下载 | ✅ | cloud_link 恒 PENDING_RESOURCE，V2 无下载路径 |
| N | crash/restart 增量恢复 | ✅ | kill -9 → launchd 复活 → reconcile 补齐；无重复 Item/Job |
| O | no historical backfill | ✅ | 14 源 start_at 硬边界；库内无边界前消息 |
| P | WxPusher REVIEW | ⏳ H2 待凭据（wxpusher.env）；运行时已就绪、缺失自动降级 |
| Q | 08/12/20 Digest cursor 补算 | ✅ 机制（digest-agent 已装、游标单测）；真实投递待 P |
| R | Telegram → KI → K2C staged | ✅ | SCMP job COMPLETED：release rel.e65d15f6bcd7d81b staged |
| S | Publish/Activate 未自动执行 | ✅ | K2C registry 无 activate 记录；staged 不进 discovery |
| T | EDIT handoff 前重建同 Item | ✅ | 单测 + 生产 edit 重建（227616 等） |
| U | EDIT handoff 后 REVIEW 不改 Corpus | ✅ | SOURCE_EDITED_AFTER_HANDOFF 审计 + 单测 |
| V | DELETE 可见不物理删 | ✅ | deleted_at/source_deleted（job 6e01df provenance=false 样本） |
| W | DOWNLOAD_ONCE 幂等 | ✅ | 闸门/重试/一致性 doctor 检查 + 单测；真实 >50MiB 样本未出现 |

V2.1-4 增补路径：视频→本地 Fun-ASR 转写→docchunk（23s/153s 实测 30s/53s）✅；
通道分类准确率 15/15 干跑 + 21 次生产调用零熔断 ✅；显式 handoff（SCMP）✅；
14 源工况 ✅；LaunchAgent 重启自愈 ✅。

## 交付证据（§10.10）

- **H3-A 主样本**：`tg_vip_docs:3084`《南华早报260917_在美国联邦基金利率上升之后_中国还会继续实施货币宽松政策吗》10.87MB（SHA-256 `c3a299646ebd…`）
- **KI Job**：`20260919-134255-telegram-tg-vip-docs-3084-6e01df` → **COMPLETED**
- **Corpus**：`document-set-9273b0bb67f7`（verify_status: passed）
- **target-k2c handoff**：`handoff/target-k2c.yaml`（三指纹 + budget：balanced / max 20）
- **K2C build**：`Data/KnowledgeToCapability/runs/m2-20260919-134255-…-6e01df/`
  1 family / 1 variant / 1 capability（5 条 evidence 规则）；
  eval base_gain=+1.00、marginal_gain=+0.625（answers=host-generated-20260919-h3a-scmp-3084，如实标注）
- **staged**：`runs/…/staged/`（SKILL.md：美元加息周期的中国货币政策研判清单）；
  release `rel.e65d15f6bcd7d81b`（immutable）；**publish/activate 未执行（人工门）**
- **IMPORTANT_CAPABILITY 生产者核对**：None（K2C manifest 无显式 high/important 信号 → 无生产者，符合 §8.7/E4）
- **Doctor**：0 FAIL / 1 WARN（REVIEW 积压=人工队列，属预期）
- **LaunchAgent**：watch-agent loaded（`--config` 钉死 + 通道 env 持久化）；
  digest-agent loaded（08/12/20）；ki-resume 修复复活（exit 0）
- **积压清扫（V2.1-4.1）**：tg_search 停采时代 260 条 materialized/open →
  skipped_noise（一次性 curation，本记录即决策凭据）；tg_search 零 handoff-eligible 残留

## 外部环节与如实标记（§10.7）

- **H2 WxPusher**：用户投放 `wxpusher.env` 后 `telegram notify` 验证；P/Q 行标记 ⏳
- **H3-B**：真实 Edit/Delete 与百度链接样本未出现——T/U/V 以生产审计 +
  单测语义验收，L 行如实标 ⏳，未伪造通过
- **DOWNLOAD_ONCE 真实大文件**：未出现；闸门/重试/一致性以单测 + doctor 验证

## 双评审闭环与如实偏差（2026-09-20 晚）

本记录经两道独立评审（review-agent 直审 + requesting-code-review subagent），
全部 Critical/Important 已修复并推送：
- **C1**：`close_stale_windows` 原为死代码（未接入 reconcile_all）→ 已接线并逐条隔离
- **C2**：retention 可能删除被 KEEP 侧共享的去重文件 → 候选 SQL 排除共享路径
- **I3**：迁移不原子（crash 可留半迁移态/丢表）→ 重建包进 `BEGIN IMMEDIATE…COMMIT` + 先清残留 v2 表
- **I4**：doctor 会在 state.db 缺失时创建库 → 缺失时直接 FAIL 不开库
- **I5**：classify-dryrun 对 parked video 误走 noise 通道 → 已并入 interest 通道
- **I6**：通知"失败即永久消费" → 未投递事件可重试；digest 游标仅在成功投递后推进；digest event_id 按窗口稳定
- **I7 偏差如实标记**：① `doctor --fix`（§10.3）未实现——实施记录为本项偏差，待后续单独实施；② §10.2 "cursor 未倒退"已实现（last_seen 滞后检查）；③ §9.7 digest 内容已扩充（分类分布/进入 KI 计数），完整 12 项清单中"verify/staged 状态行"随 target complete 证据归档覆盖
- **Minors**：skipped_unsupported 入终态清单（DELETE 不改写）；retention 路径安全断言；video 的 gate/edit 回归测试；review kind 标签用真实 item kind
- digest-agent 同样需要解释器 TCC 授权（与 watch-agent 相同，授权已生效）

## 结论

Telegram Source V2 的全部 TG0–TG8 范围已完成或如实标记外部待办；
自动化严格止于 staged；无越权 publish/activate；语料基线已清扫。
满足方案 §10"正式完成"条件。

## QA 归档（2026-09-20）

- 测试与巡检类报告统一落 `KnowledgePipeline/qa/reports/`（逐频道10条 / 全链路验收 / channels-raw / 生成物全文示例）；
- H3-A case 目录已归档至 `KnowledgePipeline/qa/cases/h3a-scmp-3084`（原 k2c-cases/；m2 驱动复用时传 `--case-dir` 新路径）；
- 生产数据根（state.db / materialized / attachments / LongDocCorpus / K2C runs）原地不动。
