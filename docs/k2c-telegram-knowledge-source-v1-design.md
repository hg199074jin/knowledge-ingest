# K2C Telegram Knowledge Source V1 设计文档

> **Continuous Knowledge Ingestion · Telegram Source Adapter for knowledge-ingest → K2C**

- 状态：**DRAFT FOR REVIEW**
- 日期：2026-09-18
- 所属仓库：`hg199074jin/knowledge-ingest`
- 下游 Target：`K2C / Knowledge-to-Capability`
- 设计性质：新增 Source 子系统，不修改 K2C V5 冻结职责边界
- 核心原则：Source-level Trust / Local-first / Provenance-first / Fail-closed / Exception-driven Automation / Human Gate at Publish & Activate

---

# 0. 结论先行

本设计将 Telegram 定义为 `knowledge-ingest` 的一个**持续知识源（Continuous Knowledge Source）**，而不是 K2C 内部模块，也不是微信转发器。

正式链路冻结为：

```text
Telegram 白名单群 / 频道
        ↓
Telegram Source Runtime
        ↓
Raw Event Store（SQLite）
        ↓
5 分钟文字合并 / 类型识别
        ↓
Noise Filter + Interest Policy
        ↓
Normalized Source Item
        ↓
knowledge-ingest Job OS
        ↓
docchunk / verify PASS
        ↓
target=k2c
        ↓
K2C compile
        ↓
staged
        ↓
Human Gate
        ↓
Publish / Activate
```

微信退出知识搬运链路，只承担：

```text
异常通知
REVIEW 通知
重要 Capability 通知
08:00 / 12:00 / 20:00 Digest
```

V1 不做历史回溯，只从部署完成后的启用时间开始采集；服务中断期间产生、但时间晚于启用边界的消息，允许在恢复后补齐，视为**增量恢复**，不视为历史 Backfill。

---

# 1. 背景与真实使用场景

用户加入的 Telegram 来源不是普通闲聊群，而更接近经过人工筛选的高质量知识 Feed。典型内容只有三类：

1. **单人连续发布知识文字**；
2. **单独发布 PDF，通常伴随标题或简短说明**；
3. **发布夸克、百度等网盘链接，通常伴随资源说明**。

群本身已经完成第一层人工筛选，因此 V1 不再要求“逐条人工确认后才进入 K2C”。

设计哲学从：

> 每条消息先证明自己有价值，才能进入知识系统。

改为：

> **白名单来源默认可信；只有明确广告、明确无兴趣主题或异常内容才退出自动链路。**

这相当于把 Human Gate 从“消息级”上移到“知识源级”：用户负责决定哪些群值得长期监听，系统负责自动采集、轻量过滤、去重、归档和进入 K2C。

---

# 2. 与现有 K2C / knowledge-ingest 的职责边界

## 2.1 K2C V5 保持冻结

K2C V5 已明确五层职责：

```text
Layer 1  Source / Ingestion      → knowledge-ingest
Layer 2  Source Processing       → media-transcriber / docchunk
Layer 3  Capability Compiler     → K2C
Layer 4  Runtime Resolution      → K2C Runtime Resolver
Layer 5  Agent / Human Workspace → Codex / Claude / Router / Obsidian
```

因此 Telegram 必须落在 Layer 1，而不能直接进入 `src/k2c/`。

K2C 继续只从 Verified Corpus 或其允许的短文本 Direct Input 之后接手，不负责：

- Telegram 登录；
- 群监听；
- 消息抓取；
- PDF 下载；
- 广告识别；
- 网盘链接收集；
- Telegram 断线恢复；
- Source Registry。

## 2.2 knowledge-ingest 继续作为 Job OS

Telegram Source Runtime 只负责把 Telegram 世界转换为 canonical Source Item。

一旦 Source Item 被正式提交给 `knowledge-ingest`，后续仍由现有能力负责：

- Job manifest；
- 文件路由；
- 缓存；
- 跨来源去重；
- docchunk；
- verify；
- Target Runtime；
- checkpoint / resume；
- report。

**不得在 Telegram Runtime 内重新实现第二套 knowledge-ingest。**

## 2.3 三个事实层，各管一段生命周期

V1 明确只允许三个权威状态源：

```text
Telegram source lifecycle  → telegram/state.db
knowledge-ingest lifecycle → jobs/<job-id>/job.yaml
Capability lifecycle       → K2C Capability Registry
```

边界如下：

- `telegram/state.db`：只负责 Telegram 原始事件、Source Item、cursor、过滤/审核状态；
- `job.yaml`：从正式 handoff 创建 Job 后接管处理状态；
- K2C Registry：从 Capability 编译阶段开始接管能力生命周期。

一个层不得反向修改另一个层的权威状态。

---

