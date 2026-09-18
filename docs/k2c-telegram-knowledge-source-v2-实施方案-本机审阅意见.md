# K2C-Telegram-Knowledge-Source-V2 实施方案 · 本机审阅意见

- 审阅日期：2026-09-18
- 审阅对象：《K2C Telegram Knowledge Source V2 实施方案》（DRAFT FOR REVIEW，会话交付稿，**尚未落盘** docs/）
- 设计输入复核：`docs/k2c-telegram-knowledge-source-v2-design.md` SHA-256 = `0d6e45de76785db2cf318a93261a6aabb31e2dc03993ce8693b134f7093e926c`，与方案引用值**逐字节一致**（2026-09-18 本机重算确认）
- 核对基准：`hg199074jin/knowledge-ingest` @ `4ca311e`（local main == origin/main）
- 审阅结论：**修复 4 项 Medium（M1–M4）后即可开工**——方案与冻结设计之间无产品决策冲突，技术事实引用全部属实；但存在 1 处冻结设计覆盖缺口（M1）、2 处冻结功能未排期（M2）、1 处 CI 策略缺失（M3），不补会在实施中途返工或 CI 变红

---

## 1. 逐 TG 事实核对

### TG0 — Canonical Freeze + Preflight

| 方案声称 | 本机事实 | 判定 |
|---|---|---|
| canonical 两文件 | `docs/k2c-telegram-knowledge-source-v2-design.md`（46,338 B）+ `docs/k2c-telegram-knowledge-source-v2-本机审阅意见.md` 均已在盘（未跟踪） | ✅ 已存在，走"只校验 SHA"分支 |
| 冻结 SHA | `0d6e45de…93e926c` 重算一致 | ✅ |
| target=k2c 20 测试 | 8+4+8=20，当前全绿 | ✅ |
| 全量基线 | **286 passed**（4ca311e），ruff 全绿 | ✅（TG0 应记录 286 为旧基线） |
| local==origin | 双 `4ca311e`，0/0 | ✅ |
| 备份分支保留至 TG6 | `backup/pre-rebase-k2c-target-f70a031`（→f70a031）在 | ✅ |

**F1（Medium，TG0 唯一问题）**：§2.2 推荐"若 V2 canonical 尚不存在：用 `git mv` 保留历史，再以冻结稿覆盖内容"——该分支一旦触发会把 `docs/k2c-telegram-knowledge-source-v1-design.md`（5edd25f 提交的 V1 冻结稿）从工作树**改名覆盖**，V1 文档将不再是独立历史文件，且 git 会把 V2 内容伪装成 V1 的演化。现实是 V2 canonical 已存在，根本不会走这个分支；建议直接删除 `git mv` 分支，改为一句话："V1 文件保留为历史冻结稿，原地不动、不再作为实施输入；V2 canonical 只校验 SHA"。

### TG1 — Source Handoff v2

| 方案声称 | 本机事实 | 判定 |
|---|---|---|
| `models.py` ProviderName 扩展点 | `models.py:40` `Literal["local","baidu","quark"]`、`:66` JobRequest.provider | ✅ |
| CLI choices | `cli.py:161-162` | ✅ |
| 完成门 `schema_version != 1` 即拒（须改为 `{1,2}`） | `cli.py:416` 现状确为 `!= 1` 即拒 | ✅ 判断准确且必要 |
| baidu 范围门仅对 baidu | `cli.py:456` `if provider == "baidu"`（`BAIDU_APP_PATH`） | ✅ 结构上已隔离，测试锁定即可 |
| cli route 无 provider 枚举分支 | `_cmd_route`（cli.py:472）及其路径零 provider 引用（动态取 `manifest.request.provider`） | ✅ |
| v1 fixtures | **只有 `baidu/quark-source-handoff.json`，无 local fixture** | ⚠️ 小注：v1 local 用例沿用现有测试内联构造即可，勿假设 fixture 存在 |
| doctor.py 列入"核对并修改" | `doctor.py:26` `CLOUD_SKILL_KEYS=("baidu","quark")`——telegram 不是 cloud skill，**预期零修改** | ⚠️ 小注：TG1 对 doctor.py 应是"verify-only"，防止实施 Agent 误改 |

版本策略（v1=旧三 provider 继续用，v2=telegram+provenance，版本门 `in {1,2}`，schema 3 fail-fast）与冻结 §13.5 一致，且与 `manifest_store.py:120-121` 已有的 job manifest v1/v2 并存先例同构。✅

