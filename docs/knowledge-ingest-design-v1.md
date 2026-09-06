# 多源知识摄取与能力蒸馏流水线设计文档

**项目建议名称：** `knowledge-ingest`
**目标平台：** macOS / Apple Silicon
**设计版本：** V1.1（2026-09-06 依本机 Runtime Inventory 校订）
**设计目标：** 将百度网盘、夸克网盘及本地文件统一接入现有 `media-transcriber`、`docchunk`、`cangjie-skill` 与 `personal-capability-distiller`，形成一条可追踪、可恢复、可验证、可人工确认的知识摄取与能力沉淀流水线。

> **修订记录（V1.0 → V1.1）**：依据 `docs/runtime-inventory.md` 的本机实测结果修订——
> ① §4 注明云端两 Skill 已安装及实际版本；② §8.3 Cangjie 确认点改为真实 3 处；③ §9 档位默认 C、
> 命名状态确认门、Obsidian vault 本机路径；④ §10/§11 字段名统一 `personal`；⑤ §17 Skill 实体库
> 架构与安装方式；⑥ 附录指向 runtime-inventory。架构与流程本身无变化。

---

## 1. 背景与目标

当前已经具备四个相对独立的知识处理能力：

1. `hg199074jin/media-transcriber`
   将视频/音频在本地转写为结构化 Markdown，并输出 metadata。

2. `hg199074jin/docchunk`
   将 PDF、DOCX、Markdown、TXT 或课程目录转换为可验证、可追溯、可恢复的长文档 Corpus，并通过 Atomic Chunk + Reading Batch 供 Agent 完整阅读。

3. `kangarooking/cangjie-skill`
   将书籍、课程逐字稿、播客、访谈等长内容中的方法论蒸馏为可执行 Agent Skills。

4. `hg199074jin/personal-capability-distiller`
   将课程、文章、书籍、项目复盘等原料进一步沉淀为个人可复用的能力卡、SOP、Prompt、候选 Skill 和能力库资产。

现在缺少的是最上层的统一编排能力。

用户希望最终可以直接对 Mac Agent 说：

> 把我夸克网盘"审计课程/融资担保"里的资料处理一下，做成 Skill，同时蒸馏到我的个人能力库。

Agent 自动完成：

```text
网盘搜索
→ 下载
→ 文件识别
→ 文档 / 媒体分流
→ 媒体转写
→ docchunk
→ verify
→ cangjie-skill
→ personal-capability-distiller
→ 输出处理报告
```

V1 的核心目标不是增加新的解析、OCR、ASR 或蒸馏算法，而是把现有能力可靠地编排起来。

---

## 2. 核心设计原则

### 2.1 现有工具各司其职，不重复造轮子

`knowledge-ingest` 只负责编排，不重新实现：

- 百度网盘下载；
- 夸克网盘下载；
- OCR；
- PDF 页级路由；
- ASR；
- 文档 Chunk；
- Skill 蒸馏；
- 个人能力蒸馏。

每个现有项目保持独立升级。

### 2.2 `docchunk Corpus` 是统一的知识中间层

所有最终需要进入知识蒸馏的长内容，原则上都先归一化为 `docchunk Corpus`。

```text
文档 ──────────────────────┐
                           ↓
                    docchunk Corpus
                           ↑
视频 / 音频 → 转写 Markdown ┘
```

下游不需要再关心原始资料是：

- PDF；
- Word；
- Markdown；
- 视频；
- 音频；
- 一整套课程。

下游只需要消费已经 `verify PASS` 的 Corpus。

### 2.3 Chunking 与 Distillation 分离

必须坚持：

> Chunking is lossless. Distillation may be lossy.

前处理阶段首先保证资料完整、来源可追溯，再进入带有抽象、筛选和方法论重构的蒸馏阶段。

### 2.4 前处理自动化，蒸馏阶段保留人工确认门