# 3. GitHub 开源调研与复用策略

V1 不直接套用完整开源项目，而是吸收成熟设计，避免重复造轮子和引入第二套产品状态机。

## 3.1 `tggo/tg-archive`

项目：<https://github.com/tggo/tg-archive>

最值得吸收：

1. **SQLite 是 source of truth，Markdown 只是 projection**；
2. 新消息、编辑、删除先写事实库，再生成下游视图；
3. attachment 可以晚于文字下载；
4. live 模式之外增加周期性增量补齐，避免 MTProto update 丢失；
5. `doctor` 用于检测消息缺口和归档不一致；
6. `FLOOD_WAIT`、rate limit 是正常运行状态，不应当作不可恢复错误。

V1 采用其中的“本地事实库 + 增量补齐 + doctor”思想。

不直接部署 `tg-archive` 作为生产依赖，原因是它自身已经是一套 Telegram Archive 产品；若再接 `knowledge-ingest`，会出现两套归档/恢复状态机和重复 Markdown 投影。

## 3.2 Telegram media downloader 类项目

参考：

- <https://github.com/botnick/telegram-media-downloader>
- <https://github.com/vinodkr494/telegram-media-downloader>

吸收：

- SHA-256 内容去重；
- 持久下载状态；
- 自动 resume / retry；
- 下载并发限制；
- 大文件阈值；
- 完整性扫描；
- 磁盘文件与数据库状态交叉校验。

不吸收：

- GUI；
- 媒体库；
- 分享链接；
- 图库；
- 视频播放器；
- 与 K2C 无关的媒体智能功能。

## 3.3 Telethon

GitHub 原仓库：<https://github.com/LonamiWebs/Telethon>

其 GitHub 仓库已于 2026-02-21 归档，但项目明确迁移到 Codeberg：<https://codeberg.org/Lonami/Telethon>。

V1 可采用 Telethon 作为第一版 MTProto user client，但**业务代码不得直接依赖 Telethon 对象模型**，必须经 `TelegramClientPort` 隔离。

这样未来若更换 MTProto client，只替换 Adapter，不影响 Source Registry、Event Store、Interest Policy、PDF Router 和 knowledge-ingest handoff。

---

# 4. 总体架构

```text
┌───────────────────────────────────────┐
│ Telegram                              │
│ 白名单群 / 频道                       │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ TelegramClientPort                    │
│ V1 Adapter: Telethon                  │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ Source Registry                       │
│ enabled / start_at / policy           │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ Raw Event Store: SQLite               │
│ message / edit / delete / cursor      │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ Source Item Builder                   │
│ 5-min text merge / PDF / cloud link   │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ Intake Policy                         │
│ Noise Filter + Interest Policy        │
│ KEEP / SKIP / REVIEW                  │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ Materializer                          │
│ Markdown / PDF / Pending Link         │
└──────────────────┬────────────────────┘
                   ↓
┌───────────────────────────────────────┐
│ knowledge-ingest                      │
│ Job OS / dedup / docchunk / verify    │
└──────────────────┬────────────────────┘
                   ↓
              target=k2c
                   ↓
┌───────────────────────────────────────┐
│ K2C                                   │
│ compile → staged                      │
│ Human Gate: Publish / Activate        │
└───────────────────────────────────────┘
```

旁路通知：

```text
Telegram Runtime / knowledge-ingest / K2C
                 ↓
             Notifier
                 ↓
             WxPusher
                 ↓
               微信
```

---

# 5. 部署位置与存储边界

## 5.1 代码归属

V1 逻辑归属 `knowledge-ingest`，建议新增：

```text
src/knowledge_ingest/telegram/
├── client_port.py
├── telethon_adapter.py
├── registry.py
├── event_store.py
├── watcher.py
├── merge.py
├── classify.py
├── materialize.py
├── notifier.py
└── doctor.py
```

不是简单增加一个巨型 `adapters/telegram.py`。

原因：Telegram 是常驻事件源，不是一次性的文件转换 Adapter。

## 5.2 运行数据根

遵守现有 ORICO fail-closed 原则，建议：

```text
/Volumes/ORICO/KnowledgePipeline/telegram/
├── state.db
├── raw/
├── attachments/
├── materialized/
├── review/
└── logs/
```

ORICO 不可用时：

- watcher 不创建内置盘 fallback；
- 不推进 Source Item；
- 不下载 PDF；
- 发出异常通知；
- ORICO 恢复后，从已持久化 cursor / Telegram 增量范围恢复。

## 5.3 Session 与密钥

Telegram session、`api_id`、`api_hash` 不得写入 Git、SQLite provenance 或 Job report。

建议放置：

```text
~/.config/knowledge-ingest/telegram/
├── session.session
└── credentials.env
```

权限：