### TG2 — State Core + Registry

- PRAGMA 三件套（WAL / foreign_keys / busy_timeout=5000）与冻结 §8.4 一致 ✅
- 表结构与主键（`tg_messages(source_id,message_id)`、`downloads(item_id,document_message_id)`）与冻结 §8.2 + 审阅修正 E3 一致 ✅
- `PRAGMA user_version` 从 1 起、未知高版本 fail-fast ✅
- maintenance lock 范围（doctor --fix / retention / migration / 批量 repair）与冻结 §8.4 一致，且仓库有现成 flock 风格（`cache.py:109-177`）可复用 ✅
- `require_orico` 警告的代码前提已在 TG6.6 落实（见下）✅

### TG3 — Client + Auth + Watcher

- optional dependency 策略与 `pyproject.toml` 现状吻合（无 optional-dependencies，hatchling 后端支持；telethon/pytest-asyncio 全环境未装）✅
- auth 人工环节 H1 与本机现实吻合（无 Telegram 客户端、无任何 session 文件）✅
- watcher 双路径（live + reconcile）先 commit SQLite 再下游，与冻结 §19.2 一致 ✅
- **M3（Medium，见 §2）**：CI 策略缺失。

### TG4 — Item Builder + Materialization

- 固定窗口（first-message anchored，非 rolling）与冻结 §9.1 一致 ✅
- 50 MiB = `50*1024*1024`，三边界测试（-1/exact/+1）✅
- ORICO fail-closed 四个检查点与"不可只读 `processing.require_orico`"——代码前提已核实：`config.py:31` 仅有定义、src 运行时零读取 ✅
- provider_guess `quark|baidu|unknown` 与冻结 §22 一致 ✅
- 小注：`message.md` 按冻结 §9.1 模板含固定标题行 `# Telegram Knowledge Item`；§6.3"只写原始正文"应理解为"不加 provenance/摘要"，不是去掉标题行。

### TG5 — Classification + Source AI Budget

- 规则优先顺序（TEXT 0 模型调用 / 疑似广告才 LLM / PDF 预筛后 Interest / CLOUD_LINK 默认 KEEP）与冻结 §10.4、§24.2（E1 修正后）一致 ✅
- 结构化输出契约 + 失败→REVIEW 三条与冻结 §21 一致 ✅
- Source AI Budget"状态写 Telegram SQLite、不写 JobManifest budget"——与冻结 §21.1"独立记账"一致，且与 `budget.py` 现状（target/host 级账本，`acquire/outcome`、breaker `consecutive_empty:3 / rate_limit:2`、DENY_REASONS）正交不冲突 ✅
- **E1（Minor 但值得改）**：§7.3 转述的 INCLUDE/EXCLUDE 清单比冻结稿**缩水**（丢"会计""企业管理""AI 工具""思维模型""商业模式"等条目）。方案 §0 已声明冻结稿是唯一输入，§7.3 应直接引用"冻结 §11.2/§11.3 原文"而非二次转述，防止实现者按缩略清单建分类体系。

### TG6 — E2E

- `source.json schema_version=2` 命名与现状一致（真实产物 `jobs/<id>/handoff/source.json` 即 handoff 文件）✅
- Handoff 幂等（`source_items.knowledge_ingest_job_id` 关联、retry/重启/replay 不产生第二个 Job）与冻结 §19.3 一致 ✅
- target=k2c 全量复用、不自动批准 Human Gate、"20 tests 全绿"回归线——与冻结 §15/§14 一致 ✅

### TG7 — Notification + LaunchAgent

- NotificationPort 字段、event_id 幂等、有限 retry、delivery log、digest 持久 cursor、双 LaunchAgent（watch=KeepAlive / digest=StartCalendarInterval）与冻结 §17 一致 ✅
- "不得复用 ki-resume 的 StartInterval=900 当 watcher"——与现状精确吻合（`watchdog.py:22` `START_INTERVAL_SECONDS = 900`）✅
- **M4（Medium，见 §2）**：notification delivery 状态是 TG7 新增表，但 §4.5 的 schema version 演进只在 TG2 定义了"从 1 开始"，TG7 没写迁移落点。

### TG8 — Doctor + Recovery + Acceptance