`cangjie-skill` 与 `personal-capability-distiller` 本身都存在用户确认节点。

因此 V1 不允许总控 Skill 通过伪造"用户已确认"的方式强行一键跑到底。

正确方式：

```text
自动完成下载 / 转写 / docchunk / verify
                    ↓
              Corpus Ready
                    ↓
         进入蒸馏工作流
                    ↓
       遇到原 Skill 确认门时暂停
                    ↓
             等待用户确认
                    ↓
               断点续跑
```

### 2.5 Local First

除百度/夸克网盘下载本身及模型/依赖首次获取外：

- 媒体转写尽量本地；
- PDF/OCR 按现有 docchunk 机制；
- Corpus 保存在本地；
- 不自动上传原始资料；
- 不自动把产物同步回网盘。

---

## 3. 总体架构

```text
                         User
                          │
                          ▼
                knowledge-ingest
                 Orchestrator Skill
                          │
                ┌─────────┴─────────┐
                │   Source Router   │
                └─────────┬─────────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   百度网盘             夸克网盘            Local
 baidu-drive       quarkclouddrive      local path
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
                   Local Source
                          │
                   File Type Router
                          │
            ┌─────────────┴─────────────┐
            │                           │
            ▼                           ▼
        Document                     Media
 PDF/DOCX/MD/TXT        MP4/MOV/MKV/AVI/MP3/M4A/
            │                WAV/FLAC/AAC
            │                           │
            │                  media-transcriber
            │                           │
            │                    Clean Markdown
            │                           │
            └──────────────┬────────────┘
                           ▼
                        docchunk
                           │
                      verify PASS
                           │
                    Verifiable Corpus
                           │
               ┌───────────┴───────────┐
               │                       │
               ▼                       ▼
         cangjie-skill       personal-capability-distiller
               │                       │
               ▼                       ▼
       Executable Skills        Capability Assets
               │                       │
               └───────────┬───────────┘
                           ▼
                     Final Job Report
```

逻辑上两个蒸馏出口是并列的。

但 V1 默认执行策略建议为：

```text
Corpus Ready
    ↓
cangjie-skill
    ↓
personal-capability-distiller
```

原因：

- 两者都可能需要用户确认；
- 避免同时弹出两套确认流程；
- 减少 Agent 上下文混乱；
- 避免同一大 Corpus 同时被两个重型蒸馏任务重复读取。

未来 V2 再考虑并行。

---

## 4. Source Layer 设计

### 4.1 百度网盘

使用：

```text
baidu-netdisk/bdpan-storage
Skill: baidu-drive
```

**本机状态（2026-09-06 实测）：** Skill v1.7.5 已安装于实体库 `~/.agents/skills/baidu-drive`，
bdpan CLI 3.8.7 已装；网盘登录授权待用户扫码完成。所有文件操作限制在 `/apps/bdpan/`
（"我的应用数据/bdpan"）内。

职责仅限：

```text
search
list
download
stat / metadata
```

总控默认不执行：

```text
delete
move cloud files
rename cloud files
overwrite
upload outputs
```

除非用户明确要求。

### 4.2 夸克网盘

使用：

```text
quark-clouddrive/quarkclouddrive_offical
Skill: quarkclouddrive
```

**本机状态（2026-09-06 实测）：** Skill/CLI 1.0.17-ea0ddf3 已安装于实体库
`~/.agents/skills/quarkclouddrive`，`node scripts/quark-drive.cjs --version` 自检通过。
Search/Browse 结果以完整 Artifact 为准，预览（最多 5 条）不得当作全部候选。

职责同样限定为：

```text
search
list
download
stat / metadata
```

不得读取、打印或写入日志：

- OAuth Token；
- Cookie；
- 授权码；
- 密码；
- 认证配置正文。

### 4.3 Local

支持用户直接提供：

```text
/absolute/path/to/file
/absolute/path/to/folder
```

本地输入不复制原文件，优先按只读源处理。

---

