# knowledge-ingest

多源知识摄取与能力蒸馏流水线（macOS / Apple Silicon）。

把**本地文件、百度网盘、夸克网盘**的资料统一送入现有能力：

```text
media-transcriber（视频/音频 → Markdown）
docchunk（长文档 → 可验证 Corpus）
cangjie-skill（方法论蒸馏为可执行 Agent Skill）
personal-capability-distiller（沉淀个人能力卡/SOP/Prompt → Obsidian 能力库）
```

`knowledge-ingest` 本身不做 OCR/ASR/chunking/蒸馏——它只做**编排、状态、
断点恢复、去重、缓存、人工确认门与最终报告**。

```text
百度/夸克/本地资料 → 可验证 Corpus → Agent Skill + 个人能力资产
```

## 安装

```bash
cd /Volumes/ORICO/Projects/knowledge-ingest
uv sync
uv run knowledge-ingest doctor --config ./config.example.yaml
```

Skill 全局注册（实体库 symlink + 视图同步）：

```bash
ln -sfn /Volumes/ORICO/Projects/knowledge-ingest ~/.agents/skills/knowledge-ingest
~/.local/bin/skill-sync        # 体检；必要时 skill-sync fix
```

可选 CLI wrapper（`~/.local/bin/knowledge-ingest`）：

```zsh
#!/bin/zsh
cd /Volumes/ORICO/Projects/knowledge-ingest || exit 1
exec uv run knowledge-ingest "$@"
```

## 快速上手

```bash
knowledge-ingest job create --provider local --source /path/book.pdf --target cangjie
# 云端：Agent 用 baidu-drive / quarkclouddrive 下载后生成 source.json 再 register
knowledge-ingest source register JOB --handoff source.json
knowledge-ingest route JOB
knowledge-ingest preprocess JOB        # 长任务请放后台，轮询 status
knowledge-ingest status JOB
knowledge-ingest next JOB --json
knowledge-ingest report JOB
```

自然语言入口（经 Skill）：*"把夸克网盘'审计课程/融资担保'学习掉，做成 Skill，
并沉淀成我的个人能力。"* 用户只在确认门处参与判断。

## 目录

- `src/knowledge_ingest/` — Python CLI（uv 管理，stdlib + PyYAML + pydantic）
- `SKILL.md` — Agent Skill 入口；`references/` — 详细规则
- `schemas/` — Source/Target handoff 契约示例
- `docs/` — 设计文档 V1.1、实施计划 V1.1、Runtime Inventory
- `tests/` — 单元 + 集成（TDD 全程）

## 安全边界

- 不读取/不落盘任何云盘 Token/Cookie/授权码；日志与报告强制脱敏。
- 默认只下载，不删除/移动/上传云盘文件；本地原始文件只读。
- 百度仅支持 `/apps/bdpan` 应用目录与分享链接；越界请求显式 BLOCKED。
- `docchunk verify` FAIL 一律 BLOCKED，绝不进入蒸馏。
- 用户确认门绝不允许伪造。

## 许可

私有项目（hg199074jin）。