- doctor 检查清单 ⊇ 冻结 §19.4，且合理增补 notification 失败与 LaunchAgent 状态（冻结 §17.5"连续失败进入 doctor 可见状态"的落点）✅
- Retention cleanup 的 dry-run→候选数→maintenance lock→delete 流程与冻结 §18.2/§8.4 一致 ✅
- 验收矩阵 A–S 覆盖冻结 §26 全部大项——**除 M1 外**（见下）✅

## 2. 必须先修的 4 项 Medium

### M1 编辑/删除语义在方案中没有落点（冻结设计覆盖缺口）

冻结 §8.1 把"消息可能编辑/删除"列为 SQLite 事实层的存在理由之一，冻结 §20 给出完整语义：

- finalization 前 edit → 更新 Raw Event、重新生成 Item；
- handoff 后 edit → 记 `SOURCE_EDITED_AFTER_HANDOFF`、进 REVIEW/增量更新候选，**不反向改 Corpus**；
- delete → 记 `deleted_at` / `source_deleted=true`，不物理抹除。

但方案中：TG2 只建了含 `edited_at/deleted_at` 列的表（schema 在、行为无）；TG3 FakeTelegramClient 用例（§5.8）无 edit/delete；TG4/TG5 无重新生成与 REVIEW 候选逻辑；TG8 验收矩阵 A–S 无对应行。真实白名单群里编辑/删除是常态（冻结 §3.1 引 tg-archive 的核心场景），不补就是上线后第一批脏数据。

**建议补法**：TG3 TDD 增 `edited message` / `deleted message` 两个 fake 用例；TG4 增"finalized 前重生成、handoff 后只打标"两条；TG8 矩阵增两行（T. 编辑后未 handoff → Item 更新且无重复；U. 编辑发生在 handoff 后 → SOURCE_EDITED_AFTER_HANDOFF 进 REVIEW、Corpus 不变）。

### M2 `telegram review list/resolve`、DOWNLOAD_ONCE、`telegram status` 未排期