## 5. 文件类型 Router

V1 只支持已经被现有两个处理项目正式支持的格式。

### 5.1 文档类型

直接进入 `docchunk`：

```text
.pdf
.docx
.md
.txt
```

目录：

```text
folder/
```

如果目录内主要由上述受支持文档组成，则作为 Document Set 交给 `docchunk`
（`docchunk split` 原生支持目录输入）。

### 5.2 媒体类型

先进入 `media-transcriber`：

```text
.mp4
.mov
.mkv
.avi
.mp3
.m4a
.wav
.flac
.aac
```

转写完成后，将生成的 Markdown 交给 `docchunk`。
（实测 `media-transcriber transcribe` 直接收绝对路径，无需文件先进 inbox；
产物为 `output/<stem>/<stem>.md` + 同目录 `metadata.yaml`。）

### 5.3 暂不支持

例如：

```text
.epub
.ppt
.pptx
.xls
.xlsx
.html
图片目录
未知二进制文件
```

V1 不自动猜测转换方式。

处理状态记为：

```text
UNSUPPORTED
```

并在最终报告中列出。

未来通过新的 Adapter 扩展，而不是修改 Router 核心逻辑。

---

## 6. 媒体处理设计

对于单个视频/音频：

```text
Cloud Download
      ↓
Local Media
      ↓
media-transcriber transcribe
      ↓
Clean Markdown
metadata.yaml
      ↓
docchunk split
```

对于一整套课程：

```text
课程目录
├── 01.mp4
├── 02.mp4
├── 03.mp4
└── 04.pdf
```

处理方式：

```text
01.mp4 → transcript 01.md
02.mp4 → transcript 02.md
03.mp4 → transcript 03.md
04.pdf  → 原文保留
                ↓
      形成 Collection Handoff
                ↓
            docchunk
                ↓
         一个 Document Set
```

必须保留：

- 原文件名；
- 原相对路径；
- 网盘来源；
- 媒体 metadata；
- transcript 与 source 的对应关系；
- Document ID。

不要把整套课程粗暴拼成一个 Markdown 后再丢失来源边界。

---

## 7. `docchunk` 统一中间层

### 7.1 进入下游蒸馏前的硬门

只有：

```text
docchunk verify == PASS
```

才允许进入：

```text
cangjie-skill
personal-capability-distiller
```

如果 verify 失败：

```text
JOB = BLOCKED
reason = corpus_verify_failed
```

严禁继续蒸馏。

（实测说明：`docchunk split` 成功退出已内含一次 verify；总控仍显式调用
`docchunk verify` 作为独立硬门，二者不冲突。）

### 7.2 下游读取方式

对于长 Corpus：

优先使用现有：

```text
longdoc-router
```

下游 Agent 不应该：

```text
cat combined.md
```

然后假设已经读完全文。

正确方式：

```text
verify Corpus
↓
按照 B0001 → B0002 → ... 顺序读取 batches
↓
根据 frontmatter 区分：
overlap_atomic_ids
new_atomic_ids
↓
必要时通过 index.jsonl 回查来源
↓
全部 Batch 完成后再做跨文档蒸馏
```

### 7.3 Corpus 复用

同一原始资料如果已经存在：

```text
verified Corpus
```

且：

- source hash 相同；
- docchunk 配置兼容；
- Corpus 未损坏；

则优先复用。

不要重复：

- OCR；
- PDF 解析；
- Atomic Chunk；
- Corpus 构建。

（补充本机事实：docchunk 为 uv 可编辑安装且 split 对未变更源自动复用，
总控 cache key 必须包含 docchunk git HEAD 以防复用失效。）

---

## 8. Cangjie 分支

### 8.1 输入

输入必须来自：

```text
Verified docchunk Corpus
```

总控提供：

- 原内容标题；
- 作者/讲者（如果能从原资料或用户输入得到）；
- 来源；
- 发布时间/出版年（如果可得）；
- Corpus path；
- provenance；
- 用户用途；
- 是否首次试点。

