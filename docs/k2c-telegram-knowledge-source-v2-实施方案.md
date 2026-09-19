# K2C Telegram Knowledge Source V2 实施方案

> **Implementation Plan for the FROZEN Telegram Source V2 Design**

- 状态：**FINAL**
- 日期：2026-09-18
- 所属仓库：`hg199074jin/knowledge-ingest`
- 唯一设计输入：`docs/k2c-telegram-knowledge-source-v2-design.md`
- 冻结设计 SHA-256：`0d6e45de76785db2cf318a93261a6aabb31e2dc03993ce8693b134f7093e926c`
- 审阅输入：`docs/k2c-telegram-knowledge-source-v2-本机审阅意见.md`
- 实施方案终审输入：`docs/k2c-telegram-knowledge-source-v2-实施方案-本机审阅意见.md`
- 实施目标：在不修改 K2C V5 FROZEN 边界、不重做 knowledge-ingest Job OS 的前提下，实现 `Telegram → knowledge-ingest → target=k2c → staged` 的持续知识采集链路。
- 本次修订：吸收本机实施方案初审 M1–M4、F1、E1/E2，以及终审 E3–E6；不修改 FROZEN Design。
- V2.1 修订（2026-09-19）：依据 TG0–TG6 全部落地与通道灰度实测，修订后续（P0 / TG6 收尾 / TG7 / TG8）的顺序与范围，见文末《修订记录 V2.1》；FROZEN Design 仍不变，执行待用户逐项放行。

---

# 本次实施方案修订记录

1. **M1**：补齐冻结设计 §20 的编辑/删除语义，并分别落到 TG3（事件采集测试）、TG4（状态语义实现）、TG8（真实验收）。
2. **M2**：明确 `telegram status`、`review list/resolve`、`DOWNLOAD_ONCE` 的责任 TG 与幂等语义。
3. **M3**：补齐 Telethon optional dependency 的 CI/无 extra 契约，保证核心 KI 在未安装 Telethon 时仍可 import、测试。
4. **M4**：TG7 新增通知持久化时明确 `user_version 1 → 2` 幂等迁移与 maintenance lock。
5. **F1**：删除 TG0 的 `git mv V1` 建议；V1 历史文件原地保留，V2 canonical 为唯一实施输入。
6. **E1**：Interest Policy 不再在实施方案复制第二份枚举，唯一规范来源改为 FROZEN Design §11.2 / §11.3。
7. **E2**：人工参与点从 2 个扩为 H1/H2/H3，真实验收明确需要用户选择来源并配合部署后真实消息。
8. **E3**：TG3 显式认领 `.gitignore` 安全规则，并规定必须在 H1 Telegram Auth 前生效。
9. **E4**：补齐 `IMPORTANT_CAPABILITY` 的生产者契约：TG6 仅复用 K2C 既有显式 high-value / important 信号；无显式信号时保留事件类型但不得自行发明分类规则。
10. **E5**：TG1 将 `doctor.py` 标记为 verify-only；Telegram 不加入 `CLOUD_SKILL_KEYS`，若核对后无需修改则保持原文件不变；v1 local handoff 测试继续沿用 inline 构造。
11. **E6**：§12 不再维护第二套 commit message，以各 TG 章节 Commit 小节为唯一准则；文档头补列实施方案终审报告路径。

---

# 0. 实施总原则

本实施方案不再讨论冻结设计中的产品决策，只负责回答：

1. 具体改哪些文件；
2. 每一步先写什么测试；
3. 各 Task 的依赖关系；
4. 每个 Milestone 如何验收；
5. 哪些步骤必须由用户现场参与。

冻结边界不得在实施期顺手修改，包括但不限于：

```text
白名单来源默认可信
不做历史 Backfill
同作者固定 5 分钟文字合并
PDF 先 Interest Policy
<=50 MiB 自动下载
>50 MiB REVIEW
网盘只保存正文 + URL，PENDING
Resource Collection 只做 Stub
Noise = 默认 KEEP / 明确广告 SKIP / 不确定 REVIEW
规则优先，LLM 只处理边缘判断
Source AI Budget 与 Target Budget 分账
SQLite = Telegram Source Facts
knowledge-ingest job.yaml = Job Facts
K2C Registry = Capability Facts
自动化最多到 staged
Publish / Activate 保留 Human Gate
WxPusher 只负责 ERROR / REVIEW / IMPORTANT_CAPABILITY + Digest
Digest = 08:00 / 12:00 / 20:00
Telethon 通过 TelegramClientPort 隔离
target=k2c 已存在，必须复用，不得旁路
```

实施过程中允许根据本机事实调整：

- 具体函数名；
- 实际文件插入位置；
- 当前 CLI parser 的挂载方式；
- LaunchAgent plist 的生成方式；
- uv/Telethon/pytest-asyncio 的兼容版本；
- 测试 fixture 的具体目录；
- SQLite migration 的具体 SQL 组织方式。

禁止借实施之机新增：

- 通用“万能 Source Framework”；
- 第二套 Job OS；
- 第二套 Capability Registry；
- Telegram Bot 运营体系；
- 网盘自动登录/目录抓取；
- 历史 Telegram 回溯；
- 自动 Publish / Activate；
- GitHub/Web/YouTube/公众号/Email Source。

---

# 1. 实施阶段总览

```text
TG0  Canonical Freeze + Preflight
  ↓
TG1  Source Handoff v2
  ↓
TG2  Telegram State Core + Source Registry
  ↓
TG3  Telegram Client + Auth + Watcher
  ↓
TG4  Source Item Builder + Materialization
  ↓
TG5  Classification + Source AI Budget
  ↓
TG6  knowledge-ingest → target=k2c E2E
  ↓
TG7  Notification Runtime + LaunchAgent
  ↓
TG8  Doctor + Recovery + Retention + Real Acceptance
```

原则上顺序实施，不为了“并行提速”制造跨里程碑冲突。

每个 TG 都遵循：

```text
先写失败测试
→ 最小实现
→ 相关测试全绿
→ ruff
→ milestone 回归
→ 独立 commit
```

任何 TG 全量回归出现红灯，停止进入下一阶段。

---

# 2. TG0 — Canonical Freeze + Preflight

## 2.1 目标

在写任何 Telegram 代码之前，先把冻结设计变成本仓库唯一 canonical source，并确认现有基线健康。

## 2.2 Canonical design 落库

Canonical 文件必须是：

```text
docs/k2c-telegram-knowledge-source-v2-design.md
docs/k2c-telegram-knowledge-source-v2-本机审阅意见.md
```

第一份文件必须与冻结稿逐字节一致，并校验：

```text
SHA-256 =
0d6e45de76785db2cf318a93261a6aabb31e2dc03993ce8693b134f7093e926c
```

旧路径：

```text
docs/k2c-telegram-knowledge-source-v1-design.md
```

