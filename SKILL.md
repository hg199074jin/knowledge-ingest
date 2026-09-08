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
8.  distill prepare + target start    # 脚手架 distill 工作区 → CORPUS_READY → DISTILLING_*
9.  invoke target skill          # 按 references/handoff-contracts.md 传 target handoff
10. 🔴 CHECKPOINT：gate enter / 真实用户确认 / gate resolve   # 确认门映射见 references/target-gates.md
11. target complete [--pipeline-state PATH]
12. next                         # 双 target 时先 Cangjie 后 Personal
13. final report                 # knowledge-ingest report JOB → reports/final.md
```

每次会话开始时：先 `doctor`，再 `resume`（一条命令给出全部 Job 的下一步）；
有未完成 Job 按 `references/recovery.md` 恢复，绝不重跑已完成阶段。

## 绝对禁止行为

```text
禁止重新实现 OCR/ASR/chunking
禁止 verify FAIL 后蒸馏
禁止伪造用户确认（gate resolve 只能来自真实用户回复）
禁止读取云盘 Token/Cookie/授权码配置（~/.config/bdpan 等一律不碰）
禁止自动删除/移动云端源文件
禁止自动上传蒸馏结果
禁止把百度能力范围描述成整个个人网盘（仅 /apps/bdpan 与分享链接）
禁止用 Quark 5 条 preview 代替完整 Search/Browse Artifact
禁止把多课程粗暴拼成一个失去来源边界的 Markdown
禁止修改 media-transcriber / docchunk / cangjie-skill / personal-capability-distiller 核心代码
```

## CLI 速查

```bash
knowledge-ingest doctor --config ./config.yaml [--json]
knowledge-ingest job create --provider local|baidu|quark --source "..." --target cangjie [--target personal] --prompt "..."
knowledge-ingest resume [--job JOB] [--exec]   # 全部 Job 的下一步；--exec 自动续跑第一个
knowledge-ingest source init JOB --provider来源自动 [--remote-path ...] [--local-path DIR] [--note ...]
knowledge-ingest source register JOB --handoff source.json
knowledge-ingest job amend JOB --add-target cangjie|personal   # preprocess 运行中会被锁拒绝
knowledge-ingest route JOB [--exclude PATH ...]
knowledge-ingest preprocess JOB          # 长任务：放后台跑，轮询 status（每文件实时落盘）
knowledge-ingest distill prepare JOB --target cangjie|personal  # 脚手架 distill 工作区 + handoff
knowledge-ingest status JOB [--json]
knowledge-ingest next JOB --json
knowledge-ingest target start JOB --target cangjie   # CORPUS_READY -> DISTILLING_*
knowledge-ingest gate enter JOB --target cangjie --name GATE
knowledge-ingest gate resolve JOB --target cangjie --name GATE --decision confirmed|rejected
knowledge-ingest target complete JOB --target cangjie --output-path PATH \
  [--pipeline-state PATH]                              # cangjie: 登记断点文件
knowledge-ingest report JOB
```

## 长任务纪律

- `docchunk split` 处理 PDF 时走 MinerU，可达 10–20+ 分钟：
  **`preprocess` 必须放后台执行并轮询 `status` / `next`**，不同步阻塞会话。
- 转写与 docchunk 产物都有缓存（内容指纹 + 工具 HEAD），重复请求不会重复算力。
- 转写**逐文件落盘**：`status` 的 N/M 与 `logs/events.jsonl` 的 `media_transcribed`
  事件实时反映进度（32 条视频跑了 5 小时的真实场景验证）。
- 重启/断电后：`com.sandro.ki-resume` watchdog（模板在仓库 `ops/`）每 15 分钟自动
  `resume --exec`；preprocess 持 pidfile 锁，绝不与运行中进程重叠。

## 失败模式速查（if-then）

| 症状 | 一线修复 | 仍失败兜底 |
|---|---|---|
| preprocess 被重启/断电杀掉 | `knowledge-ingest resume --exec`（锁保护下自动续跑） | 手动 `status` 定位阶段后重跑 preprocess；缓存保证零重复转写 |
| 想加 distill target 但 amend 被锁拒绝 | 等 preprocess 退出再 `job amend --add-target personal` | 用 `next` 确认顺序；complete 后链式也能接上 |
| `status` 长时间 "running 0/N" | 正常——逐文件落盘后看 events.jsonl 的 media_transcribed 计数 | 若 events 也停滞：检查 MediaTranscriber output 目录增长 |
| source.json 手写易错 | `source init --local-path DIR --remote-path apps/bdpan/...`（校验存在+算指纹） | register 的完成门会拦下坏 handoff，按报错修字段 |
| quark CLI 报 code -104 | 所有 quark 调用加 `CLAUDECODE=1` 前缀（配置按 Agent 身份分桶） | 见 references/cloud-sources.md |
| distill 工作区/断点文件遗漏 | `distill prepare JOB --target X`（幂等，不覆盖已有 PIPELINE_STATE） | 手工补 books/ 目录与 handoff，契约见 handoff-contracts.md |
| doctor FAIL | 按检查项修环境（ORICO/项目/Skill） | FAIL 未清零前禁止起 Job

## 详细参考

- `references/architecture.md` — 架构与目录
- `references/routing.md` — 文件类型路由规则
- `references/cloud-sources.md` — 百度/夸克 Adapter 与范围限制
- `references/target-gates.md` — 确认门映射（Cangjie 3 处 / Personal 命名状态）
- `references/recovery.md` — 断点续跑策略
- `references/handoff-contracts.md` — Source/Target handoff 契约
- `docs/runtime-inventory.md` — 本机实测基线（升级组件后先更新）
- `docs/knowledge-ingest-design-v1.md` / `docs/knowledge-ingest-implementation-v1.md` — 设计与实施