（cangjie-skill 实际输入要求：内容文本来源 + 元信息（书名/作者/出版年 或
标题/讲者/发布时间）+ 是否首次试点；缺失的元信息保持 null，禁止猜测。）

### 8.2 输出

由 `cangjie-skill` 自己决定内部 RIA-TV++ 流程。

总控不得自行复制 Cangjie 的方法论实现。

**本机事实：** Cangjie 把全部产物写入其运行 cwd 下 `books/<slug>/`，并维护
断点文件 `PIPELINE_STATE.md`。因此总控必须：

- 为每次 Cangjie 运行固定 cwd（建议 `/Volumes/ORICO/KnowledgePipeline/distill/<job-id>/cangjie/`）；
- 在 Job Manifest 中登记 `cangjie.pipeline_state`（PIPELINE_STATE.md 路径）与 `cangjie.output_path`；
- 恢复 Job 时先读取 PIPELINE_STATE.md，再决定是否重入（总控 Manifest 与该文件互补，不复制其内部状态）。

最终记录：

```text
cangjie.status
cangjie.output_path
cangjie.pipeline_state
cangjie.skill_mode
cangjie.completed_at
```

### 8.3 用户确认

**（V1.1 修订）** cangjie-skill 内部没有 `stage_*_confirmation` 形式的命名门；
真实确认点是 3 处：

1. **阶段 0 骨架确认**：向用户展示 `BOOK_OVERVIEW.md`（"骨架我理解对了吗？"），确认后才进入阶段 1；
2. **阶段 1.5 候选确认**：筛选完成后用户轻确认（"这 N 个会做成 skill，有想捞回或砍掉的吗？"）；
3. **阶段 5 安装位置询问**：询问安装到用户级还是项目级（本机约定：装到实体库 `~/.agents/skills/`，视图自动同步）。

总控为以上 3 处分别定义 Job gate 名称（建议 `stage0_overview`、
`stage1_5_candidates`、`stage5_install_location`），映射表维护在
`references/target-gates.md`；原 Skill 的 Gate 条件本身不得改变。

当 Cangjie 到达上述任一确认点时，总控必须把问题原样升级给用户。

Job 状态：

```text
WAITING_USER
```

并记录：

```text
waiting_for = cangjie:<gate-name>
```

---

## 9. Personal Capability Distiller 分支

### 9.1 定位

该分支的目标不是"再生成一次普通 Skill"。

它负责将知识进一步转化为用户长期可复用的：

```text
能力卡
SOP
Prompt
判断规则
方法论
候选 Skill
能力关系
Obsidian 能力库资产
```

### 9.2 输入

优先使用同一个：

```text
Verified docchunk Corpus
```

不得重新从原 PDF / 视频开始处理。

（本机事实：该 Skill 只接受 Markdown / 纯文本输入；docchunk Corpus 的
`combined.md` 与 `atomic/*.md` 天然满足该要求。）

### 9.3 档位

总控不擅自修改原 Skill 的 A/B/C 规则。

用户明确指定：

```text
depth=A
depth=B
depth=C
```

则按用户指定。

未指定时：

```text
遵循 personal-capability-distiller 自身默认行为
```

（V1.1 注：实测该 Skill 未指定档位时**默认 C 档**并显式声明；总控以
`depth=null` 传递，由原 Skill 按默认行为执行。）

### 9.4 用户确认门

原 Skill 的确认门是命名状态（references/workflow-states.md），关键用户门包括：

- `inventory_reviewed`（资料清点后确认主领域与档位）；
- `human_material_approved`（材料定稿，须用户明示"定稿"）;
- `installation_approved`（安装确认，须用户明示授权，绝不自动安装）。

原 Skill 要求确认时，总控必须暂停。

不得使用：

```text
用户已确认
用户同意定稿
用户同意安装
```

等虚构信息绕过门禁。