作为历史文件**原地保留**，不执行 `git mv`，不以 V2 内容覆盖，不再作为任何实施输入。Agent 实施时只能读取 V2 canonical。

处理规则：

- 若 V2 canonical 已存在：只校验 SHA，不再重写；
- 若 V2 canonical 尚未落库：直接新增 V2 canonical，不移动或改写 V1；
- 不创建第二份“看起来同样有效”的 V2 副本；
- 如后续确需给 V1 增加 superseded 指针，必须作为独立文档维护动作处理，不与 TG0 功能实施混做。

## 2.3 Preflight

必须确认：

```text
local main == origin/main
target=k2c 20 个既有测试全绿
knowledge-ingest 全量测试全绿
ruff 全绿
/Volumes/ORICO/KnowledgePipeline 可写
K2C target registry / corpus identity / output manifest 均在
```

记录现有测试数量作为基线，不把未来新增测试混入“旧基线是否退化”的判断。

## 2.4 环境核对

只读检查：

```text
pyproject.toml
uv.lock
现有 LaunchAgent
~/.config/knowledge-ingest/
ORICO KnowledgePipeline
```

确认当前没有 Telegram session 时，只记录事实，不在 TG0 登录。

## 2.5 验收

- canonical frozen design SHA 正确；
- local/origin 同步；
- 旧 K2C 20 tests 全绿；
- 全量 tests 全绿；
- ruff 全绿；
- 不产生 Telegram 功能代码。

## 2.6 Commit

建议：

```text
docs: freeze Telegram Knowledge Source V2 design
```

---

# 3. TG1 — Source Handoff v2

## 3.1 目标

先打通 `provider=telegram` 的正式 Source Handoff 契约，同时保证 v1 `local/baidu/quark` 全部继续可用。

这是后续所有 Telegram Source Item 进入 knowledge-ingest 的唯一合法入口。

## 3.2 预计修改范围

至少核对以下影响面；其中 `doctor.py` 为 **verify-only**，核对后若无需修改则保持原样：

```text
src/knowledge_ingest/models.py
src/knowledge_ingest/cli.py
src/knowledge_ingest/doctor.py          # verify-only
templates/job.yaml
schemas/source-handoff.example.json
tests/fixtures/*-source-handoff.json
相关 unit / contract tests
report rendering tests（若受影响）
```

`doctor.py` 的核对结论预期为：

```text
telegram 不加入 CLOUD_SKILL_KEYS
telegram 不是 cloud skill provider
若现有 doctor 无 provider 枚举阻塞，则不修改 doctor.py
```

v1 `local` handoff 测试若当前没有独立 fixture 文件，继续沿用现有测试内联构造方式；不得为了形式一致额外创建无必要的 local fixture。

其中：

### `models.py`

扩展：

```python
ProviderName = Literal["local", "baidu", "quark", "telegram"]
```

不得改变现有 JobManifest target 状态机。

### `cli.py`

至少覆盖：

```text
job create --provider telegram
source register 完成门
schema_version v1 / v2 并存
provider_mismatch
baidu 专属范围门只对 baidu 生效
cli route 对 telegram provider 不做额外枚举分支
```

### Handoff version policy

冻结为：

```text
schema_version = 1
→ 继续支持既有 local / baidu / quark

schema_version = 2
→ 支持 telegram + provenance
```

不得写成：

```text
schema_version != 2 → reject
```

否则会反向破坏旧 v1。

建议完成门明确做：

```text
version in {1, 2}
provider 与 JobRequest.provider 一致
download_completed == true
local_path 存在
telegram/v2 provenance 满足最小契约
```

## 3.3 provenance 最小字段

Telegram v2 至少支持：

```text
platform
source_id
chat_id
message_ids
sender_id
message_url
first_message_at
last_message_at
```

允许后续字段扩展，但 reader 对未知键必须容忍。

## 3.4 TDD

先补测试：

```text
v1 local handoff 仍 PASS
v1 baidu handoff 仍 PASS
v1 quark handoff 仍 PASS
v2 telegram handoff PASS
v2 telegram provider mismatch BLOCKED
v2 telegram local_path missing BLOCKED
v2 telegram provenance 可读取
未知 provenance 键不导致拒绝
schema_version 3 仍 fail-fast
baidu /apps/bdpan 门不会误作用于 telegram
```

## 3.5 验收

- 旧 fixtures 全绿；
- `job create --provider telegram` 可创建 Job；
- `source register` 能注册 v2 telegram；
- v1 handoff 零回归；
- 不实现 Telegram 网络连接。

## 3.6 Commit

```text
feat: add backward-compatible Source Handoff v2 for telegram
```

---

# 4. TG2 — Telegram State Core + Source Registry

## 4.1 目标

建立 Telegram Source Runtime 的本地事实层，不接真实 Telegram 网络；同时认领纯本地运维入口：

```text
telegram status
telegram review list
telegram review resolve
```

TG2 只负责状态事实与人工决定持久化，不在本阶段执行真实下载或 Telegram 网络调用。

## 4.2 新增目录

按冻结设计：

```text
src/knowledge_ingest/telegram/
├── client_port.py
├── registry.py
├── event_store.py
└── ...
```

TG2 只实现：

```text
registry.py
event_store.py
必要的数据类型
review/status 的纯本地查询与状态变更入口
maintenance lock
```

CLI handler 可以先落在现有 `cli.py`，不为了形式完整额外拆模块。

不提前写 watcher、Telethon、WxPusher。

## 4.3 SQLite 初始化

数据根：

```text
/Volumes/ORICO/KnowledgePipeline/telegram/
```

数据库：

```text
state.db
```

必须启用：

```text
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
busy_timeout=5000
```

## 4.4 表

按冻结设计实现最小表：

```text
tg_sources
tg_messages
source_items
downloads
reviews
```

其中：

```text
tg_messages PRIMARY KEY (source_id, message_id)

downloads PRIMARY KEY (item_id, document_message_id)
```

`reviews` 至少保存冻结设计字段，并允许为 DOWNLOAD_ONCE 幂等消费增加最小实现字段，例如：

```text
decision_consumed_at
```

如果最终选择用 `source_items.processing_status` 表达消费状态，也必须满足同一幂等契约；不得同时维护两个互相冲突的授权事实源。

不要在 TG2 把 knowledge-ingest Job 状态复制进 SQLite。

只允许存：

```text
knowledge_ingest_job_id
handoff 是否完成
```

## 4.5 Schema version

SQLite 使用显式 schema version：

```text
PRAGMA user_version
```

TG2 首版 schema **固定为 user_version=1**。

只建立当前 Telegram Source 所需的最小迁移机制，不提前建设通用 migration framework。

必须保证：

- 初始化幂等；
- 已初始化 DB 再打开不破坏；
- `user_version=1` 可重复打开；
- 未知高版本 fail-fast；
- 后续 TG7 可在 maintenance lock 下执行 1→2。