冻结 §16.3 的三个人工决定（KEEP/SKIP/**DOWNLOAD_ONCE**）和 §23 CLI 清单中的 `telegram review list / review resolve / telegram status`，方案没有任何 TG 认领。DOWNLOAD_ONCE 是">50 MiB 单次授权下载"的唯一合法入口，缺了它 TG4 的下载门控就闭环不了；review resolve 缺了它 REVIEW 积压永远只能靠 doctor 看不能清。

**建议补法**：`review list/resolve`（含 DOWNLOAD_ONCE→触发一次下载）放 TG5 末尾（决策落库之后）或独立小 TG；`telegram status` 并入 TG2（读 SQLite 汇总）或 TG8 doctor 前身。

### M3 CI 可选依赖测试策略缺失

CI（`.github/workflows/tests.yml`）两平台都是 `uv sync`（**不装 extras**）+ ruff + pytest + ubuntu coverage `--cov-fail-under=50`。TG3 之后 `telethon_adapter.py` 及其测试（FloodWait/RPCError 映射）一旦被 pytest 收集，CI 立刻 import error 变红；或反过来为了 CI 把 telethon 塞进核心依赖，违反冻结 §3.4。

**建议补法**（方案 §5.2/§5.8 补三句话）：① `telethon_adapter.py` 对 telethon 延迟导入（模块导入不触发）；② adapter 专属测试用 `pytest.importorskip("telethon")`，CI 无 telethon 时自动跳过；③ 端口/业务层测试一律走 FakeTelegramClient（不 import telethon），保证 CI 全绿。pytest-asyncio 进 dev group（`uv sync` 默认装 dev group，CI 可用）。

### M4 TG7 新表的 schema 迁移落点未写

notification delivery 状态（§9.4）是 TG7 往 `state.db` 加新表，而 §4.5 只说"user_version 从 1 开始、不建 migration framework"。按现写法，TG7 要么偷偷改初始化 DDL（对已存在的 V2 数据库不生效），要么无据可依。

**建议补法**：TG7 明确"新表用幂等 DDL（CREATE TABLE IF NOT EXISTS）+ `user_version` 1→2 升级步骤，升级持 maintenance lock"，复用 §4.8 已有原则即可，不需要 migration framework。

## 3. Minor 备注（不阻塞，实施时留意）

- **E1** §7.3 清单转述缩水（会计/企业管理/AI 工具/思维模型/商业模式等丢失）——改为引用冻结 §11.2/§11.3 原文。
- **E2** §11"只有 H1/H2 两个用户环节"低估了 TG6/TG8 真实验收的现实依赖：冻结 §27 禁止自动发消息造数据，真实 TEXT 用例需要用户允许的测试来源里**有人发新消息**（等待自然流量或用户手动发）。建议补 H3"真实来源准入与真实消息配合（TG6/TG8）"。
- **E3** `.gitignore` 增补（`*.session`、`*.session-journal`、`credentials.env`、`telegram-secrets*`、`wxpusher.env`）在冻结 §5.3 是硬要求，但方案没有任何 TG 把它列入修改范围——应明确挂 TG3（auth 之前必须生效）。
- **E4** `IMPORTANT_CAPABILITY` 通知类型在 §9.2 定义了，但没有指定生产者——建议明确"由 TG6 staged 事件发出"或显式标注 V2 先占类型、生产者后接，避免实现时悬空。
- **E5** TG1 对 `doctor.py` 应标注 verify-only（telegram 不进 `CLOUD_SKILL_KEYS`，预期零修改）；v1 local 用例无现成 fixture（tests/fixtures/ 仅 baidu/quark），沿用现有内联构造。

## 4. 明确确认通过的部分

- 冻结边界转述（§0 的 20 条）与冻结稿 §29 逐条一致，无走样；
- "禁止新增"清单与冻结 §25/§28 一致；
- 每 TG 的 TDD 用例设计（5-min 边界 0/1/299/300/301 秒、50 MiB 三边界、budget replay 幂等、digest 补算不重复等）质量高，与仓库 TDD 文化匹配；
- §13 执行模板（重读冻结章节→红灯→最小实现→回归→diff 自审→普通 push）与用户既有 SDD/TDD/worktree 纪律一致；
- §12 commit 粒度与"TG0 后保留备份分支至 TG6"符合当前仓库状态（备份分支在）；
- 所有引用的文件:行号、测试数（20/286）、SHA、分支名、目录现状经本机复核**全部属实**。

## 5. 结论

方案骨架（TG0–TG8 顺序、先 Handoff 后 State 再 Client 的依赖排序、冻结边界清单、两个人工环节、最终成功定义）**成立且质量高**，与本机 `4ca311e` 事实零冲突。按 M1–M4 修订（均为方案文本增补，不动冻结设计）后即可进入 TG0——TG0 实际上已完成大半：canonical 两文件已在盘、SHA 已核对、基线 286 全绿，只差一次 commit。

建议流程：用户按本报告修订方案 → 方案落盘 `docs/k2c-telegram-knowledge-source-v2-实施方案.md` → 与审阅意见一并 commit → 开工 TG0。

---

## 6. 终审记录（2026-09-18 · REVISED FOR FINAL REVIEW）

审阅对象：修订版实施方案（状态 REVISED FOR FINAL REVIEW，会话交付稿）。核对基准不变：`4ca311e`；冻结设计 SHA 复核仍为 `0d6e45de…93e926c`，工作区无漂移。

### 6.1 七项修订逐项核对 —— 全部真实落地

| 项 | 修订版落点 | 判定 |
|---|---|---|
| **M1** 编辑/删除 | §5.3 `TelegramEvent` 强制 NEW/EDIT/DELETE（DELETE 至少携带 `(source_id, message_id)` 身份字段，符合 Telegram MessageDeleted 现实）；§5.7 三类 update 的 Raw Event 处理（EDIT/DELETE 不在 TG3 决定下游）；§6.4 A/B/C/D 四态语义（未 finalized / finalized 未 handoff / 已 handoff / DELETE）；§6.10 四条 TDD；§10.8 矩阵 T/U/V；§10.10 evidence | ✅ 严格沿用冻结 §20 |
| **M2** REVIEW 闭环 | §4.1/§4.2/§4.6 TG2 认领 `telegram status`（只读汇总）、`review list`（仅 unresolved）、`review resolve`（同 decision 幂等 no-op、异 decision 明确拒绝）；DOWNLOAD_ONCE 定义为 **item 级一次人工授权**而非单次网络 attempt，TG2 只落授权、TG4 §6.7 消费（同授权内 retry/restart 不重复授权/下载/Job，原子消费，replay 不二次下载）；§4.4 授权事实源唯一（`decision_consumed_at` 或 `processing_status` 二选一）；§10.8 矩阵 W | ✅ |
| **M3** CI/可选依赖 | §5.2 无 extra CI 契约五条（核心禁顶层 import、adapter 延迟导入、adapter 测试 importorskip、业务层全 Fake、无 extra 时 import/collection/非 adapter 测试全绿）+ 最小契约测试；§5.8/§10.9 回归纳入 | ✅ 与 CI 现状（`uv sync` 不装 extras）精确对应 |
| **M4** SQLite 迁移 | §4.5 TG2 固定 `user_version=1` 并预留 1→2；§9.4 迁移七规则（maintenance lock、DDL 幂等、旧数据不丢、fresh DB 直接 latest(2) 与 0→1→2 等价、>2 fail-fast、失败不提前写版本）；§9.10/§9.11 TDD 与验收 | ✅ |
| **F1** TG0 canonical | §2.2 重写：V1 原地保留、不 git mv、不覆盖、不建第二副本；superseded 指针如需则独立文档动作 | ✅ |
| **E1** 兴趣清单 | §7.3 唯一规范来源改为冻结 §11.2/§11.3 原文，policy 可版本化 + `policy_version` 入审计（顺带强化冻结 §21 要求）；TDD 样本明示"非完整枚举"；改兴趣类别必须走冻结设计显式版本 | ✅ |
| **E2** 人工环节 | §11 扩为 H1/H2/H3；§8.8 H3-A、§10.7 H3-B；"真实样本未出现必须如实标记 real sample pending，不得伪造通过" | ✅ 与技能评测纪律（禁改题后称 100%）同源 |

### 6.2 方案级细化确认（合理扩展，不违背冻结）

- **§6.4 B 态**（finalized 未 handoff 的中间态）是冻结 §20.1 两态（未 finalized / 已 handoff）之间的合理细化：同 item_id 重建、不建 KI Job，与冻结 §19.3 幂等一致；
- **§10.4**"已删除源消息保留审计事实、retention 不得误删"是对冻结 §18/§20 交叉处的正确封堵；
- **§9.4** fresh-DB 等价性要求（latest(2) ≡ 0→1→2）是迁移实现的关键防坑条目。

### 6.3 遗留与终审新增（全部为单行级文本增补，不动架构与 TG 排序）

- **E3（建议冻结前补，唯一偏安全项）**：`.gitignore` 增补（`*.session`、`*.session-journal`、`credentials.env`、`telegram-secrets*`、`wxpusher.env`）是冻结 §5.3 硬要求，修订版仍只在校验侧出现（§5.5"gitignore 命中"），没有任何 TG 认领编辑动作。建议 TG3 §5.2 修改范围补 `​.gitignore` 一项。
- **E4**：`IMPORTANT_CAPABILITY`（§9.2）仍无生产者。建议一句话二选一：生产者为 TG6 staged 事件；或显式标注"V2 预留类型，生产者后接"。
- **E5**：TG1 §3.2 的 `doctor.py` 建议标注 **verify-only**（telegram 不进 `CLOUD_SKILL_KEYS`，预期零修改）；v1 local 用例无现成 fixture（tests/fixtures/ 仅 baidu/quark），沿用现有测试内联构造即可。
- **E6（终审新发现，纯文本漂移）**：§12"建议最终历史"与各 TG 章节的 commit 建议已不同步——TG0（§2.6 `…V2 design` vs §12 `…canonical inputs`）、TG2（§4.11 `…registry and review CLI` vs §12 `…and registry`）、TG4（§6.12 `…edit semantics and…` vs §12 无 edit）、TG7（§9.12 `…database migration and…` vs §12 无 migration）、TG8（§10.11 `…edit and real acceptance` vs §12 无 edit）。建议 §12 标注"以各 TG 章节为准"或同步五处。另建议 header"审阅输入"补列本报告路径（`docs/k2c-telegram-knowledge-source-v2-实施方案-本机审阅意见.md`）。

### 6.4 终审结论

七项修订**全部落地且无一处走样**，未发现新引入的实质问题；E3–E6 均为单行文本级事项。处置建议二选一：

- **A（推荐）**：补入 E3–E6 四处单行修订后标 **FINAL**，随即落盘 commit、开工 TG0；
- **B**：现版即放行，E3 作为硬约束挂入 TG3（H1 auth 前必须生效），E4–E6 由实施 Agent 在对应 TG 内按本报告处置。

两条路的差别只在文档整洁度；无论哪条，**TG0 已具备开工条件**（canonical 在盘、SHA 核对通过、基线 286 全绿、local==origin）。