### 9.5 Obsidian 能力库路径（V1.1 新增）

原 Skill 内部把 vault 根写死为 Windows 路径 `E:\Obsidian\Skill_Library`。
**本机实际 vault 为 `/Volumes/ORICO/Obsidian/Skill_Library`**（00_能力地图 …
07_应用反馈 + 90_模板与配置 结构完整）。总控调用该 Skill 时必须显式指定本机
vault 路径；vault 缺失时沿用原 Skill 暂存 `drafts/` 的行为。

---

## 10. Job 与状态机

### 10.1 一个请求对应一个 Job

示例：

```text
job_id:
20260905-233500-quark-guarantee-course
```

### 10.2 总状态

建议：

```text
CREATED
DISCOVERING
DOWNLOADING
DOWNLOADED
ROUTING
TRANSCRIBING
DOCCHUNKING
VERIFYING
CORPUS_READY
DISTILLING_CANGJIE
DISTILLING_PERSONAL
WAITING_USER
COMPLETED
PARTIAL
BLOCKED
FAILED
```

### 10.3 子状态

```yaml
source:
  status: pending|running|success|failed

media:
  status: skipped|pending|running|success|failed

docchunk:
  status: pending|running|verified|failed

cangjie:
  status: pending|running|waiting_user|success|failed|skipped

personal:
  status: pending|running|waiting_user|success|failed|skipped
```

### 10.4 断点续跑

Agent 每次开始处理前必须先检查 Job Manifest。

例如：

```text
media = success
docchunk = verified
cangjie = waiting_user
```

则恢复时：

```text
直接继续 cangjie
```

不得重新下载、重新转录或重新 chunk。

（Cangjie 分支恢复时还必须先读其 `PIPELINE_STATE.md`，见 §8.2。）

---

## 11. Job Manifest

建议每个 Job 使用一个权威 Manifest：

```yaml
schema_version: 1

job:
  id: 20260905-233500-quark-guarantee-course
  created_at: 2026-09-05T23:35:00+08:00
  status: CORPUS_READY

request:
  raw_prompt: "把夸克网盘审计课程/融资担保里的资料做成skill并蒸馏"
  targets:
    - cangjie
    - personal

source:
  provider: quark
  remote_path: /审计课程/融资担保
  source_id: "provider-specific-id"
  downloaded_path: /Volumes/ORICO/KnowledgePipeline/jobs/.../source
  source_hash: "sha256:..."
  size_bytes: 123456789

routing:
  collection: true
  documents: 4
  media: 8
  unsupported: 0

media:
  status: success
  outputs:
    - source: 01.mp4
      transcript: /Volumes/ORICO/MediaTranscriber/output/.../01.md
      metadata: /Volumes/ORICO/MediaTranscriber/output/.../metadata.yaml

docchunk:
  status: verified
  corpus_path: /Volumes/ORICO/LongDocCorpus/...
  manifest_path: /Volumes/ORICO/LongDocCorpus/.../manifest.json
  verify: PASS

cangjie:
  status: waiting_user
  waiting_for: stage1_5_candidates
  output_path: null

personal:
  status: pending
  depth: null

errors: []
```

Manifest 是总控的唯一权威 Job 状态。

已有项目内部：

- SQLite；
- PIPELINE_STATE；
- state.json；
- manifest.json；

保持原样。

总控只保存它们的引用，不复制它们的内部状态机。

---

## 12. 目录设计

建议新建：

```text
/Volumes/ORICO/KnowledgePipeline/
├── jobs/
│   └── <job-id>/
│       ├── job.yaml
│       ├── source/
│       ├── handoff/
│       ├── reports/
│       └── logs/
├── cache/
├── distill/          # V1.1 新增：cangjie/personal 运行 cwd（PIPELINE_STATE 等落这里）
└── tmp/
```

已有项目继续使用自己的目录：