## 4.6 Source Registry + Review/Status CLI

Source Registry 最终 CLI：

```bash
knowledge-ingest telegram sources list
knowledge-ingest telegram sources discover
knowledge-ingest telegram sources add ...
knowledge-ingest telegram sources enable ...
knowledge-ingest telegram sources disable ...
knowledge-ingest telegram sources show ...
```

TG2 先实现纯本地：

```text
sources list
enable
disable
show
telegram status
telegram review list
telegram review resolve
```

`discover/add` 等到 TG3 有真实 Telegram client 后接上。

### `telegram status`

只读汇总，不改变状态。至少能看到：

```text
DB schema/user_version
source enabled/disabled 数
last_seen / last_reconciled 摘要
open REVIEW 数
PENDING_RESOURCE 数
下载状态摘要
最近 handoff/job 关联摘要
```

### `telegram review list`

列出 unresolved REVIEW，至少展示：

```text
review_id
item_id
reason
kind
size（PDF 可得时）
created_at
```

### `telegram review resolve`

冻结允许决定：

```text
KEEP
SKIP
DOWNLOAD_ONCE
```

要求：

- 同 review 重复提交同一 decision → 幂等 no-op；
- 已解决 review 再提交不同 decision → 明确拒绝，不静默覆盖；
- `DOWNLOAD_ONCE` 只创建**一个 item 级授权事实**，TG2 不执行下载；
- TG4 负责消费该授权。

## 4.7 start_at 契约

Source 首次启用时必须持久化：

```text
start_at
last_seen_message_id
```

TG2 用 fixture 测试硬边界：

```text
message_date < start_at → 永不生成 Source Item
```

## 4.8 并发

### watcher 内部

为后续 watcher 预留**单写队列**契约。

TG2 的 EventStore API 不允许业务代码直接拿裸 sqlite connection 到处写。

### CLI

短事务：

```text
BEGIN
update
COMMIT
```

服从 busy_timeout。

### maintenance lock

以下操作必须持锁：

```text
doctor --fix
retention cleanup
schema migration
批量 repair
```

优先复用仓库现有 flock 风格；不要引入复杂分布式锁。

## 4.9 TDD

覆盖：

```text
DB 初始化
user_version=1
WAL 生效
foreign_keys 生效
busy_timeout 生效
重复 message upsert
重复 source add 幂等
enable / disable
start_at 不倒退
downloads 复合主键
review 状态迁移
review list 只返回 unresolved
review resolve 同 decision 幂等
review resolve 冲突 decision 拒绝
DOWNLOAD_ONCE 仅落授权，不触发下载
telegram status 只读且可在空 DB 正常输出
maintenance lock 拒绝并发 fix
未知 schema version fail-fast
```

## 4.10 验收

使用 fake data 完成：

```text
Source Registry
→ Raw TelegramEvent
→ SQLite commit
→ telegram status
→ review list / resolve
```

并证明 DOWNLOAD_ONCE 在本阶段只成为持久化授权，不产生网络下载。

尚不连接 Telegram。

## 4.11 Commit

```text
feat: add Telegram source state store registry and review CLI
```

---

# 5. TG3 — Telegram Client + Auth + Watcher

## 5.1 目标

接入真实 Telegram MTProto，但业务层不依赖 Telethon 类型；同时建立 New/Edit/Delete 三类 update 的统一内部事件契约。

## 5.2 Optional dependencies + CI 契约

修改：

```text
pyproject.toml
uv.lock
.gitignore
```

其中 `.gitignore` 是 **H1 Telegram Auth 前置安全门**。在任何真实 Telegram 登录/session 生成之前，必须确认以下规则已经生效：

```gitignore
*.session
*.session-journal
credentials.env
telegram-secrets*
wxpusher.env
```

未满足上述规则时不得执行 `knowledge-ingest telegram auth`。

要求：

- Telethon 放入 `[project.optional-dependencies]` 的 telegram extra，不进入核心 dependencies；
- Telegram 未启用时，核心 KI 可以不安装 Telethon；
- `pytest-asyncio` 进入 dev/test 依赖；
- 具体兼容版本由本机 `uv` 求解，不在方案里硬编码失真的版本号。

### 无 extra CI 契约

本仓库当前默认 CI/本地基线是普通 `uv sync`，不会自动安装 telegram extra。因此 TG3 必须保证：

```text
1. core/business layer 不得顶层 import Telethon；
2. telethon_adapter.py 使用延迟导入或等效隔离；
3. adapter-specific tests 在未安装 Telethon 时使用 pytest.importorskip；
4. 业务层 unit/integration tests 一律走 FakeTelegramClient；
5. 未安装 telegram extra 时：
   - import knowledge_ingest 成功
   - import knowledge_ingest.cli 成功
   - 非 adapter tests 全绿
   - 完整 pytest 收集不得因 ModuleNotFoundError: telethon 直接失败
```

另增加一条最小契约测试，明确守住“Telegram extra 未安装不影响 KI 核心”这一边界。

## 5.3 TelegramClientPort

`client_port.py` 至少定义：

```python
list_dialogs()
watch_updates()
fetch_messages_after()
get_message()
download_document()
resolve_chat()
```

上层只认识内部对象，例如：

```text
TelegramEvent
TelegramDocumentRef
TelegramDialog
```

`TelegramEvent` 至少能表达：

```text
NEW
EDIT
DELETE
```

删除 update 若拿不到完整正文，也必须至少携带足以定位 `(source_id, message_id)` 的身份字段。

禁止 Telethon `Message` / `Document` 穿透业务层。

## 5.4 TelethonAdapter

`telethon_adapter.py` 负责：

```text
Telethon session 初始化
dialog → 内部 TelegramDialog
NewMessage → TelegramEvent(kind=NEW)
MessageEdited → TelegramEvent(kind=EDIT)
MessageDeleted → TelegramEvent(kind=DELETE)
document → TelegramDocumentRef
FloodWait / RPCError → 内部错误类别
文件下载
增量 fetch
```

## 5.5 Auth

CLI：

```bash
knowledge-ingest telegram auth
```

### 外部人工环节 H1

此步骤必须用户现场参与：

```text
手机号
Telegram 登录码
二步验证密码（如启用）
```

禁止：

- 把验证码写日志；
- 把 2FA 密码写盘；
- 把 session 提交 Git。

成功后核对：

```text
session 文件存在
目录 700
session 600
.gitignore 已在登录前生效且实际命中 session / credentials
wxpusher.env 同样受 .gitignore 保护
```

## 5.6 discover / add

接真实账号：

```bash
knowledge-ingest telegram sources discover
knowledge-ingest telegram sources add <chat-id-or-username>
```

第一次 add 时：

```text
读取当前 latest message id/time
写 start_at = 当前启用时间
写 last_seen_message_id = 当前边界
```

不得把已有旧消息导入。

## 5.7 Watcher

