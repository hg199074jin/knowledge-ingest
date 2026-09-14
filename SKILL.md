---
name: knowledge-ingest
description: >-
  多源知识摄取总控：把本地文件、百度网盘、夸克网盘的资料统一送入
  media-transcriber → docchunk → cangjie-skill / personal-capability-distiller，
  形成可验证 Corpus、可断点续跑的 Job、人工确认门与最终报告。
  TRIGGER: 用户要求"把百度网盘/夸克网盘/本地的 XX 做成 skill"、"学习/蒸馏某个课程或资料"、
  "继续刚才那个知识摄取任务"、"刚才那个课程处理到哪一步了"。
  DO NOT TRIGGER: 单纯的文档格式转换（用 MinerU/Pandoc/OfficeCLI）、
  已在 Cangjie/Personal 流程内部的请求、与知识摄取无关的网盘操作。
allowed-tools: Bash, Read, Glob, Grep, Write, Edit, AskUserQuestion
---

# knowledge-ingest — 多源知识摄取总控 Skill

**机器负责搬运、解析、验证、调度和恢复；用户只负责知识判断与最终确认。**

Python CLI（`knowledge-ingest`）负责状态、指纹、路由、调用 media-transcriber/docchunk、
缓存与报告；本 Skill 负责理解用户意图、调用云盘 Skill、调用蒸馏 Skill、维护确认门。
职责边界详见 `references/handoff-contracts.md`。

## 触发语义

- "把百度网盘里的 XX 做成 skill"
- "把夸克网盘这个课程学习掉并蒸馏"
- "处理这个本地 PDF，做成 Cangjie Skill"
- "继续刚才那个知识摄取任务"
- "刚才那个课程处理到哪一步了"

## 主流程（严格决策树）

```text
1.  doctor                       # 环境检查：knowledge-ingest doctor [--config]
2.  resume                       # knowledge-ingest resume：列出全部 Job 的下一步
3.  create / next                # 无 Job → job create；未完成 Job → next --json 恢复
4.  source acquire / init+register   # 云盘 Skill 下载 → source init 生成 handoff → register
5.  route                        # 文件类型分流（documents/media/unsupported）
6.  preprocess（后台）           # 媒体转写 → Document Set → docchunk split → verify
7.  🔴 STOP：确认 docchunk verify PASS  # 硬门；FAIL 一律 BLOCKED，禁止蒸馏
8.  distill prepare + target start    # --target cangjie|personal|family_router|k2c（一次只一个 RUNNING）
9.  invoke target skill          # 按 references/handoff-contracts.md 传 target handoff
10. 🔴 CHECKPOINT：gate enter / 真实用户确认 / gate resolve
    # 确认门映射见 references/target-gates.md；白名单 operational gate 可用
    # gate preauthorize 发 grant，夜间 resolve --preauthorization 引用（规格 10）
11. target complete [--pipeline-state PATH]
12. next                         # 依赖≠排序：cangjie [] / personal [] / family_router [cangjie] / k2c []；
                                 # 声明顺序=执行顺序
13. final report                 # knowledge-ingest report JOB → reports/final.md（含"运行审计"段）
```

每次会话开始时：先 `doctor`，再 `resume`（一条命令给出全部 Job 的下一步）；
有未完成 Job 按 `references/recovery.md` 恢复，绝不重跑已完成阶段。

## 绝对禁止行为

```text
禁止重新实现 OCR/ASR/chunking
禁止 verify FAIL 后蒸馏
禁止伪造用户确认（gate resolve 只能来自真实用户回复或本人白天发放的 preauthorization grant）
禁止读取云盘 Token/Cookie/授权码配置（~/.config/bdpan 等一律不碰）
禁止自动删除/移动云端源文件
禁止自动上传蒸馏结果
禁止把百度能力范围描述成整个个人网盘（仅 /apps/bdpan 与分享链接）
禁止用 Quark 5 条 preview 代替完整 Search/Browse Artifact
禁止把多课程粗暴拼成一个失去来源边界的 Markdown
禁止修改 media-transcriber / docchunk / cangjie-skill / personal-capability-distiller 核心代码
禁止 KI 转码或猜测文件编码（TextEncodingError → BLOCKED 交用户处理；这是 v0.3 批准的
  最小例外——只预检上报、不转码；docchunk 其余红线不变）
```

## CLI 速查