```text
/Volumes/ORICO/MediaTranscriber/
/Volumes/ORICO/LongDocCorpus/
```

原则：

> 不把现有成熟项目的输出全部搬进 KnowledgePipeline。

KnowledgePipeline 保存：

```text
引用
状态
映射
审计信息
最终报告
```

而不是保存所有工具的重复副本。

### 大媒体文件

下载后的媒体原则上只保留一份正式本地副本。

不要出现：

```text
KnowledgePipeline/source/a.mp4
MediaTranscriber/inbox/a.mp4
MediaTranscriber/processing/a.mp4
另一个 archive/a.mp4
```

四份长期占盘。

V1 总控处理网盘下载媒体时，推荐调用：

```text
media-transcriber transcribe <absolute-path>
```

而不是再把文件复制进 Inbox。

现有 Inbox / Watcher 保留给人工拖入文件的独立场景。

---

## 13. 去重与幂等

### 13.1 下载前初步去重

优先记录：

```text
provider
remote file id
remote path
size
mtime
```

用于判断是否已经处理过。

### 13.2 下载后内容指纹

本地完成下载后生成：

```text
SHA-256
```

最终以内容 Hash 作为跨来源去重依据。

例如：

```text
百度网盘/A/book.pdf
夸克网盘/B/book.pdf
```

若 SHA-256 相同：

```text
复用同一个 Corpus
```

### 13.3 输出不可静默覆盖

已有：

```text
verified Corpus
transcript
Cangjie output
Capability output
```

默认都不覆盖。

需要重做时必须产生：

- 新 run；
- 新版本；
- 或使用原项目显式支持的 force/rebuild 机制。

---

## 14. 错误处理

### 14.1 下载失败

```text
FAILED_SOURCE_DOWNLOAD
```

记录：

- provider；
- remote file；
- error；
- retryable；
- retry_count。

### 14.2 转写失败

不得直接进入 docchunk。

状态：

```text
media.status = failed
overall = PARTIAL / BLOCKED
```

### 14.3 docchunk verify 失败

硬阻断：

```text
overall = BLOCKED
```

### 14.4 单个文件失败与批量课程

若一个课程 20 个文件：

```text
19 success
1 failed
```

默认：

```text
不进入最终蒸馏
```

因为"完整阅读"要求不应在缺一课的情况下静默继续。

除非用户明确说：

> 忽略第 X 个失败文件，按剩余资料继续。

必须在 provenance 中记录排除项。

---

## 15. Source Provenance

最终任何 Skill / 能力蒸馏结果，都应能回到：

```text
Cloud Provider
↓
remote path / source ID
↓
local source
↓
transcript（若有）
↓
docchunk document_id
↓
atomic id
↓
page / timestamp / source file
```

因此总控至少维护：

```yaml
source_provenance:
  provider:
  remote_path:
  remote_id:
  local_path:
  sha256:
  media_metadata:
  docchunk_corpus:
  document_ids:
```

不得在蒸馏后只剩下一份"总结"，找不到原始来源。

---

## 16. 安全边界

### 必须遵守

1. 不读取或输出百度/夸克认证 Token。
2. 不把 Token 写入 Job Manifest。
3. 默认只下载，不修改网盘源文件。
4. 不自动删除网盘文件。
5. 不自动上传蒸馏结果。
6. 原始文件只读。
7. 源媒体不因转写而修改。
8. 敏感资料优先本地处理。
9. 日志不得包含认证信息。
10. 用户确认节点不得由 Agent 自行伪造。

---

## 17. `knowledge-ingest` Skill 职责

实施结构以《knowledge-ingest Implementation Plan v1》为准（uv 管理的 Python CLI
项目 + SKILL.md + references/ + schemas/ + templates/），代码位于
`src/knowledge_ingest/`，通过 `[project.scripts]` 暴露 `knowledge-ingest` 命令。

### 本机 Skill 安装架构（V1.1 修订，遵 AGENTS.md §19）