```text
chmod 700 directory
chmod 600 session / credentials
```

`.gitignore` 必须覆盖：

```text
*.session
*.session-journal
credentials.env
telegram-secrets*
```

Session 按“账号完全访问凭据”处理。

---

# 6. Source Registry

## 6.1 目标

用户未来可以随时增加/停用 Telegram 白名单来源，而无需改代码。

推荐 CLI：

```bash
knowledge-ingest telegram sources list
knowledge-ingest telegram sources discover
knowledge-ingest telegram sources add <chat-id-or-username>
knowledge-ingest telegram sources disable <source-id>
knowledge-ingest telegram sources enable <source-id>
knowledge-ingest telegram sources show <source-id>
```

不建议把 `k2c telegram add` 作为正式命令，因为 Source 层属于 `knowledge-ingest`，不是 K2C。

## 6.2 Source Registry 数据

示例：

```yaml
sources:
  - source_id: tg_ai_explore
    platform: telegram
    chat_id: -1001234567890
    username: null
    display_name: AI探索指南
    enabled: true
    start_at: 2026-09-18T10:00:00+08:00

    policy:
      text: ingest
      pdf: classify_then_ingest
      cloud_link: pending
      noise_default: keep

    merge:
      same_author_text_window_seconds: 300
```

## 6.3 `start_at` 是硬边界

V1 不做历史 Backfill。

首次启用来源时写入 `start_at`，任何 `message_date < start_at` 的历史消息不得进入 Source Item。

但服务宕机后恢复时：

```text
start_at <= message_date <= now
且 message_id > last_processed_id
```

的消息必须补齐。

这属于增量恢复，不属于历史回溯。

---

# 7. TelegramClientPort

## 7.1 目的

隔离第三方 MTProto client，避免业务逻辑与 Telethon 强耦合。

接口语义：

```python
class TelegramClientPort(Protocol):
    async def list_dialogs(...): ...
    async def watch_updates(...): ...
    async def fetch_messages_after(...): ...
    async def get_message(...): ...
    async def download_document(...): ...
    async def resolve_chat(...): ...
```

设计要求：

- 上层永远消费内部 `TelegramEvent` / `TelegramDocumentRef`；
- 不把 Telethon `Message`、`Document` 等对象穿透到业务层；
- Telethon-specific FloodWait、RPC Error 在 Adapter 内映射为内部错误类别；
- Adapter 可以独立替换。

## 7.2 V1 只读

V1 Telegram session 只用于：

- 读取消息；
- 获取编辑/删除更新；
- 下载符合规则的 PDF；
- 获取来源元数据。

V1 不提供：

- 自动发送消息；
- 自动回复；
- 自动转发；
- 自动加群；
- 自动私聊；
- 批量操作联系人。

降低账号风险，也缩小权限面。

---

# 8. SQLite Raw Event Store

## 8.1 为什么需要 SQLite

Telegram 是事件流：

- 消息可能编辑；
- 消息可能删除；
- 服务可能掉线；
- 连续文字在 5 分钟结束前还不能确定最终 Source Item；
- PDF 下载可能晚于消息本身；
- REVIEW 需要可恢复状态。

因此“收到一条立刻写 Markdown”不够可靠。

冻结原则：

> **SQLite 是 Telegram Source Runtime 的事实层；Markdown/PDF handoff 是派生产物。**

## 8.2 最小表设计

### `tg_sources`

```text
source_id PK
chat_id UNIQUE
display_name
enabled
start_at
last_seen_message_id
last_reconciled_at
created_at
updated_at
```

### `tg_messages`

```text
source_id
message_id
sender_id
message_date
edited_at
deleted_at
text
reply_to_message_id
has_document
document_name
document_mime
document_size_bytes
cloud_links_json
raw_fingerprint
status
PRIMARY KEY (source_id, message_id)
```

### `source_items`

```text
item_id PK
source_id
kind                 # text | pdf | cloud_link
first_message_id
last_message_id
message_ids_json
created_at
finalized_at
noise_decision       # KEEP | SKIP | REVIEW
interest_decision    # INCLUDE | EXCLUDE | REVIEW | N/A
processing_status
materialized_path
knowledge_ingest_job_id
```

### `downloads`

```text
item_id
document_message_id
telegram_document_id
expected_size_bytes
local_path
sha256
status
attempts
last_error
```

### `reviews`

```text
review_id PK
item_id
reason
created_at
resolved_at
decision
```

## 8.3 不把 Job 状态塞进 SQLite

SQLite 只记录：

```text
是否已成功 handoff
对应 knowledge_ingest_job_id
```

进入 `knowledge-ingest` 后，Job 的真实状态只以 `job.yaml` 为准。

---

# 9. Source Item Builder

V1 只支持三类正式 Source Item。

