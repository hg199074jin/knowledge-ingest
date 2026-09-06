# knowledge-ingest

[![Tests](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml/badge.svg)](https://github.com/hg199074jin/knowledge-ingest/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/hg199074jin/knowledge-ingest)](https://github.com/hg199074jin/knowledge-ingest/releases)

[English](README.md)

macOS（Apple Silicon）上的多源知识摄取与能力蒸馏流水线。把**本地文件、
百度网盘、夸克网盘**的资料统一送入现有能力：

```text
media-transcriber（视频/音频 → Markdown）
docchunk（长文档 → 可验证 Corpus）
cangjie-skill（方法论蒸馏为可执行 Agent Skill）
personal-capability-distiller（能力卡/SOP/Prompt → Obsidian 能力库）
```

`knowledge-ingest` 本身不做 OCR/ASR/chunking/蒸馏——它只提供**编排、持久状态、
断点恢复、去重、缓存、人工确认门与最终报告**。

```text
百度/夸克/本地资料 → 可验证 Corpus → Agent Skill + 个人能力资产
```

## 工作方式

```text
云盘 Skill（baidu-drive / quarkclouddrive）下载到 Job source 目录
  → Source Handoff（canonical source.json）
  → 文件类型路由（documents / media / unsupported）
  → 媒体转写（按内容指纹 + 工具版本缓存）
  → Document Set（symlink + provenance map，绝不拼成一个失去来源的 Markdown）
  → docchunk split → verify PASS（硬门）
  → cangjie-skill → personal-capability-distiller（串行，人工确认门）
  → 最终报告（强制脱敏）
```

核心保证：

- **`docchunk verify` FAIL 即阻断蒸馏**——由显式状态机结构性保证，不靠自觉。
- **绝不伪造人工确认**。gate resolve 只能来自真实用户回复，全程留痕。
- **凭据零落地**。不读取云盘 Token/Cookie；自由文本 prompt 与报告强制过脱敏器；
  事件日志为结构化脱敏 JSON。
- **跨来源去重**。百度与夸克下载到相同内容时复用同一 verified Corpus
  （cache key = handoff 指纹 + 工具 HEAD + 配置）。
- **可断点续跑**。每个阶段跃迁原子落盘；`next` 永远返回精确的下一步，
  绝不重跑已完成阶段。

## 安装

```bash
git clone git@github.com:hg199074jin/knowledge-ingest.git
cd knowledge-ingest
uv sync
uv run knowledge-ingest doctor --config ./config.example.yaml
```

可选 CLI wrapper（`~/.local/bin/knowledge-ingest`）：

```zsh
#!/bin/zsh
cd /path/to/knowledge-ingest || exit 1
exec uv run knowledge-ingest "$@"
```

## 快速上手

```bash
knowledge-ingest job create --provider local --source /path/book.pdf --target cangjie
knowledge-ingest source register JOB --handoff source.json   # 云盘下载完成后
knowledge-ingest route JOB
knowledge-ingest preprocess JOB       # 长任务：放后台跑，轮询 status
knowledge-ingest status JOB
knowledge-ingest next JOB --json
knowledge-ingest target start JOB --target cangjie
knowledge-ingest gate enter JOB --target cangjie --name GATE
knowledge-ingest gate resolve JOB --target cangjie --name GATE --decision confirmed
knowledge-ingest target complete JOB --target cangjie --output-path PATH
knowledge-ingest report JOB
```

作为 Agent Skill 的自然语言入口："把百度网盘里的 XX 做成 skill"、
"把夸克网盘这个课程学习掉并蒸馏"、"继续刚才那个知识摄取任务"、
"刚才那个课程处理到哪一步了"——见 [SKILL.md](SKILL.md)。

## 仓库结构

| 路径 | 用途 |
|---|---|
| `src/knowledge_ingest/` | Python CLI（标准库 + PyYAML + pydantic，uv 管理） |
| `SKILL.md` | Agent Skill 入口 |
| `references/` | 架构、路由、云源、确认门映射、恢复策略、handoff 契约 |
| `schemas/` | Source/Target handoff 契约示例 |
| `docs/` | 设计文档 v1.1、实施计划 v1.1、Runtime Inventory、验收记录 |
| `tests/` | 单元 + 集成测试（全程 TDD） |

## 文档

- [设计文档 v1.1](docs/knowledge-ingest-design-v1.md)
- [实施计划 v1.1](docs/knowledge-ingest-implementation-v1.md)
- [验收记录](docs/acceptance-v1.md)

## 相关项目

- [docchunk](https://github.com/hg199074jin/docchunk) — 可验证长文档 Corpus
- [media-transcriber](https://github.com/hg199074jin/media-transcriber) — 本地 ASR → 结构化 Markdown

## 许可

[MIT](LICENSE)