- **唯一实体库是 `~/.agents/skills/`**；`~/.zcode/skills`、`~/.claude/skills`、
  `~/.codex/skills` 都是指向它的 symlink 视图，由 `~/.local/bin/skill-sync` 体检/修复。
- 安装 knowledge-ingest 时，只允许在实体库建 symlink：
  `ln -sfn /Volumes/ORICO/Projects/knowledge-ingest ~/.agents/skills/knowledge-ingest`，
  随后跑 `skill-sync`（必要时 `skill-sync fix`）同步视图。
- **禁止**直接向任何视图目录复制或链接实体内容（2026-09-02 rsync 事故教训），
  **禁止**复制两份源码。

### SKILL.md 只负责

```text
识别用户意图
↓
选择 Source Adapter
↓
创建 / 恢复 Job
↓
调用现有工具
↓
维护状态
↓
处理确认门
↓
生成最终报告
```

不要在 SKILL.md 中复制：

- docchunk 算法；
- media-transcriber 算法；
- Cangjie 方法论；
- Personal Distiller 的阶段体系。

---

## 18. 推荐自然语言接口

### 18.1 单文件

> 把百度网盘里的《融资担保公司监督管理条例培训.pdf》做成 Skill，并蒸馏到我的能力库。

### 18.2 整个目录

> 把夸克网盘 `/审计课程/融资担保/` 全部处理，作为一套课程做成 Skill，同时做个人能力蒸馏。

### 18.3 只做 Skill

> 把百度网盘里的《XX》做成 Cangjie Skill，不做个人能力蒸馏。

### 18.4 只做能力蒸馏

> 把这个本地课程目录蒸馏成我的能力资产，不生成 Cangjie Skill。

### 18.5 恢复任务

> 继续刚才那个融资担保课程任务。

Agent 应根据 Job Manifest 从断点恢复。

### 18.6 查询

> 刚才那个课程处理到哪一步了？

返回：

```text
Source       success
Transcribe   success 8/8
DocChunk     verified
Cangjie      waiting_user: stage1_5_candidates
Personal     pending
```

---

## 19. Agent 行为规则

### 应该

- 先 `doctor` / 环境检查；
- 先检查 Job 是否存在；
- 优先复用已有产物；
- 每一步成功后立即落盘 Job 状态；
- 长耗时阶段完成后写日志；
- 错误必须可定位；
- 用户确认后从断点续跑；
- 最终输出清晰的产物路径。

### 不应该

- 一上来重新 clone 所有项目；
- 修改现有项目核心代码来适配总控；
- 把所有能力塞进一个巨大的脚本；
- 重写 OCR / ASR / chunker；
- 同一文件重复转录；
- verify 失败仍继续蒸馏；
- 自动替用户通过 Cangjie / Distiller 的确认节点；
- 自动删除本地或网盘原件；
- 为 V1 引入 Web UI、数据库服务、Docker 或复杂队列。

---

## 20. V1 非目标

V1 暂不实现：

```text
Web GUI
React / Vue
数据库服务
远程 API
多用户
账号系统
多任务并行
自动上传回网盘
跨机器调度
Windows Worker
NAS
115 网盘
阿里云盘
Google Drive
YouTube 在线抓取
微信公众号抓取
网页爬虫
EPUB/PPTX/Excel 自动解析
向量数据库
RAG
自动长期监控网盘新增文件
```

这些未来都可以通过 Source Adapter 或 Worker 扩展。

---

## 21. 实施优先级

### Phase 1：打通 Happy Path

先只实现：

```text
Local PDF
↓
docchunk
↓
verify
↓
cangjie
```

确认总控 Job/恢复机制成立。

### Phase 2：接媒体

```text
Local MP4
↓
media-transcriber
↓
docchunk
↓
verify
↓
cangjie
```

### Phase 3：接个人能力蒸馏

增加：

```text
Corpus
↓
personal-capability-distiller
```

并正确处理人工确认门。