`watcher.py` 两条输入路径：

```text
A. live updates
B. periodic reconcile
```

两者统一先：

```text
Telegram event
→ SQLite transaction commit
```

之后才允许下游处理。

### NEW

按 `(source_id, message_id)` upsert 原始事实。

### EDIT

先更新 Raw Event 的最新事实与 `edited_at`，**不在 TG3 决定是否重写 materialization/Corpus**；具体 Source Item 语义由 TG4 实现。

### DELETE

只更新 Raw Event 的 `deleted_at`/删除事实，不物理删除 SQLite 行，不直接删除任何下游产物；具体 provenance/下游语义由 TG4 实现。

### reconcile

只能补：

```text
message_date >= source.start_at
message_id > last_seen_message_id / 已知边界
```

对于 reconcile 发现的已编辑消息，同样走 EDIT 事实更新，不旁路 EventStore。

## 5.8 TDD

FakeTelegramClient 覆盖：

```text
live NEW message
重复 NEW update
EDIT update 更新同一 Raw Event，不生成第二条 message
DELETE update 标记同一 Raw Event，不物理删除
服务重启
中断期间增量补齐
reconcile 发现 edit 后仍落同一 message identity
start_at 前旧消息不进入
FloodWait 映射
RPC error 映射
source disabled 后不采集
无 Telethon extra 时 core import / non-adapter tests 仍通过
```

真实 Telegram 只做 smoke test，不承担 unit test。

## 5.9 验收

真实账号完成：

```text
auth
discover
add 一个测试来源
watcher 启动
收到一条部署后新消息
SQLite 产生唯一 tg_messages 记录
```

Edit/Delete 的行为先以 FakeClient 验收；真实 Edit/Delete 留到 TG8 的 Real Acceptance，避免为了造数据让 Agent 自动发 Telegram 消息。

禁止历史回溯。

## 5.10 Commit

```text
feat: add Telegram MTProto client auth and incremental watcher
```

---

# 6. TG4 — Source Item Builder + Materialization

## 6.1 目标

把 Raw Telegram Event 转换成冻结的三类 Source Item：

```text
TEXT
PDF
CLOUD_LINK
```

并补齐冻结设计 §20 的编辑/删除语义与 >50 MiB PDF 的 DOWNLOAD_ONCE 授权消费闭环。

不在本阶段接真实 LLM。

## 6.2 TEXT

实现固定窗口：

```text
same source
same sender
text only
first-message anchored 5 minutes
```

注意：

> 不是 rolling 5 minutes。

第 1 条消息时间固定窗口终点；后续消息不能继续延长窗口。

PDF / cloud link 消息不并入 TEXT。

## 6.3 TEXT Materialization

输出：

```text
materialized/<item-id>/message.md
```

只写原始正文。

Telegram provenance 不混进正文。

## 6.4 编辑 / 删除状态语义

以 **handoff 是否已经发生** 作为不可逆边界。

### A. 尚未 finalized

```text
EDIT
→ 更新 Raw Event
→ 重建当前 open Source Item
```

不得额外创建第二个 Item。

### B. 已 finalized，但尚未 handoff

```text
EDIT
→ 更新 Raw Event
→ 旧 materialization 标记失效
→ 用最新事实重建同一 item_id
→ 清理/重置需要重算的 policy/materialization 状态
→ 不创建 KI Job
```

核心要求：同一 Telegram 逻辑 Item 仍只有一个 canonical Source Item identity。

### C. 已 handoff 到 knowledge-ingest

```text
EDIT
→ 不反向改写已有 Job / Corpus
→ 记录 SOURCE_EDITED_AFTER_HANDOFF
→ 创建/保持 REVIEW
→ 标记 update candidate
```

禁止自动覆盖已经进入 Corpus 的正文。

### D. DELETE

```text
DELETE
→ tg_messages.deleted_at
→ source_deleted=true（Source Item/provenance 可见）
→ 不物理删除 Raw Event
→ 不物理删除 materialization / Job / Corpus / Capability
```

如果删除发生在 handoff 前，可以阻止尚未开始的自动 handoff；如果已 handoff，只追加 source-deleted provenance，不进行跨层回滚。

## 6.5 PDF

先只建立：

```text
caption
filename
mime
size
telegram_document_id
source provenance
```

在 TG5 分类之前使用 fake decision 驱动测试。

下载成功后：

```text
SHA-256
downloads.status
local_path
attempts
```

50 MiB 使用二进制 MiB：

```text
50 * 1024 * 1024
```

## 6.6 CLOUD_LINK

识别至少：

```text
quark
baidu
unknown
```

输出：

```text
PENDING_RESOURCE
ResourceCollectionStub（符合大合集描述时）
```

绝不自动：

```text
登录网盘
列目录
下载文件
创建数千 Job
```

## 6.7 DOWNLOAD_ONCE 授权消费

对于：

```text
Interest = INCLUDE
AND size > 50 MiB
```

默认必须 REVIEW，不下载。

只有 TG2 已持久化：

```text
review decision = DOWNLOAD_ONCE
```

时，TG4 下载器才允许进入该 Item 的**一次人工授权工作流**。

“DOWNLOAD_ONCE”定义为：

> 对该 `item_id` 的一次人工授权，而不是只允许一个底层 MTProto 网络 attempt。

因此：

- 同一授权内的临时失败可按既有 retry/resume 继续；
- watcher 重启仍沿用同一 `downloads` row；
- 不创建第二份授权；
- 不创建第二个 Source Item；
- 不创建第二个 KI Job；
- 下载成功后原子记录授权已消费/完成；
- 已消费授权再次 replay 不得再次启动第二轮下载。

授权 claim/consume 与 download 状态写入必须通过短事务保持幂等。

## 6.8 ORICO fail-closed

在：

```text
watcher 启动
reconcile
PDF 下载前
materialize 前
```

都必须实际检查 ORICO 数据根。

不可只读取 `processing.require_orico`。

ORICO 不可用：

```text
不 fallback 到内置盘
不下载
不 materialize
Item 保持可恢复状态
记录 ERROR event
```

## 6.9 去重

Raw event：

```text
(source_id, message_id)
```

PDF 内容：

```text
SHA-256
```

最终跨来源内容复用仍交给 knowledge-ingest fingerprint/cache。

## 6.10 TDD

覆盖：

```text
同作者 0/1/299/300 秒边界
第 301 秒拆新 Item
rolling-window 不得发生
PDF 打断 TEXT merge
cloud link 独立 Item
quark / baidu / unknown
50 MiB -1 byte
50 MiB exact
50 MiB +1 byte
重复 PDF metadata
SHA-256
ORICO absent fail-closed
ResourceCollectionStub 不触发下载
EDIT: 未 finalized → 重建 open item
EDIT: finalized 未 handoff → 同 item_id 重建 materialization
EDIT: handoff 后 → SOURCE_EDITED_AFTER_HANDOFF + REVIEW，不改 Corpus
DELETE: deleted_at/source_deleted，Raw/下游不物理删除
DOWNLOAD_ONCE: 无授权不下载
DOWNLOAD_ONCE: 有授权可进入下载
DOWNLOAD_ONCE: 同授权 retry/restart 不重复授权、不重复 Job
DOWNLOAD_ONCE: 完成后 replay 不再二次下载
```