```bash
knowledge-ingest doctor --config ./config.yaml [--json]
knowledge-ingest job create --provider local|baidu|quark --source "..." \
  --target cangjie [--target personal] [--with-router [family_router]] --prompt "..."
  # 把资料编译成可给 Agent调用的能力（默认能力生产路径）：
  #   knowledge-ingest job create --target k2c --prompt "把这些课程学习一下，形成 Agent 能力"
knowledge-ingest resume [--job JOB] [--exec]   # 全部 Job 的下一步；--exec 自动续跑第一个
knowledge-ingest source init JOB --provider来源自动 [--remote-path ...] [--local-path DIR] [--note ...]
knowledge-ingest source register JOB --handoff source.json
knowledge-ingest job amend JOB --add-target cangjie|personal|family_router  # preprocess 运行中会被锁拒绝
knowledge-ingest route JOB [--exclude PATH ...]
knowledge-ingest preprocess JOB          # 长任务：放后台跑，轮询 status（每文件实时落盘）
knowledge-ingest distill prepare JOB \
  --target cangjie|personal|family_router|k2c   # 脚手架 distill 工作区 + handoff
  # k2c handoff 自带 corpus 指纹 + verify_status + budget；消费方 = k2c build --handoff
knowledge-ingest status JOB [--json]
knowledge-ingest next JOB --json
knowledge-ingest target start JOB --target cangjie   # CORPUS_READY -> TARGET_RUNNING
knowledge-ingest gate enter JOB --target cangjie --name GATE
knowledge-ingest gate resolve JOB --target cangjie --name GATE \
  --decision confirmed|rejected [--preauthorization GRANT_ID]  # 引用 grant 时不再传 value
knowledge-ingest gate preauthorize JOB --target cangjie \
  --name stage5_install_location --value VALUE   # 仅白名单 gate（规格 10；知识门拒绝预授权）
knowledge-ingest budget acquire JOB --target T --host HOST --case-id C --request-id R
knowledge-ingest budget outcome JOB --permit PERMIT --result success|empty|rate_limit
knowledge-ingest budget amend JOB --target T --max-external-calls N   # 改上限 ≠ 自动恢复 BLOCKED
knowledge-ingest target checkpoint JOB --target T --phase PHASE [--checkpoint PATH] [--evidence DIR]
knowledge-ingest target resume JOB --target T     # BLOCKED 显式恢复（唯一出口，规格 9）
knowledge-ingest target complete JOB --target k2c --output-path RUN_DIR
  # k2c complete 读 run 目录的 k2c-target-manifest.yaml：completed /
  # needs_review_nonblocking → COMPLETED；needs_review_blocking → 先走 gate；
  # paused_budget / failed → 拒绝（manifest 决定，Task 22 冻结映射）
knowledge-ingest target complete JOB --target cangjie --output-path PATH \
  [--pipeline-state PATH]                              # cangjie: 登记断点文件
knowledge-ingest watchdog install|status|uninstall    # 重启看门狗（LaunchAgent，动态路径）
knowledge-ingest report JOB                           # reports/final.md（含"运行审计"段）
```

## 长任务纪律

- `docchunk split` 处理 PDF 时走 MinerU，可达 10–20+ 分钟：
  **`preprocess` 必须放后台执行并轮询 `status` / `next`**，不同步阻塞会话。
- 转写与 docchunk 产物都有缓存（内容指纹 + 工具 HEAD），重复请求不会重复算力。
- 转写**逐文件落盘**：`status` 的 N/M 与 `logs/events.jsonl` 的 `media_transcribed`
  事件实时反映进度（32 条视频跑了 5 小时的真实场景验证）。
- 重启/断电后：watchdog（`knowledge-ingest watchdog install` 安装的 LaunchAgent）每 15 分钟自动
  `resume --exec`；preprocess 持 pidfile 锁，绝不与运行中进程重叠。

## 夜间模式工作流

- **operational gate 可 preauthorize**（白名单：cangjie `stage5_install_location`、
  family_router `cost_budget_confirmed`，见 references/target-gates.md）：
  白天让用户给定值（安装位置 / 预算档位）→ `gate preauthorize --value ...` 发 grant；
  夜间跑到该门时 `gate resolve --preauthorization pa_... --decision confirmed` 引用 grant
  自动过门。value 来自用户的白天决定，夜间不新增任何自由裁量。
