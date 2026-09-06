# Runtime Inventory — knowledge-ingest 实施基线

> 按《knowledge-ingest Implementation Plan v1》Pre-Implementation Gate 要求生成。
> 采集时间：2026-09-06（本机实测，非文档转抄）。实施各 Task 的 Adapter 测试以本清单为基线。
> 若后续任一组件升级（git HEAD 变化、Skill 大版本变化），必须先更新本清单再跑相关回归。

## 1. 系统基线

| 项 | 实测值 |
|---|---|
| macOS | 26.5.1 (Build 25F80, Darwin 25.5.0) |
| 架构 | arm64 (Apple Silicon Mac mini) |
| Python | 3.12.14（uv 管理：~/.local/share/uv/python/cpython-3.12-macos-aarch64） |
| uv | 0.12.5（Homebrew，/opt/homebrew/bin/uv） |
| node | v24.19.0（mise 管理，夸克 CLI 依赖满足） |
| Homebrew Git | 身份 hg199074jin / jin_zhy@163.com，默认分支 main |

## 2. 存储与挂载

- /Volumes/ORICO 已挂载（任务依赖它，启动时仍需动态检查）。
- `/Volumes/ORICO/KnowledgePipeline/` 已存在：`cache/`、`jobs/`、`tmp/`（均为空）。
- `/Volumes/ORICO/MediaTranscriber/`：`inbox/`、`output/`、`state/`（含 SQLite 任务队列）。
- `/Volumes/ORICO/LongDocCorpus/`：已有 2 个历史 corpus（docchunk 处于活跃使用状态）。
- `/Volumes/ORICO/Obsidian/Skill_Library/`：personal-capability-distiller 的 Obsidian 能力库已就位，
  含 `00_能力地图` … `07_应用反馈` + `90_模板与配置` 完整结构。
  **注意：该 Skill 内部把 vault 根写死为 Windows 路径 `E:\Obsidian\Skill_Library`，总控必须显式 override 为本机路径。**

## 3. 核心处理项目

### docchunk — /Volumes/ORICO/Projects/docchunk

- main @ `e84e889`（版本 1.1.0，page-level smart PDF routing 已合入）。
- 安装形态：**uv 全局可编辑安装**（`~/.local/bin/docchunk` → uv tools venv），任意目录可直接 `docchunk`；
  项目内 `uv run docchunk` 亦可。缓存 key 必须含 git HEAD（可编辑安装会即时吃到仓库变更）。
- CLI（Typer，9 个子命令）：`version` / `prepare` / `split` / `verify` / `batch` / `status` / `rebuild-batches` / `doctor` / `inspect`。
- `split <file-or-directory>`：接受单文件或目录（.pdf/.docx/.md/.markdown/.txt）；**成功退出码已内含一次 verify**；
  对未变更源自动复用（不重复 OCR）。`--force` 才重建。
- `verify <corpus-path>`：位置参数传 corpus 目录，PASS 退出 0，失败退出 1 —— 硬门。
- corpus 输出：默认 `/Volumes/ORICO/LongDocCorpus/<标题≤48>-<源指纹前12>/`，
  内含 `manifest.json`、`index.jsonl`、`state.json`、`combined.md`、`source/`、`atomic/A*.md`、`batches/B*.md`。
- **长任务警告**：PDF 走 MinerU 时 split 可达 10–20+ 分钟，编排器不得同步阻塞（AGENTS.md §17），
  应后台运行并轮询 `docchunk status` / corpus 内 `logs/processing.jsonl`。

### media-transcriber — /Volumes/ORICO/Projects/media-transcriber

- main @ `096e9e3`（"fix: harden media transcription recovery and safety"）。
- 安装形态：**未全局安装**，必须在项目目录 `uv run media-transcriber`。
- CLI（Typer，5 个子命令）：`transcribe` / `doctor` / `watch` / `retry` / `status`。
- `transcribe <abs-path>`：直接收任意绝对路径，**无需先进 inbox**（inbox/watcher 仅供人工拖入场景）。
- 关键 flag：`--device`（默认 auto：MPS 优先、CPU 回退）、`--timestamp`（默认 10m；可选 none/5m/10m/15m）、
  `--language`、`--glossary`、`--hotword`、`--force`、`--keep-audio`、`--debug`。
- 产物：`/Volumes/ORICO/MediaTranscriber/output/<stem>/<stem>.md` + 同目录 `metadata.yaml`（YAML，非 JSON）；
  默认不覆盖（`--force` 才覆盖）；pipeline 强制输出位于 ORICO 卷。