## 6.11 验收

完全不使用 LLM，也能把 fake/raw event 稳定生成三类 Source Item，并证明：

```text
编辑不会产生重复 Item/Job
handoff 后编辑不会反向改 Corpus
删除不会物理抹掉下游知识
>50 MiB PDF 必须经 DOWNLOAD_ONCE 才能进入下载
```

## 6.12 Commit

```text
feat: build Telegram source items edit semantics and materialization pipeline
```

---

# 7. TG5 — Classification + Source AI Budget

## 7.1 目标

实现“规则优先、LLM 只处理必要边界”的分类层。

## 7.2 Noise Filter

TEXT 默认：

```text
普通白名单知识 → KEEP，不调用模型
明确规则广告 → SKIP，不调用模型
疑似广告/知识混合 → LLM
模型失败 → REVIEW
```

PDF：

```text
规则级明确广告预筛
→ Interest Classifier
```

CLOUD_LINK：

```text
默认 KEEP / PENDING_RESOURCE
```

## 7.3 Interest Policy

Interest Policy 的**唯一规范来源**是：

```text
FROZEN Design §11.2 INCLUDE
FROZEN Design §11.3 EXCLUDE / 投资正例保护
```

实施方案不再复制第二份兴趣枚举，避免设计与实现文档漂移。

实现时必须把 policy 做成可版本化配置/数据，并在 classifier 审计记录中写入 `policy_version`。

TDD 可以保留代表性样本，但这些样本不是完整枚举，例如：

```text
AI PDF → INCLUDE
审计 PDF → INCLUDE
民宿 PDF → INCLUDE
企业分析/长期投资方法 → INCLUDE 或 REVIEW
荐股/短线 → EXCLUDE
恋爱技巧 → EXCLUDE
“上市公司商业模式分析”不得因股票词被误杀
```

任何新增/删除正式兴趣类别，都必须修改 FROZEN Design 的后续显式版本，而不是只改本实施方案。

## 7.4 Structured output

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

未知 enum / JSON 解析失败：

```text
REVIEW
```

## 7.5 Source AI Budget

独立于 Target Budget。

复用现有 `budget.py` 设计原则：

```text
acquire
outcome
quota
retry limit
breaker
```

但状态写 Telegram SQLite，不写 JobManifest.target budget。

至少区分：

```text
noise_classifier
pdf_interest_classifier
review_assist
```

推荐按 source + classifier_kind 记账。

不要一开始增加复杂计费货币模型；V2 先约束：

```text
calls
attempts
consecutive_empty
consecutive_rate_limit
permits/request_id
```

## 7.6 模型调用通道

优先复用本机已有 worker/router。

实现层必须 injectable：

```text
Classifier callable / adapter
```

不要把 Claude/OpenAI/某模型 SDK 直接写死在 `classify.py`。

## 7.7 TDD

覆盖：

```text
普通 TEXT = 0 次模型调用
明确广告 = 0 次模型调用
疑似广告 = 1 次模型调用
模型 failure → REVIEW
非法 JSON → REVIEW
未知 enum → REVIEW
AI PDF INCLUDE
审计 PDF INCLUDE
民宿 PDF INCLUDE
企业分析 PDF INCLUDE
荐股 PDF EXCLUDE
恋爱 PDF EXCLUDE
“上市公司商业模式”不得误杀
budget replay 幂等
budget exhausted → REVIEW/KEEP-safe
breaker open → REVIEW/KEEP-safe
```

## 7.8 验收

使用 fixture corpus 验证分类，不依赖真实 Telegram 群。

## 7.9 Commit

```text
feat: add Telegram intake policies and source AI budget
```

---

# 8. TG6 — knowledge-ingest → target=k2c E2E

## 8.1 目标

正式打通：

```text
Telegram Source Item
→ Source Handoff v2
→ knowledge-ingest Job
→ route / preprocess
→ Verified Corpus
→ target=k2c
→ K2C compile
→ staged
```

不得新建旁路。

## 8.2 TEXT Handoff

Materialized：

```text
message.md
```

创建 Job：

```text
provider=telegram
target=k2c
```

生成：

```text
source.json schema_version=2
```

注册后完全走现有 KI。

## 8.3 PDF Handoff

仅：

```text
Interest=INCLUDE
AND size <= 50 MiB
AND download complete
```

才自动创建 KI Job。

`>50 MiB` 或 REVIEW 不得偷跑。

## 8.4 Job idempotency

`source_items.knowledge_ingest_job_id` 作为 Telegram 层关联。

同一 `item_id`：

```text
handoff retry
watcher restart
reconcile replay
```

都不得产生两个有效 KI Job。

## 8.5 target=k2c 回归

必须复用当前：

```text
Target Registry
corpus identity handoff
k2c-output-manifest
checkpoint
budget
staged
Human Gate
```

Telegram 实施不得修改其语义。

## 8.6 K2C 自动化边界

自动：

```text
compile → staged
```

停止于：

```text
Publish / Activate
```

不得为了 E2E 测试自动批准 Human Gate。

## 8.7 `IMPORTANT_CAPABILITY` 生产者契约

TG6 负责核对 K2C staged 输出是否已经提供**既有、显式**的 high-value / important 信号，并据此决定是否产生 `IMPORTANT_CAPABILITY` Notification Event。

冻结规则：

```text
已有显式 high-value / important 信号
→ TG6 可产生 IMPORTANT_CAPABILITY

没有可复用的显式信号
→ 保留 IMPORTANT_CAPABILITY event type
→ 当前标记“无生产者”
→ 不发送该类通知
```

禁止：

```text
把所有 staged Capability 默认视为 important
新增一套启发式 important 规则
临时调用分类模型判断“重要”
修改 K2C FROZEN 语义来制造该信号
```

如果实施时核对发现 K2C 当前没有显式信号，必须把“IMPORTANT_CAPABILITY 当前无生产者”写入 TG6 验收记录，而不是静默补规则。

## 8.8 TDD / Integration

覆盖：

```text
TEXT → telegram handoff v2 → KI → corpus
PDF → telegram handoff v2 → KI → corpus
同一 item retry 不重复 Job
旧 local/baidu/quark Job 不退化
target=k2c 20 tests 全绿
telegram provider route 动态工作
K2C output manifest 仍按原状态映射
```

## 8.9 Real E2E

### 外部人工环节 H3-A