## 9.1 TEXT

适用于高质量纯文字内容。

### 连续消息合并规则

冻结为：

```text
同一 source
+ 同一 sender
+ 纯文字
+ 第一条消息开启固定 5 分钟窗口
+ 5 分钟窗口内后续纯文字
→ 合并为一个 Source Item
```

窗口以**第一条消息时间**计算，不采用无限滚动延长，避免作者每隔几分钟持续发帖导致一个 Item 无限增长。

如果期间出现 PDF 或网盘链接，该消息作为独立 Source Item，不并入纯文字 Item。

输出：

```markdown
# Telegram Knowledge Item

<原始文字 1>

<原始文字 2>

<原始文字 3>
```

不改写作者正文，不做总结替换。

## 9.2 PDF

同一 Telegram 消息的：

```text
caption + PDF metadata
```

组成 PDF Source Item。

在真正下载前，先执行 Interest Policy。

## 9.3 CLOUD_LINK

检测到夸克、百度等网盘链接时：

- 保存原帖正文；
- 保存 URL；
- 保存来源 provenance；
- 状态进入 `PENDING_RESOURCE`；
- V1 不登录网盘；
- V1 不解析目录；
- V1 不自动下载。

若正文明确描述“大型资源合集/课程包/数百份文件/几十 GB”等，可标记：

```text
resource_collection_candidate = true
```

但 V1 只创建 Resource Collection Stub，不解析真实目录。

---

# 10. Noise Filter

## 10.1 基本原则

白名单来源先验质量高，V1 采用：

```text
默认 KEEP
明确广告 → SKIP
不确定 → REVIEW
```

不是“模型认为有价值才 KEEP”。

## 10.2 明确 SKIP 示例

- 单纯优惠券/返利；
- 纯拉群广告；
- 联系方式 + 售卖文案，几乎无知识正文；
- 重复商业推广模板；
- 与来源主题无关的明显垃圾消息。

## 10.3 不应轻易 SKIP

以下即便包含商业推广，也优先 KEEP 或 REVIEW：

- 广告前后包含完整方法论；
- 产品推广中包含可复用实操经验；
- 课程宣传同时附带高质量长文；
- 作者在推广中给出具体案例、框架、数据和方法。

即：**宁可多收，不漏掉知识。**

---

# 11. Interest Policy

## 11.1 目的

Interest Policy 主要用于决定：

> PDF 是否值得自动下载并进入 K2C。

纯文字白名单知识默认 KEEP，不要求每条都通过兴趣分类才能保存。

## 11.2 INCLUDE 第一版

默认感兴趣：

```text
AI / 大模型 / Agent / AI 工具 / 编程工具
审计 / CPA / 会计 / 财税 / 合规 / 企业管理
商业 / 创业 / 产品 / 运营
副业 / 赚钱 / 商业模式 / 个人变现
认知提升 / 思维模型 / 决策 / 个人成长
效率 / 工作流 / 知识管理
```

## 11.3 EXCLUDE 第一版

默认不感兴趣：

```text
股票个股 / 短线交易 / 荐股 / 炒股策略
恋爱 / 情感关系 / 婚恋技巧
娱乐八卦
纯热点新闻搬运且无方法论价值
```

注意：

- “商业模式分析上市公司”不能因为包含股票词就自动 EXCLUDE；
- “投资思维/企业分析方法”可能属于认知或商业，应允许 INCLUDE；
- 因此必须是语义分类，不使用简单关键词黑名单作为最终判定。

## 11.4 PDF 下载前判定信息

因为 V1 尚未下载 PDF，分类只能基于：

```text
source identity
message caption
PDF filename
PDF MIME
PDF size
同一消息可见文本
```

输出：

```text
INCLUDE
EXCLUDE
REVIEW
```

若 filename/caption 信息不足，不猜测内容：

```text
→ REVIEW
```

V1 不为了分类而预下载整份 PDF。

---

# 12. PDF 自动处理规则

冻结规则：

```text
Interest = INCLUDE
AND size <= 50 MiB
→ 自动下载
→ SHA-256
→ handoff to knowledge-ingest
→ docchunk
→ verify
→ target=k2c
```

```text
Interest = INCLUDE
AND size > 50 MiB
→ REVIEW
→ 不自动下载
```

```text
Interest = EXCLUDE
→ 不下载
→ SKIP_INTEREST
```

```text
Interest = REVIEW
→ 不下载
→ REVIEW
```

50 MiB 是**自动处理阈值**，不是永久拒绝阈值。

## 12.1 去重

两层去重：

### Telegram Source 级轻量去重

可记录：

```text
Telegram document id
filename
size
来源 message id
```

只用于减少明显重复工作，不作为最终内容身份。

### 内容级权威去重

文件下载完成后计算：