### Phase 4：接百度网盘

```text
baidu-drive
↓
download
↓
Local Pipeline
```

### Phase 5：接夸克网盘

```text
quarkclouddrive
↓
download
↓
Local Pipeline
```

### Phase 6：批量课程 / Document Set

最后处理：

```text
一个网盘目录
↓
混合文档 + 视频
↓
统一课程 Corpus
↓
双蒸馏
```

---

## 22. V1 验收标准

### Case A：百度 PDF

用户：

> 把百度网盘里的 A.pdf 做成 Skill。

必须做到：

```text
搜索
下载
docchunk
verify PASS
Cangjie
输出 Skill 路径
```

（前置：用户已完成 bdpan 登录授权。）

### Case B：夸克视频

用户：

> 把夸克网盘里的 B.mp4 做成 Skill。

必须做到：

```text
搜索
下载
media-transcriber
Markdown
docchunk
verify PASS
Cangjie
```

### Case C：混合课程目录

目录：

```text
01.mp4
02.mp4
03.pdf
04.docx
```

必须形成：

```text
一个保持来源边界的 Document Set Corpus
```

且每个文件可回查来源。

### Case D：断点恢复

在：

```text
Cangjie waiting_user
```

中断 Agent。

重新打开会话后说：

> 继续这个任务。

不得重新下载、重新转写、重新 docchunk。

### Case E：去重

同一 PDF 分别存在于百度与夸克。

第二次处理时应识别 SHA-256 相同，并复用 Corpus。

### Case F：失败阻断

课程中一个视频转写失败。

不得静默继续生成"完整课程 Skill"。

---

## 23. 最终完成后的用户体验

理想状态：

用户只负责表达目标：

> 把夸克网盘"审计培训/融资担保"学习掉，做成 Skill，并沉淀成我的个人能力。

Agent 自动完成所有机械步骤。

用户只在真正需要判断时参与：

```text
这套内容是否理解正确？
这些方法是否值得保留？
用 single 还是 pack？
能力卡是否定稿？
是否安装？
```

也就是说：

> **机器负责搬运、解析、验证、调度和恢复；用户只负责知识判断与最终确认。**

---

## 24. 给实施 Agent 的约束

开始编码前必须：

1. **已完成**：本机 Runtime Inventory（见 `docs/runtime-inventory.md`，2026-09-06）。
   后续实施以该清单为基线；组件升级后先更新清单。
2. 阅读以下仓库当前版本的 `README.md` / `SKILL.md`，不要依赖本文档中的旧命令
   （本机已核实的 CLI 面记录在 runtime-inventory §3/§4）：

```text
baidu-netdisk/bdpan-storage
quark-clouddrive/quarkclouddrive_offical
hg199074jin/media-transcriber
hg199074jin/docchunk
kangarooking/cangjie-skill
hg199074jin/personal-capability-distiller
```

3. 检查本机实际安装路径、Skill 目录和 ORICO 挂载状态。
4. 优先通过 Adapter / Wrapper 集成，不直接修改上述仓库。
5. 若某个现有项目缺少稳定的机器接口，先做最薄 Wrapper；不要把它的内部实现复制进总控。
6. V1 先完成单任务串行、断点恢复和可验证闭环，再考虑并发。
7. 所有 destructive 操作默认关闭。
8. 所有状态变更必须先写 Job Manifest，再进入下一阶段。
9. （V1.1 新增）遵本机全局规则：docchunk 长任务后台运行（§17 条）；Skill 一律装入
   实体库 `~/.agents/skills/`（§19 条）；实施代码前载入 claude-worker-router 路由。

---

# 一句话定义

`knowledge-ingest` 不是新的 OCR、ASR、Chunker 或 Skill Builder。

它是：

> **百度/夸克/本地资料 → 可验证 Corpus → Agent Skill + 个人能力资产**

之间的统一知识摄取、编排、状态管理和断点恢复层。