真实 E2E 需要用户选择/确认一个可用于验收的白名单来源，并配合等待或提供**部署启用之后**产生的真实 TEXT/PDF/链接样本。Agent 不得自行向 Telegram 群/频道发送消息造数据。

选择一个用户允许的真实 Telegram 来源：

```text
部署后新 TEXT
→ staged
```

再验证一个真实 `<50 MiB` 感兴趣 PDF：

```text
Telegram
→ download
→ SHA
→ KI
→ docchunk verify PASS
→ K2C staged
```

## 8.10 验收

必须有真实 evidence：

```text
source_item id
source.json
job.yaml
Verified Corpus
target-k2c.yaml
k2c-output-manifest
staged capability reference
IMPORTANT_CAPABILITY producer check result
```

其中最后一项必须明确二选一：

```text
复用了 K2C 既有显式 important/high-value 信号
或
当前 K2C 无显式信号，因此 IMPORTANT_CAPABILITY 暂无生产者
```

## 8.11 Commit

```text
feat: connect Telegram source items to K2C target end to end
```

---

# 9. TG7 — Notification Runtime + LaunchAgent

## 9.1 目标

实现冻结的微信异常/审核/摘要通知，不转发普通 Telegram 原文；并把 TG2 的 SQLite `user_version=1` 安全迁移到通知运行时所需的 `user_version=2`。

## 9.2 NotificationPort

标准事件至少：

```text
event_id
event_type
source_item_id / job_id / capability_id
title
summary
created_at
attempt
delivery_status
```

类型：

```text
ERROR
REVIEW_REQUIRED
IMPORTANT_CAPABILITY
DIGEST
```

其中 `IMPORTANT_CAPABILITY` 的生产规则以 TG6 的生产者契约为准；TG7 只负责发送/幂等/记录，不得自行判断 Capability 是否“重要”。

## 9.3 WxPusherAdapter

只负责：

```text
序列化
发送
响应解析
有限 retry
delivery outcome
```

业务层不得直接写 WxPusher HTTP。

## 9.4 SQLite 1→2 Migration

TG7 新增 notification / digest persistence 时，必须显式升级：

```text
user_version: 1 → 2
```

建议最小新增事实表：

```text
notification_deliveries
digest_cursors
```

具体字段可按实现收口，但必须覆盖：

```text
event_id UNIQUE
attempt / delivery_status / last_error
last_successful_digest_at / cursor
```

迁移规则：

- 迁移前必须获取 maintenance lock；
- DDL 幂等；
- 现有 `tg_sources/tg_messages/source_items/downloads/reviews` 数据不得丢失；
- `user_version=1` 必须可升级到 2；
- 全新 DB 在 TG7 之后直接初始化为 latest schema（2），其结果必须与 0→1→2 等价；
- `user_version>2` fail-fast，不猜测降级；
- migration 失败时不得把 user_version 提前写成 2。

## 9.5 Delivery persistence

稳定 `event_id`：

```text
同一事件重复执行
→ 不重复推送
```

临时网络失败允许有限 retry。

永久失败进入：

```text
doctor/status 可见
```

不得无限 retry。

## 9.6 Digest cursor

必须持久化：

```text
last_successful_digest_at / cursor
```

统计：

```text
上一次成功 Digest cursor → 本次执行时间
```

而不是“今天 0 点以来”或进程内存。

## 9.7 Digest 内容

按冻结设计：

```text
新采集消息数
Source Item 数
TEXT
PDF
网盘 Pending
广告 SKIP
兴趣 EXCLUDE
REVIEW
重复复用
进入 KI
verify PASS / FAIL
K2C staged
重要 Capability 摘要
```

## 9.8 LaunchAgent

两个独立服务：

### Watcher

```text
telegram-watch
KeepAlive
```

负责：

```text
live watch
reconcile
```

### Digest

```text
telegram-digest
StartCalendarInterval
08:00 / 12:00 / 20:00
```

负责读取持久 cursor。

不得复用 `ki-resume` 的 `StartInterval=900` 当 watcher。

## 9.9 外部人工环节 H2

WxPusher 真实发送前需要用户提供/配置真实凭据。

要求：

```text
~/.config/knowledge-ingest/notifications/wxpusher.env
chmod 600
```

测试顺序：

```text
FakeNotifier
→ real WxPusher 单条测试
→ REVIEW 测试
→ Digest 测试
```

## 9.10 TDD

覆盖：

```text
v1 DB → v2 migration
migration 重跑幂等
迁移后旧 source/item/review 数据不丢
fresh DB 直接得到 latest schema v2
migration lock 冲突时拒绝执行
migration 失败不提前更新 user_version
event_id 幂等
重复 retry 不重复消息
临时失败 retry
永久失败停止
delivery log
digest cursor
错过 12:00 后补算
补算后不重复
普通 Telegram TEXT 不触发实时微信
ERROR 触发
REVIEW 触发
```

## 9.11 验收

真实微信至少收到：

```text
1 条 TEST/ERROR
1 条 REVIEW_REQUIRED
1 条 Digest
```

普通知识消息不得逐条出现。

同时证明：

```text
state.db user_version=2
旧 TG2 数据仍在
notification_deliveries 幂等
digest cursor 可重启恢复
```

## 9.12 Commit

```text
feat: add Telegram notification runtime database migration and launchd services
```

---

# 10. TG8 — Doctor + Recovery + Retention + Real Acceptance

## 10.1 目标

把“能跑”收紧成“可长期无人值守”，并对编辑/删除、REVIEW/DOWNLOAD_ONCE、真实恢复链路做最终验收。

## 10.2 Telegram Doctor

CLI：

```bash
knowledge-ingest telegram doctor
```

至少检查：

```text
Telegram session 存在
session 权限
API 登录有效
ORICO 在线
state.db integrity
user_version 与 latest schema 一致
enabled source 可 resolve
cursor 未倒退
materialized 文件存在
complete download 文件存在
SHA 匹配
source_item → job_id 存在
REVIEW 积压
DOWNLOAD_ONCE 未完成/异常授权
长时间未 reconcile
Notification delivery 失败
LaunchAgent 状态
```

## 10.3 `doctor --fix`

只能自动修复无争议项目。

必须 maintenance lock。

以下必须人工确认：

```text
大范围补消息
重新下载文件
覆盖物化文件
批量改状态
重建数据库
```

## 10.4 Retention

KEEP：

```text
最小 provenance 长期
原始 PDF 保留
```

SKIP：

```text
原始正文/载荷约 30 天
最小审计记录长期
```

REVIEW：

```text
未解决不清理
```

实现 cleanup 时必须：

```text
dry-run
→ 明确候选数
→ maintenance lock
→ delete
```

已删除的 Telegram 源消息仍按 §20 语义保留审计事实，不得被 retention 误当作“可立即物理删除”。

## 10.5 Crash / Restart Acceptance

必须实测：

```text
watcher 正常运行
→ 人工停止
→ Telegram 新发消息
→ watcher 重启
→ reconcile 补齐
```