```text
SHA-256
```

并继续复用 `knowledge-ingest` 已有 fingerprint/cache 机制。

同一 PDF 在不同 Telegram 群重复出现，应最终复用同一内容处理结果，同时保留多个 provenance。

---

# 13. Materialization 与 knowledge-ingest Handoff

## 13.1 TEXT materialization

生成：

```text
materialized/<item-id>/
└── message.md
```

`message.md` 只保存知识正文；Telegram 来源元数据进入 source handoff / provenance，不混入正文。

## 13.2 PDF materialization

生成：

```text
materialized/<item-id>/
└── document.pdf
```

caption 保存在 provenance 中；若 caption 本身具有明显知识价值，可作为独立 `context.md` 加入 Document Set，但不能把固定频道签名、广告尾巴混入 Corpus。

## 13.3 CLOUD_LINK materialization

不创建 knowledge-ingest Job。

只生成 Pending Record：

```text
item_id
source_id
message_id
caption
url
provider_guess
resource_collection_candidate
status=PENDING_RESOURCE
```

未来实现 Cloud Resource Resolver 时再从此处续接。

## 13.4 Source Handoff 升级

当前 Source Handoff 仅允许：

```text
local | baidu | quark
```

V1 需要新增：

```text
telegram
```

建议 Source Handoff schema 从 v1 升为 v2，并保持向后兼容。

示例：

```json
{
  "schema_version": 2,
  "provider": "telegram",
  "remote": {
    "id": "tg_ai_explore:12345-12347",
    "path": null,
    "name": "AI探索指南",
    "size_bytes": null,
    "mtime": "2026-09-18T12:30:00+08:00"
  },
  "local_path": "/Volumes/ORICO/KnowledgePipeline/telegram/materialized/tgitem_xxx/message.md",
  "download_completed": true,
  "source_notes": [],
  "provenance": {
    "platform": "telegram",
    "source_id": "tg_ai_explore",
    "chat_id": -1001234567890,
    "message_ids": [12345, 12346, 12347],
    "sender_id": "...",
    "message_url": null,
    "first_message_at": "2026-09-18T12:25:00+08:00",
    "last_message_at": "2026-09-18T12:29:00+08:00"
  }
}
```

凭据不得进入 handoff。

---

# 14. K2C 自动化边界

Telegram Source 的目标不是自动 Activate。

冻结为：

```text
Telegram
→ ingest
→ docchunk
→ verify PASS
→ compile
→ staged
```

全部允许自动。

以下继续遵守 K2C 原 Human Gate：

```text
Publish
Activate
```

即：

> `compile ≠ publish ≠ activate`

Telegram 入口不得绕过 K2C 的 release / activation 生命周期。

---

# 15. 当前仓库前置依赖：`target=k2c` 尚未落入 knowledge-ingest main

设计核对时发现：

当前 `knowledge-ingest/src/knowledge_ingest/targets.py` 的内置 Target Registry 实际只有：

```text
cangjie
personal
family_router
```

而 K2C V5 已经冻结：

```text
knowledge-ingest → target=k2c → K2C
```

因此完整 Telegram E2E 的前置条件是：

> **把已设计的 `k2c` Target 正式接入 knowledge-ingest Generic Target Runtime。**

这不是 Telegram Source Runtime 自己偷偷实现的职责。

V1 可以先完成：

```text
Telegram → Source Item → knowledge-ingest → Verified Corpus
```

但“自动到 staged”的最终验收必须在 `target=k2c` 正式存在之后进行。

---

# 16. REVIEW 机制

## 16.1 REVIEW 触发条件

至少包括：

- 广告与知识内容无法可靠区分；
- PDF 主题不确定；
- PDF > 50 MiB；
- 来源状态异常；
- PDF 下载多次失败；
- classifier 输出不满足 schema；
- handoff 创建失败；
- Source Item 存在不可解释冲突。

## 16.2 REVIEW 不阻塞整个来源

一个 Item 进入 REVIEW 时：

```text
只暂停该 Item
其他消息继续采集
其他 Item 继续处理
```

不得因为一个大 PDF 或一条广告判断不清，让整个 Telegram watcher 停住。

## 16.3 REVIEW 结果

```text
KEEP
SKIP
DOWNLOAD_ONCE   # 对 >50 MiB PDF 的单次人工授权
```

所有人工决定记录：

```text
time
item_id
reason
previous_state
decision
```

---

# 17. 微信通知与 Digest

## 17.1 不再实时转发 Telegram 原文

V1 明确关闭：

```text
Telegram 每条消息 → 微信
```

微信不再复制出第二条信息流。

## 17.2 即时通知

只在以下情况即时推送：

```text
ERROR
REVIEW_REQUIRED
IMPORTANT_CAPABILITY
```

示例：

