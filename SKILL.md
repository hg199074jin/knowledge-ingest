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
2.  查找可恢复 Job               # 扫描 /Volumes/ORICO/KnowledgePipeline/jobs/*/job.yaml
3.  create / resume              # 无 Job → job create；有未完成 Job → next --json 恢复
4.  source acquire / register    # 云盘 Skill 下载 → 生成 source.json → source register
5.  route                        # 文件类型分流（documents/media/unsupported）
6.  preprocess                   # 媒体转写 → Document Set → docchunk split → verify
7.  确认 docchunk verify PASS    # 硬门；FAIL 一律 BLOCKED，禁止蒸馏
8.  next JOB --json              # 获取下一步动作
9.  invoke target skill          # 按 references/handoff-contracts.md 传 target handoff
10. gate enter / 用户确认 / gate resolve   # 确认门映射见 references/target-gates.md
11. target complete
12. next                         # 双 target 时先 Cangjie 后 Personal
13. final report                 # knowledge-ingest report JOB → reports/final.md
```

每次会话开始时：先 `doctor`，再扫描未完成 Job；有则按 `references/recovery.md` 恢复，
绝不重跑已完成阶段。

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
knowledge-ingest source register JOB --handoff source.json
knowledge-ingest route JOB [--exclude PATH ...]
knowledge-ingest preprocess JOB          # 长任务：放后台跑，轮询 status
knowledge-ingest status JOB [--json]
knowledge-ingest next JOB --json
knowledge-ingest gate enter JOB --target cangjie --name GATE
knowledge-ingest gate resolve JOB --target cangjie --name GATE --decision confirmed|rejected
knowledge-ingest target complete JOB --target cangjie --output-path PATH
knowledge-ingest report JOB
```

## 长任务纪律

- `docchunk split` 处理 PDF 时走 MinerU，可达 10–20+ 分钟：
  **`preprocess` 必须放后台执行并轮询 `status` / `next`**，不同步阻塞会话。
- 转写与 docchunk 产物都有缓存（内容指纹 + 工具 HEAD），重复请求不会重复算力。

## 详细参考

- `references/architecture.md` — 架构与目录
- `references/routing.md` — 文件类型路由规则
- `references/cloud-sources.md` — 百度/夸克 Adapter 与范围限制
- `references/target-gates.md` — 确认门映射（Cangjie 3 处 / Personal 命名状态）
- `references/recovery.md` — 断点续跑策略
- `references/handoff-contracts.md` — Source/Target handoff 契约
- `docs/runtime-inventory.md` — 本机实测基线（升级组件后先更新）
- `docs/knowledge-ingest-design-v1.md` / `docs/knowledge-ingest-implementation-v1.md` — 设计与实施