## 4. Skill 布局与下游 Skill

### 布局（AGENTS.md §19）

- **唯一实体库 `~/.agents/skills/`**；`~/.zcode/skills`、`~/.claude/skills`、`~/.codex/skills` 为 symlink 视图，
  由 `~/.local/bin/skill-sync` 体检/修复。**绝不在视图目录放置实体内容。**
- 2026-09-06 已安装云端 Skill 并跑 `skill-sync fix` 同步三视图（缺链接 0）：
  - `baidu-drive` v1.7.5（bdpan CLI 3.8.7 已装；**登录授权待用户扫码完成**）。
    范围：所有文件操作限制在 `/apps/bdpan/`（"我的应用数据/bdpan"）内。
  - `quarkclouddrive` 1.0.17-ea0ddf3（CLI 入口 `scripts/quark-drive.cjs`，`--version` 自检通过）。
    语义：Search/Browse 结果以完整 Artifact 为准，预览最多 5 条不当全部候选。
- 四个下游/编排 Skill 均已安装且 `.agents` 与 `.zcode` 副本 diff 一致：
  `cangjie-skill`、`personal-capability-distiller`、`media-transcriber`、`longdoc-router`。

### cangjie-skill 接口事实

- **没有** `stage_*_confirmation` 之类的内部命名门；文档里的此类名称一律是总控自定义。
- 真实确认点 3 处：
  1. 阶段 0：展示 `BOOK_OVERVIEW.md` 骨架，用户确认后才进阶段 1；
  2. 阶段 1.5：候选清单轻确认（"这 N 个会做成 skill，有想捞回或砍掉的吗？"）；
  3. 阶段 5：**询问安装位置**（用户级或项目级；无"compile 模式"确认）。
- 输入：内容文本来源（路径或纯文本，不凭记忆蒸馏）+ 元信息（书名/作者/出版年 或 标题/讲者/发布时间）+ 是否首次试点。
- 产物：写入 **cwd 下 `books/<slug>/`**，含断点文件 `PIPELINE_STATE.md`（恢复时先读它）。
- 历史运行（books/jingyun-2026）实证过"longdoc-router 全量深读 58/58 批"的路径可行。

### personal-capability-distiller 接口事实

- 阶段口径：SKILL.md "阶段产出" 表 13 行，典型轨迹 14 阶段——**不要在总控里硬编码阶段数**。
- 档位 A/B/C：**未指定时默认 C**（C 档单次回答价值/资产/连接/今日动作，不自动生成或安装 Skill）；
  用户显式指定优先。
- 确认门为命名状态（references/workflow-states.md，9 个）：`intake`、`inventory_reviewed`、
  `faithful_reconstruction_reviewed`、`human_material_approved`、`skill_simulation_passed`、
  `installation_approved`、`applied_in_real_task`、`feedback_reviewed`、`archived`；
  4 个关键用户门：清点确认（主领域/档位）、材料定稿（明示"定稿"）、安装确认（明示授权，不自动装）。
- 输入：**只收 Markdown / 纯文本**（docchunk corpus 的 combined.md / atomic md 满足该要求）。
- 输出：Obsidian 能力库（本机路径见 §2）；vault 缺失时产物暂存 `drafts/`。

## 5. knowledge-ingest 仓库现状（评审时点）

- `/Volumes/ORICO/Projects/knowledge-ingest/` 已有目录骨架
  （src/knowledge_ingest/adapters、tests/{unit,integration,fixtures}、references、schemas、templates，仅 .gitkeep/__init__.py）。
- 评审时无 .git、无代码、无 pyproject——Task 1 从零开始，`git init` 于本仓库执行。

## 6. 与原两份文档的差异（已反映到 docs/ 修订版）

1. 云端两 Skill 原缺失 → 本轮已安装（版本见 §4），Task 11/12 结构性阻塞解除；真实下载 smoke test 仍需用户登录配合。
2. Cangjie 确认门改为真实 3 处映射（`stage0_overview` / `stage1_5_candidates` / `stage5_install_location`），
   删除 compile-mode 门；总控固定 Cangjie 运行 cwd 并登记 `PIPELINE_STATE.md`。
3. personal-distiller 档位默认 C、命名状态门、vault 路径 override。
4. skill_roots 补 `~/.zcode/skills`，doctor 按 resolve() 去重视图。
5. Task 4 runner 满足 docchunk split 长任务约束（Popen + 轮询，不同步阻塞）。
6. Task 14 安装方式改为实体库 symlink + skill-sync。
7. 字段名统一 `personal`（原设计文档 `personal_distiller`）。