```text
【K2C REVIEW】
来源：vip 大佬文集
文件：xxx.pdf
大小：72.4 MiB
主题判断：认知提升
原因：超过 50 MiB 自动下载阈值
状态：等待人工决定
```

## 17.3 Digest

固定：

```text
08:00
12:00
20:00
```

Digest 统计自上一次 Digest 之后的增量，不重复刷全部历史。

建议内容：

```text
新采集消息数
形成 Source Item 数
TEXT 数
PDF 数
网盘 Pending 数
广告 SKIP 数
兴趣 EXCLUDE 数
REVIEW 数
重复复用数
进入 knowledge-ingest 数
verify PASS / FAIL
K2C staged 数
重要 Capability 摘要
```

---

# 18. Retention 与 Provenance

## 18.1 KEEP 项

永久保留最小 provenance：

```text
source/chat identity
message_id / message_ids
发布时间
原始正文
原始链接
附件 filename / size
附件 SHA-256（下载后）
knowledge-ingest job_id
K2C downstream reference（可得时）
```

原始 PDF 保留。

## 18.2 SKIP 项

明确广告、兴趣排除等原始记录默认保留约 30 天，用于：

- 检查误判；
- 调整规则；
- classifier 回归测试。

30 天后允许清理正文和不必要原始载荷，但最小统计审计记录可以长期保留：

```text
source_id
message_id
decision
reason
classified_at
```

## 18.3 REVIEW 项

未解决前不得自动清理。

解决为 KEEP → 按 KEEP 保留。

解决为 SKIP → 从解决时间起进入约 30 天清理周期。

---

# 19. 恢复、可靠性与 `doctor`

## 19.1 两层增量可靠性

### 实时更新

正常情况下由 MTProto updates 推入 Event Store。

### 周期 reconcile

不能假设实时 update 永不丢失。

定期执行：

```text
fetch_messages_after(last_seen_message_id)
```

只允许补：

```text
message_date >= source.start_at
```

不跨越历史启用边界。

## 19.2 状态写入顺序

冻结为：

```text
Telegram event
→ SQLite transaction commit
→ 后续 merge/classify/download
```

不得先下载或创建 Job，后补 Event Store。

## 19.3 幂等

重复收到同一 update：

```text
(source_id, message_id)
```

必须 upsert，不能形成两个 Source Item。

Handoff 也必须通过 `item_id` 保持幂等：同一 Item 不得创建多个有效 knowledge-ingest Job。

## 19.4 Telegram Doctor

推荐 CLI：

```bash
knowledge-ingest telegram doctor
```

至少检查：

- Session 是否存在且权限正确；
- API 登录是否有效；
- ORICO 是否在线；
- state.db integrity；
- Registry 中 enabled source 能否 resolve；
- cursor 是否倒退；
- 已 materialize 文件是否缺失；
- `downloads.status=complete` 是否真的存在文件；
- SHA-256 是否匹配；
- source_item 指向的 job_id 是否存在；
- REVIEW 是否长期未处理；
- 是否存在启用时间之后明显未 reconcile 的来源。

`doctor --fix` V1 只允许执行无争议修复；涉及重新下载、大量补消息、覆盖文件等动作必须显式人工确认。

---

# 20. 编辑与删除语义

## 20.1 消息编辑

若 Source Item 尚未 finalization：

```text
更新 Raw Event
→ 重新生成 Item
```

若已经 handoff 到 knowledge-ingest：

V1 不自动反向重写已进入 Corpus 的 Job。

记录：

```text
SOURCE_EDITED_AFTER_HANDOFF
```

并进入 REVIEW / 后续增量更新候选。

原因：跨层自动修改已有 Corpus 会破坏可审计性。

## 20.2 消息删除

不物理抹掉已采集知识。

记录：

```text
deleted_at
source_deleted=true
```

下游 provenance 必须能够知道 Telegram 原消息后来被删除。

---

# 21. 分类模型的契约

V1 不把分类逻辑写成不可测试的自由文本 prompt。

必须要求结构化输出。

Noise：

```json
{
  "decision": "KEEP | SKIP | REVIEW",
  "reason_code": "...",
  "confidence": 0.0
}
```

Interest：

```json
{
  "decision": "INCLUDE | EXCLUDE | REVIEW",
  "primary_topic": "...",
  "reason": "...",
  "confidence": 0.0
}
```

关键规则：

- 解析失败 → REVIEW；
- 模型调用失败 → REVIEW，不 SKIP；
- 模型输出未知 enum → REVIEW；
- 不得因为模型不可用而丢消息；
- classifier 版本与 policy 版本写入 Item 审计记录。

---

# 22. Resource Collection V1 边界

Resource Collection 是正式概念，但 V1 只实现 Stub。

结构：