并证明：

```text
只补 start_at 后
不产生重复 Item
不产生重复 KI Job
```

## 10.6 ORICO Failure Acceptance

实测或安全模拟：

```text
ORICO unavailable
→ watcher fail-closed
→ 不写内置盘
→ ERROR
→ ORICO 恢复
→ 正常继续
```

## 10.7 外部人工环节 H3-B

真实验收需要用户：

```text
选择/确认测试白名单来源
配合等待或提供部署启用后的真实 TEXT
配合等待或提供真实 PDF / 网盘链接
在需要时对一条 >50 MiB PDF 做 DOWNLOAD_ONCE 人工决定
如条件允许，人工编辑/删除一条用于验收的既有测试消息
```

Agent 不得为了造验收数据自动向 Telegram 发消息、编辑消息或删除消息。

如果真实来源无法安全制造 Edit/Delete，用 Fake/fixture 完成严格语义验收，并把 Real Acceptance 标记为“外部样本未出现”，不得伪造通过。

## 10.8 Real Acceptance Matrix

最终至少完成：

```text
A. 新纯文字
B. 5 分钟连续文字合并
C. >5 分钟拆分
D. 明确广告 SKIP
E. 疑似广告 REVIEW
F. 感兴趣 PDF <50 MiB 自动进入 K2C
G. 荐股 PDF EXCLUDE
H. 感兴趣 PDF >50 MiB REVIEW
I. 无法判断 PDF REVIEW
J. 重复 PDF SHA 复用
K. 夸克链接 PENDING
L. 百度链接 PENDING
M. 176G/2755份类大合集 Stub，不下载
N. watcher crash/restart 增量恢复
O. no historical backfill
P. WxPusher REVIEW
Q. 08/12/20 Digest cursor 补算
R. Telegram → KI → K2C staged
S. Publish/Activate 未被自动执行
T. EDIT：handoff 前重建同一 Item，不产生重复 KI Job
U. EDIT：handoff 后记录 SOURCE_EDITED_AFTER_HANDOFF + REVIEW，不反向改 Corpus
V. DELETE：deleted_at/source_deleted 可见，下游知识不物理删除
W. DOWNLOAD_ONCE：一次 item 级授权；retry/restart 不重复授权、下载或 Job
```

## 10.9 全量回归

最终必须：

```text
ruff 全绿
knowledge-ingest 全量 tests 全绿
target=k2c 既有 20 tests 全绿
Telegram unit tests 全绿
Telegram contract tests 全绿
Telegram integration tests 全绿
无 telegram extra 的核心 import / 非 adapter 测试契约全绿
```

## 10.10 交付证据

输出一份实施验收记录，至少包含：

```text
Git commit / HEAD
测试统计
真实 source_id
真实 message_id（可脱敏）
Source Item
edit/delete evidence（若真实样本出现）
REVIEW / DOWNLOAD_ONCE evidence
KI job_id
Corpus id
target-k2c handoff
K2C staged reference
LaunchAgent status
Doctor result
Digest delivery result
```

## 10.11 Commit

```text
test: complete Telegram source recovery edit and real acceptance
```

---

# 11. 外部人工参与点

实施中的 TDD、FakeTelegramClient、SQLite、KI integration 可由 Agent 独立完成；但真实外部系统有以下人工参与点。

## H1 — Telegram Auth

发生于 TG3：

```text
knowledge-ingest telegram auth
```

用户输入：

```text
手机号
验证码
2FA（如启用）
```

Agent 不得保存验证码或 2FA 密码。

## H2 — WxPusher

发生于 TG7：

用户提供/配置 WxPusher 所需真实凭据，并确认测试微信确实收到消息。

## H3 — Real Acceptance Source/Data

发生于 TG6/TG8。

用户需要：

```text
选择或确认一个可用于验收的白名单 Telegram 来源
配合等待/提供部署启用后的真实 TEXT/PDF/网盘链接
必要时人工做 REVIEW / DOWNLOAD_ONCE 决策
条件允许时配合一条真实 Edit/Delete 验收
```

Agent 不得自行发送、编辑或删除 Telegram 消息来制造验收数据。

若外部真实样本在验收窗口内没有自然出现，相应项目必须如实标记为“fixture/fake 已通过，real sample pending”，不得伪造真实通过。

---

# 12. Commit / 回滚策略

推荐每个 TG 一个逻辑 commit，必要时 Task 内可多个小 commit，但进入下一 TG 前应整理成可读边界。

**最终 commit 名称以各 TG 章节的 `Commit` 小节为唯一准则；本节不再维护第二套 commit message，避免文档漂移。**

阶段顺序仅为：

```text
TG0
↓
TG1
↓
TG2
↓
TG3
↓
TG4
↓
TG5
↓
TG6
↓
TG7
↓
TG8
```

禁止：

```text
force push main
把多个未验收 TG 混成一个巨大 commit
修测试时顺手重构无关模块
为通过测试修改冻结语义
```

TG0 后保留之前的安全备份分支，至少到 TG6 E2E 通过后再考虑删除。

---

# 13. Agent 每个 TG 的固定执行模板

每个 Milestone 都按：

```text
1. 重新读取 FROZEN design 对应章节
2. 核对当前 main / origin/main
3. 列出本 TG 要修改的文件
4. 先写失败测试
5. 只实现本 TG 所需最小代码
6. 跑本 TG 测试
7. 跑相关既有回归
8. ruff
9. 必要时全量 pytest
10. 展示 diff
11. 自审“是否触碰冻结边界”
12. commit
13. push 前确认工作区
14. 普通 push
15. local/origin 再核对
```

如果发现设计与本机事实冲突：

```text
STOP
→ 不自行改 FROZEN design
→ 形成 conflict report
→ 交给用户决策
```

---

# 14. 实施成功的最终定义

不是“Telegram 能收到消息”，而是以下链路同时成立：

```text
白名单 Telegram Source
        ↓
Raw Event 持久化
        ↓
Source Item
        ↓
轻量 Noise / Interest Policy
        ↓
TEXT / PDF / PENDING_RESOURCE
        ↓
knowledge-ingest
        ↓
Verified Corpus
        ↓
target=k2c
        ↓
K2C compile
        ↓
staged
```

同时：

```text
历史消息没有被回溯
重复消息没有重复入库
大 PDF 没有被擅自下载
网盘合集没有被自动展开
模型故障没有丢知识
ORICO 掉线没有写内置盘
watcher 重启可以补齐
微信没有变成第二条 Telegram 信息流
Publish / Activate 没有被自动越权
```

达到以上条件，Telegram Source V2 才算正式完成。

---

# 修订记录 V2.1（2026-09-19，TG0–TG6 落地 + 通道灰度实测后）