- **knowledge gate 必须 live**（cangjie `stage0_overview` / `stage1_5_candidates`、
  Personal 全部门、family_router `necessity_gate` / `acceptance_report_reviewed` /
  `review_disposition_reviewed`）：夜间跑到知识门**自动停**（WAITING_USER），
  产出门问题 + 上下文（Review Bundle）等用户回复；`gate resolve --preauthorization`
  对知识门一律被 CLI 拒绝。watchdog 的 `resume --exec` 只续跑 preprocess，绝不替用户答门。

## 评测口径规范（规格 12）

蒸馏产物/Skill 的评测分数只允许三分口径，禁止混称：

- **Blind**：蒸馏前不知道具体题目的盲测——衡量真实泛化。
- **Regression**：对既有通过集的重跑——衡量改动没有变差。
- **Holdout**：保留集终验——衡量"终版"水平。

诚实约束：

- 改过 description/技能文本后**未重跑原通过集** → 禁止声称"终版 100%"。
- **修改过题目本身** → 历史分数作废，禁止新旧分数拼接宣传。
- 报告/审计只引用可复现的分数并标注口径；manifest 无 scores 字段时审计段不展示分数。

## 诚实边界（规格 16）

- **Budget Guard 是协议级/协作式守卫**：它约束的是"经 KI 登记的外部调用"
  （`budget acquire` → 调用 → `budget outcome`）。KI 不拦截、不监控宿主 Agent 的
  任意其他外部调用；绕过 acquire 直接调外部 API 不受预算保护，也不进审计。
- **"并行 ≤ 3"是宿主经验值**（media-transcriber/MinerU 本机资源实测），
  **不是 KI 的架构约束**；KI 层面的串行保证只有一个：同时最多一个
  RUNNING/WAITING_USER target（状态机强制）。

## 失败模式速查（if-then）

| 症状 | 一线修复 | 仍失败兜底 |
|---|---|---|
| preprocess 被重启/断电杀掉 | `knowledge-ingest resume --exec`（锁保护下自动续跑） | 手动 `status` 定位阶段后重跑 preprocess；缓存保证零重复转写 |
| 想加 distill target 但 amend 被锁拒绝 | 等 preprocess 退出再 `job amend --add-target personal` | 用 `next` 确认顺序；complete 后链式也能接上 |
| `status` 长时间 "running 0/N" | 正常——逐文件落盘后看 events.jsonl 的 media_output_ready 计数（按 run_id/outcome 统计） | 若 events 也停滞：检查 MediaTranscriber output 目录增长 |
| `BLOCKED: text_encoding_unsupported` | 文档非 UTF-8（detected_encoding 见 errors）：转码或 `route --exclude` 后重新 route | KI 不转码不猜编码（规格 14） |
| `BLOCKED`（budget_exhausted / breaker_open / case_retry_exceeded） | `budget amend JOB --target T --max-external-calls N` 改上限（改 ≠ 恢复）；限流窗口过后 breaker 由 success 双清零 | `target resume JOB --target T` 显式恢复；语义见 references/recovery.md |
| source.json 手写易错 | `source init --local-path DIR --remote-path apps/bdpan/...`（校验存在+算指纹） | register 的完成门会拦下坏 handoff，按报错修字段 |
| quark CLI 报 code -104 | 所有 quark 调用加 `CLAUDECODE=1` 前缀（配置按 Agent 身份分桶） | 见 references/cloud-sources.md |
| distill 工作区/断点文件遗漏 | `distill prepare JOB --target X`（幂等，不覆盖已有 PIPELINE_STATE） | 手工补 books/ 目录与 handoff，契约见 handoff-contracts.md |
| doctor FAIL | 按检查项修环境（ORICO/项目/Skill） | FAIL 未清零前禁止起 Job

## 详细参考

- `references/architecture.md` — 架构与目录
- `references/routing.md` — 文件类型路由规则
- `references/cloud-sources.md` — 百度/夸克 Adapter 与范围限制
- `references/target-gates.md` — 确认门映射（Cangjie 3 处 / Personal 命名状态 / Family Router 4 门）与预授权语义
- `references/recovery.md` — 断点续跑策略
- `references/handoff-contracts.md` — Source/Target handoff 契约
- `docs/runtime-inventory.md` — 本机实测基线（升级组件后先更新）
- `docs/knowledge-ingest-design-v1.md` / `docs/knowledge-ingest-implementation-v1.md` — 设计与实施