```text
ResourceCollectionStub
├── source_item_id
├── caption
├── cloud_url
├── provider_guess
├── estimated_size_text
├── estimated_count_text
└── status=PENDING_RESOURCE
```

例如：

```text
“2755 份 / 176G 英语动画”
```

不会自动：

- 登录夸克；
- 下载 176G；
- 遍历 2755 个文件；
- 创建数千个 Job。

未来单独设计 Cloud Resource Resolver 后，再 enrich：

```text
Stub
→ directory manifest
→ selection policy
→ child Source Items
→ knowledge-ingest
```

符合“长期做持续学习系统，但一个来源一个来源来”的原则。

---

# 23. CLI 与运维面

推荐 V1 命令：

```bash
knowledge-ingest telegram auth
knowledge-ingest telegram sources discover
knowledge-ingest telegram sources add ...
knowledge-ingest telegram sources list
knowledge-ingest telegram sources enable ...
knowledge-ingest telegram sources disable ...
knowledge-ingest telegram watch
knowledge-ingest telegram status
knowledge-ingest telegram review list
knowledge-ingest telegram review resolve ...
knowledge-ingest telegram digest --since ...
knowledge-ingest telegram doctor [--fix]
```

常驻方式优先复用 macOS LaunchAgent，与现有 `knowledge-ingest watchdog` 运维风格一致。

V1 不强制 Docker。原因：

- 当前 `knowledge-ingest` 本身是本机 Python/uv 项目；
- Telegram session 与本机 ORICO 数据根直接绑定；
- LaunchAgent 与现有运行方式一致；
- 为单一 watcher 引入 Docker 会增加 session mount、volume、网络和运维复杂度。

若未来 Source Runtime 独立服务化，再重新评估容器化。

---

# 24. 数据流详解

## 24.1 纯文字

```text
NewMessage
→ Raw Event Store
→ 检查 source.start_at
→ 打开/加入 5min fixed window
→ finalize TEXT Source Item
→ Noise Filter
→ KEEP
→ materialize message.md
→ knowledge-ingest create/register
→ docchunk
→ verify PASS
→ target=k2c
→ staged
```

## 24.2 PDF

```text
NewMessage(document/pdf)
→ Raw Event Store
→ caption + filename + size
→ Noise Filter
→ Interest Policy
   ├── EXCLUDE → skip
   ├── REVIEW  → review
   └── INCLUDE
         ↓
      size check
   ├── >50 MiB → review
   └── <=50 MiB
         ↓
      download
         ↓
      SHA-256
         ↓
      knowledge-ingest
         ↓
      docchunk / verify
         ↓
      target=k2c
```

## 24.3 网盘

```text
NewMessage(cloud url)
→ Raw Event Store
→ Noise Filter
→ KEEP
→ create Pending Resource / Collection Stub
→ Digest / optional REVIEW notification
→ STOP in V1
```

---

# 25. 不做什么（V1 Non-goals）

明确不做：

- 历史 Telegram 全量回溯；
- 多人群聊语义线程重建；
- Conversation Thread Builder；
- 跨群大规模语义聚类；
- 网盘自动登录和目录抓取；
- 大型 Resource Collection 自动下载；
- 自动抓 YouTube / GitHub / 公众号 / Email；
- 微信实时原文转发；
- Telegram 自动发言；
- Bot 运营；
- 自动 Publish / Activate Capability；
- 在 Telegram Runtime 内重新实现 docchunk / K2C / knowledge-ingest Job OS。

---

# 26. 验收标准

V1 必须通过以下验收。

## A. Source Registry

- 可 discover 当前账号可见 Telegram 来源；
- 可 add / enable / disable；
- 新增来源无需改代码；
- `start_at` 生效，启用前消息不进入系统。

## B. 文字

- 同一作者固定 5 分钟窗口内连续纯文字正确合并；
- 超出窗口生成新 Item；
- 原文不被模型改写；
- 重启不重复生成 Item。

## C. PDF

至少用 4 类真实样本：

1. 副业/赚钱 PDF，<50 MiB → 自动下载并进入 knowledge-ingest；
2. 股票 PDF，<50 MiB → EXCLUDE，不下载；
3. 感兴趣 PDF，>50 MiB → REVIEW，不自动下载；
4. 标题/说明无法判断 → REVIEW，不猜测。

## D. 网盘

- 正确保存 caption + URL；
- 不自动登录；
- 不自动下载；
- 大型合集可形成 Resource Collection Stub。

## E. 广告

- 明确广告进入 SKIP；
- 广告与知识混合时不得激进丢弃；
- 不确定进入 REVIEW；
- classifier 故障不得导致消息丢失。

## F. 去重