> 依据：TG0–TG6 代码全部落地（main=b599aeb，513 测试绿，六轮评审 R1–R11 全部闭环）；
> 分类通道灰度上线（21 次真实调用、熔断零异常）；14 源真实运行。
> 本修订只调整**后续**实施的顺序与范围；FROZEN Design（SHA 0d6e45de…）不变；
> 附件分类学等 schema 级变更随 TG7 的 `user_version 1→2` 迁移实施，
> 并按设计文档"显式版本升级"规则补记。执行时机由用户逐项放行（P0 优先）。

## V2.1-0 状态基线（本修订的事实前提）

- 已交付（超出原方案范围的部分）：
  `telegram handoff <item_id>`（显式单条交付，幂等）、
  `telegram budget show|reset`（通道账本运维面，熔断/耗尽可恢复）、
  `telegram classify-dryrun [--limit N]`（只读试跑通道）、
  `telegram download <item_id>`（人工下载入口）、
  通道 env 配置（`KI_TELEGRAM_LLM_CMD/_TIMEOUT/_MAX_CALLS/_BREAKER_*/_CWD`）。
- 真实运行证据：视频附件可经既有媒体腿本地转写（router 原生认 `.mp4`；
  Fun-ASR-Nano 本地推理零网络流量；实测 23s / 153s 视频的 preprocess
  墙钟分别 30s / 53s）；5 条附件下载 complete（~39 MB，SHA 均落盘）。
- 实测缺陷/欠账（本修订要吸收的）：
  ① 4/5 个"PDF"实为 MP4（`classify_kind` 只看有无 document，kind 误标）；
  ② 同一 SHA 附件被两个源各下载一遍（浪费 9.3 MB）；
  ③ 8 条 text item 永悬 `open`（安静源无关窗定时器，14 源后只会更多）；
  ④ CLI 与 watcher 抢 telethon session 报 `database is locked`
  （实测三次连续失败，登记源时须停 watcher）；
  ⑤ review #23（10.87 MB 真 PDF）开放 1.5 小时无任何通知渠道。

## V2.1-1 【P0，提前自 TG7】watcher 持久化

理由：watcher 当前为 nohup 裸进程——**重启即死**，且通道环境变量仅存在于
启动命令中，重启后无人带参拉起，reconcile 不会自愈；本机存在凌晨自动重启
场景，此为当前最高运维风险。

1. LaunchAgent（RunAtLoad + KeepAlive）承载 `telegram watch`，
   路径动态生成（沿用 `watchdog install` 的动态模板纪律，不硬编码）。
2. 通道与开关环境变量写入 plist（LLM_CMD/TIMEOUT/MAX_CALLS/
   BREAKER_*/LLM_CWD）；`KI_TELEGRAM_HANDOFF` 显式写 0（可审计）。
3. `telegram watch-agent install|status|uninstall` 子命令；install 会把
   `--config` 钉进 ProgramArguments（launchd 的 cwd 不可控）。
4. 验收：kill -9 后自动复活；`launchctl kickstart` 模拟重启后，
   无人工干预恢复采集与通道（横幅/账本可证）。
5. **本机现实约束（2026-09-19 实测，代码评审后补记）**：macOS TCC 默认
   拒绝 launchd 子进程访问可移动卷（ORICO）——探针 touch 实锤 exit 1，
   interpreter 读卷上文件挂起。完成 P0 部署需先在"隐私与安全性 → 完整
   磁盘访问"对解释器本体授权；授权前 watcher 以 nohup 过渡运行
   （迁移顺序：停 nohup → install → status 验证；install 对已运行
   watcher 有警告）。同根因：v0.3 ki-resume watchdog 自 09-13 起未跑成
   （launchctl exit 78），修复方式相同。验收第 4 条在授权前不可验证，
   P0 状态 = 代码完成、部署待授权。

## V2.1-2 TG6 收尾（锚点变更）

1. H3-A 验收路径由"KI_TELEGRAM_HANDOFF=1 自动扫描"改为
   **`telegram handoff <item_id>` 显式交付**（§8.10 证据清单不变）。
   已实跑：text（tg_trivia:30481，TARGET_RUNNING，checkpoint
   `awaiting_richer_sample`）、video ×2（CORPUS_READY）。
   收尾 = 挑一个够厚的样本（候选：tg_vip_docs:3084 真 PDF，
   待 review #23 人工 KEEP）编译至 staged，归档 §8.10 证据与
   IMPORTANT_CAPABILITY 生产者核对（无显式信号 = 无生产者）。
2. 自动扫描（KI_TELEGRAM_HANDOFF=1）开启条件（硬性，写死）：
   ① 通道灰度稳定 ≥2–3 天且熔断零误开；② 人工抽检 20 条
   materialized 正文，可用比例达标（阈值由用户定）；
   ③ V2.1-4.1 积压清扫完成、open review 清零。
   首次开启仍配小 max_calls。

## V2.1-3 TG7 载荷扩充（user_version 1→2 一次装齐）

原范围（通知/digest 持久化、幂等迁移 + maintenance lock、H2 WxPusher）
保留，新增：

1. **附件分类学**：kind 按 mime/扩展名细分——`video`（.mp4/.mov/…，
   走既有媒体转写腿，已实测）、`pdf`（application/pdf 与文档扩展名）、
   其余 `skipped_unsupported`；迁移时为存量误标数据回填。
2. **下载内容寻址去重**：跨源相同 SHA-256 不重复下载。
3. **open 窗口墙钟关闭**：reconcile 时按 `first_message_at + 300s`
   关窗，安静源的 item 不再永悬。
4. **session 互斥**：adapter 对 `database is locked` 加退避重试；
   LaunchAgent 化后明确 session 所有权归 watcher，CLI 侧排队/让出。
5. **digest 增补两行**：待审 REVIEW（item/原因/大小）与通道预算/熔断
   状态（H2 凭据交付后生效；实测依据：review #23 等待 1.5h 无人知晓）。

## V2.1-4 TG8 增补

1. **积压 curation 清扫**（自动扫描的硬前置）：tg_search 时代的
   ~227 条 materialized 以搜索关键词堆为主，逐条或按源处置为
   skipped/excluded，建立"未来自动扫描只会遇到干净语料"的基线。
2. Real Acceptance Matrix 增补真实路径：视频→本地转写→docchunk、
   通道开启下的分类准确率（已有 21 次调用零熔断的样本起点）、
   显式 handoff、14 源工况、LaunchAgent 重启自愈。
3. doctor / retention / crash-restart 原范围不变。

## V2.1-5 执行顺序（替代原阶段总览中尚未执行的部分）

```
P0（V2.1-1 watcher 持久化）
→ TG6 收尾（V2.1-2，等 #23 或更厚样本编译到 staged）
→ TG7（V2.1-3，迁移 + 通知，载荷一次装齐）
→ TG8（V2.1-4，清扫 + 矩阵 + 原范围）
```

人工环节不变：H2 WxPusher 凭据、H3 样本确认、自动扫描开启确认。