- 同一 update 重放不产生重复 Item；
- 同一 PDF 重复下载可通过 SHA-256 识别；
- knowledge-ingest 继续承担最终跨来源内容复用。

## G. 恢复

- watcher 重启后能补齐启用边界之后的中断期间消息；
- 不越过 `start_at` 做历史回溯；
- ORICO 不在线时 fail closed；
- ORICO 恢复后可继续。

## H. 通知

- ERROR 即时通知；
- REVIEW 即时通知；
- 不实时推送普通 Telegram 原文；
- Digest 准时支持 08:00 / 12:00 / 20:00 三个窗口。

## I. K2C 边界

- Telegram Source Runtime 不写 Capability Registry；
- 自动化最多推进到 staged；
- Publish / Activate 仍要求 K2C Human Gate；
- `target=k2c` 未接入时必须诚实 BLOCKED / PRECONDITION_MISSING，不得伪装完整链路成功。

---

# 27. 测试策略

坚持现有仓库 TDD 文化。

建议测试层次：

```text
Unit
├── Registry
├── 5-min merge
├── URL detection
├── Noise policy
├── Interest schema
├── 50 MiB boundary
├── SQLite idempotency
└── retention calculation

Contract
├── TelegramClientPort fake adapter
├── Source Handoff v2
└── knowledge-ingest provider=telegram

Integration
├── fake Telegram events → Source Item
├── TEXT → local materialization → KI
├── PDF INCLUDE → download fake → KI
├── PDF EXCLUDE → no download
└── CLOUD_LINK → Pending only

Real Acceptance
├── 真实白名单来源
├── 真实个人 Telegram session
├── 真实 <50 MiB PDF
├── 真实广告样本
├── 真实网盘链接
└── 真实中断恢复
```

真实 Telegram 验收不得使用自动发消息进行造数据；优先选择已有测试来源或用户明确允许的测试群。

---

# 28. 未来扩展原则

Telegram 是 Continuous Knowledge Ingestion 的第一个持续来源，但 V1 不提前实现其他来源。

未来统一模型可以演进为：

```text
Telegram ─┐
GitHub ────┤
Web ───────┤
YouTube ───┤→ Source Adapter → knowledge-ingest → K2C
公众号 ────┤
Email ─────┘
```

未来每个 Source Adapter 都应复用相同高层契约：

```text
Source Registry
Raw Event / Object
Normalized Source Item
Provenance
Intake Policy
Handoff
```

但每个来源单独设计、单独实施、单独验收。

不在 Telegram V1 中预建一个“万能 Source Framework”。

---

# 29. 冻结决策清单

本设计确认后，下列内容作为 Telegram V1 冻结输入：

```text
1. 归属：knowledge-ingest Source 层，不进入 K2C 内核
2. Source Registry：支持未来随时增加白名单来源
3. 历史 Backfill：关闭
4. 部署启用之后持续监听：开启
5. 宕机后的启用边界内增量补齐：开启
6. 纯文字：同作者固定 5 分钟窗口合并
7. PDF：先 Interest Policy，后下载
8. PDF 自动下载阈值：<=50 MiB
9. >50 MiB：REVIEW，不是永久拒绝
10. INCLUDE：AI/审计/商业/副业/赚钱/认知/效率等
11. EXCLUDE：荐股/短线股票、恋爱、娱乐八卦等
12. 网盘：只保存原帖 + URL，PENDING
13. 大型资源：Resource Collection Stub
14. Noise：默认 KEEP / 明确广告 SKIP / 不确定 REVIEW
15. 自动化：Telegram → ingest → compile → staged
16. Human Gate：Publish / Activate
17. 微信原文实时转发：关闭
18. 微信即时通知：ERROR / REVIEW / IMPORTANT_CAPABILITY
19. Digest：08:00 / 12:00 / 20:00
20. Telegram 登录：个人账号 MTProto user session
21. Client：V1 Telethon，经 TelegramClientPort 隔离
22. Telegram Source Facts：SQLite
23. knowledge-ingest Job Facts：job.yaml
24. K2C Capability Facts：Capability Registry
25. KEEP provenance：长期保留
26. 原始 PDF：保留
27. SKIP 原始记录：约 30 天后可清理
28. 长期目标：Continuous Knowledge Ingestion
29. 当前只实施 Telegram，不提前做 GitHub/Web/YouTube/公众号/Email
```

---

# 30. 实施前必须解决的唯一系统级前置问题

在开始完整 E2E 实施前，需要核实并完成：

> **knowledge-ingest 的 Generic Target Runtime 中正式注册 `k2c` Target。**

当前 main 的事实与 K2C V5 设计存在这一处接口落差。

建议把它作为 Telegram 实施计划的显式前置任务，而不是在 Telegram 模块中临时旁路。

除此之外，本设计不要求修改 K2C V5 冻结架构。
