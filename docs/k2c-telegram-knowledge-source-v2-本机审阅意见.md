# K2C-Telegram-Knowledge-Source-V2 设计文档 · 本机审阅意见

- 审阅日期：2026-09-18
- 审阅对象：《K2C Telegram Knowledge Source V2 设计文档》（DRAFT FOR REVIEW，会话交付稿）
- 审阅重点（用户指定）：**新补的工程契约是否与本机实际实现冲突**
- 核对基准：`hg199074jin/knowledge-ingest` @ `4ca311e`（local main == origin/main，2026-09-18 推送后核验）
- 审阅结论：**通过（PASS）** —— 0 处实质冲突；3 处清单补全/澄清（F1–F3）+ 3 处编辑性修正（E1–E3），全部已并入冻结稿并在其"V1 → V2 修订记录"留痕
- 冻结产物：`docs/k2c-telegram-knowledge-source-v2-design.md`
- 冻结稿 SHA-256：`0d6e45de76785db2cf318a93261a6aabb31e2dc03993ce8693b134f7093e926c`

---

## 1. 六类新契约逐项核对

### 1.1 §15 / §30 `target=k2c` 回归契约与 Preflight —— 事实全部为真

| 文档声称 | 本机证据 |
|---|---|
| main 已注册 k2c Target | `src/knowledge_ingest/targets.py:187` `name="k2c"`（TargetRuntime 完整：invoke_k2c、depends_on=()、专属 output_manifest、handoff_extra） |
| corpus identity handoff | `targets.py:132-154` `_read_corpus_identity`、`:157-183` `_k2c_handoff_extra` |
| k2c-output-manifest | `src/knowledge_ingest/k2c_output_manifest.py`（冻结状态映射，completed/needs_review_nonblocking 放行、blocking→gate、paused_budget/failed 拒绝） |
| SKILL.md 入口语义 | `SKILL.md:40/78/86/101`（`--target cangjie\|personal\|family_router\|k2c` 等） |
| 相关测试 | `tests/unit/test_target_k2c.py`(8) + `test_k2c_orchestration.py`(4) + `test_k2c_output_manifest.py`(8) = 20 个，当前全绿 |
| 真实产物 | `jobs/20260914-155208-local-m2-source-b3a6a1/handoff/target-k2c.yaml`（真实 corpus `document-set-9c31a3a1da96`） |
| Preflight#1 local==origin | 2026-09-18 push 后双双 `4ca311e`，ahead/behind 0/0 |

V1 版此节"target=k2c 尚未落入 main"的过时表述已正确修复，且修复方式（改为回归契约而非删除）比 V1 审阅建议更进一步：回归基线语义（§26.I"既有 K2C target 回归测试不得退化"）与本仓库 20 个既有 K2C 测试可直接对接。

### 1.2 §13.4 / §13.5 Source Handoff v2 与迁移影响面 —— 清单实质准确，补 2 处遗漏（F1/F2）、1 处澄清（F3）

全量扫描 `provider` 触点（src + templates + schemas + fixtures）：

| 代码触点 | 现状 | 文档清单 |
|---|---|---|
| `models.py:40` `ProviderName = Literal["local","baidu","quark"]` | 硬编码 | ✅ 已列 |
| `models.py:66` `JobRequest.provider` | 引用上者 | ✅（"Pydantic Literal"覆盖） |
| `cli.py:161-162` `job create --provider` choices | 硬编码 | ✅ 已列 |
| `cli.py:414-425` 完成门：`:416` `schema_version != 1 → invalid_handoff_schema`；`:420-421` `provider_mismatch` | 硬校验 | ✅ 已列（"unknown-key / schema-version 行为"） |
| `cli.py:456-463` baidu 专属 `/apps/bdpan` 范围门（`BAIDU_APP_PATH`） | provider 条件分支 | ✅（"source register validation"覆盖，冻结稿已显式点名） |
| `doctor.py:26` `CLOUD_SKILL_KEYS = ("baidu","quark")` | 硬编码 | ✅ 已列 |
| `templates/job.yaml:9` `provider: local  # local \| baidu \| quark` | 注释枚举 | ❌ **遗漏 → F1，冻结稿已补** |
| `schemas/source-handoff.example.json:3` `"provider": "quark"` | 示例文件 | ❌ **未显式 → F2，冻结稿已补** |
| `tests/fixtures/{baidu,quark}-source-handoff.json` | v1 fixtures | ✅ 已列 |
| `report.py:184/208` provider 渲染 | **动态** `dict.get`，无枚举 | ✅ 已列（"report rendering"） |
| `manifest_store.py:168` job_id 内嵌 `{provider}` 字符串 | 动态插值 | 无需改动（自动兼容 telegram） |
| 文档所列"Source Router" | 仓库无 `routing.py`；实为 `cli route` 文件路由阶段，provider 引用动态 | ⚠️ **名称易误导 → F3，冻结稿已澄清** |

关键确认：完成门 `cli.py:416` 的 `schema_version != 1` 即拒，**正是 §13.5 所禁止的"看似兼容、实际拒绝"行为的现存实例**——v2 handoff（schema_version: 2）在当前代码下必被拒。文档把它列为必改项是正确且必要的。另 `manifest_store.py:120-121` 已有 job manifest v1→v2 并存迁移先例，handoff v1/v2 并存在本仓库有成熟模式可循。handoff 为裸 dict 读取（无 pydantic 模型约束），旧 reader 对未知键 `provenance` 天然容忍，"向后兼容"唯一阻塞点就是版本门——文档的判断与代码事实完全吻合。

### 1.3 §8.4 SQLite 并发契约 —— greenfield，无冲突

- `rg sqlite3 src/` 零命中：仓库现有代码无任何 SQLite 使用，§8.4 是纯新增契约，无既有实现可冲突。
- 仓库现有并发纪律为 flock（`cache.py:109-177` 索引锁、A3 事务性 mutate），§8.4 的"maintenance lock"与该纪律同风格，实施时可直接复用 `flock` 工具函数，不引入第二套锁抽象。
- `busy_timeout=5000ms`、单写队列、短事务均为标准 SQLite 实践，与 macOS LaunchAgent 常驻形态兼容。

### 1.4 §5.2 `require_orico` 死配置警告 —— 警告有据，与代码事实精确吻合

核实：`require_orico` 在 `config.py:31` 定义（默认 True），全部其余引用都在测试断言（7 个测试文件），**src 运行时零读取**；实际 ORICO 阻断靠 `doctor.py:122-127` 的 `orico_mounted` FAIL 检查。§5.2 新增段落（"不得只依赖配置值，watcher 必须主动确认数据根可用"）正是对这一假保护漏洞的正确封堵——这是 V2 相对 V1 最有价值的防坑补充之一。

### 1.5 §10.4 / §21.1 模型策略与 Source AI Budget —— 与现有 Budget Guard 结构吻合

- `budget.py` 现状：`acquire/outcome` 语义（`:4`）、quota（target 级）、breaker（target+host 级，`consecutive_empty: 3` / `consecutive_rate_limit: 2`，`DENY_REASONS = budget_exhausted / breaker_open / case_retry_exceeded`）。
- §21.1 声明"不复用 Target 业务账本，但复用 acquire/outcome/quota/retry limit/breaker 设计原则"——与 budget.py 的实际作用域（target/host 级）一致：Source 级确需独立账本，文档没有越权宣称直接复用账本。✅
- "模型调用通道优先复用本机已有统一 worker/router 能力；设计层不写死 provider/model"——本机存在 claude-worker-router 体系，且符合全局 AGENTS.md §13"provider 手动切换、不自动 fallback"的纪律。✅

### 1.6 §17 Notification Runtime 与 §23 双 LaunchAgent —— greenfield，与现状无冲突

- 本机/仓库 WxPusher 与 notifier 实现为零（此前已全盘核查），§17 作为"明确认领的 Integration 子系统"（而非顺手实现）定位准确。
- §23"不得用现有 15 分钟周期性 ki-resume LaunchAgent 替代"——与现状精确吻合：`com.sandro.ki-resume` 为 `RunAtLoad + StartInterval=900`、无 KeepAlive，确实撑不起常驻 watcher；watch（KeepAlive）与 digest（StartCalendarInterval 08/12/20）分离是正确的新增形态。
- §17.5 持久 cursor 补算语义消化了 launchd 非精确触发的现实，避免"错过的时刻=数据丢失"的隐含 bug。

### 1.7 §3.4 / §7.3 可选依赖与首次认证 —— 与本机环境精确吻合

- `pyproject.toml` 无 `optional-dependencies`（将新增，hatchling/uv 支持无障碍）；telethon、pytest-asyncio 全环境未安装（uv.lock 零命中）——§3.4 把二者列为"将补充"准确。
- 本机无 Telegram 桌面客户端、无任何 `.session` 文件（此前 `ls /Applications` + `mdfind` 核实），§7.3"首次 auth 为交互式显式运维步骤"是必须写明的现实，且"验证码/二步验证密码不落盘"与 §5.3 凭据纪律闭环。
- Telethon PyPI 最新 1.43.2（2026-04-20 发版）可安装；§3.3 的 GitHub 归档（2026-02-21）与 Codeberg 迁移声明此前已核实为真。

### 1.8 §11.2 / §11.3 兴趣清单 —— 与本机画像对齐

INCLUDE 补入民宿/短租/OTA/本地生活、自媒体/内容运营/抖音/直播电商/带货、企业分析/投资纪律/长期投资方法/风险管理，覆盖本机 minsu-\*（15 个技能）、daihuo-\* 系列、jingyun-stock-discipline 等真实工作流；EXCLUDE 收窄到"个股/短线/行情预测"并对投资认知类内容加 INCLUDE/REVIEW 正例保护，消除 V1"见股票词即排除"的误杀面。§29 冻结清单第 10/11 条与 §11.2/§11.3 一致。

## 2. 审阅发现（已全部并入冻结稿）

### 清单补全 / 澄清

- **F1** §13.5 迁移面遗漏 `templates/job.yaml:9`（provider 注释枚举）——已补入冻结稿清单。
- **F2** §13.5 未显式点名 `schemas/source-handoff.example.json`（现值 provider: quark，是 v2 schema 示例的自然改造点）——已补入。
- **F3** §13.5"Source Router"在仓库中无对应文件（无 routing.py），实指 `cli route` 文件路由阶段，且该阶段 provider 引用为动态（`manifest.request.provider`），无需枚举改动、仅需补 telegram 用例——冻结稿已改为准确表述。

### 编辑性修正

- **E1** §24.2 PDF 流程写"→ Noise Filter → Interest Policy"，与 §10.4"PDF 直进 Interest Classifier"存在措辞冲突——冻结稿改为"规则级 Noise 预筛（明确广告直接 SKIP，不调模型）"，两节对齐且强调不产生模型调用。
- **E2** §26.H"Digest 准时支持三个窗口"与 §17.5"错过时刻按 cursor 补算"语义未接续——冻结稿补"错过的 launchd 时刻按 §17.5 持久 cursor 补算，不算数据丢失"。
- **E3** §8.2 `downloads` 表未声明主键，重试场景下可能产生重复行——冻结稿补 `PRIMARY KEY (item_id, document_message_id)`（`attempts` 列承担重试计数）。

## 3. 明确不构成冲突、留待实施方案的事项

- `downloads` 下载并发与 `watcher 单写队列` 的具体实现（asyncio queue vs 串行化）——实施细节。
- WxPusher 发送测试方案（Preflight#9）：真实发送需要真实 app 凭据，属环境准备而非设计缺口。
- `telegram auth` 交互式登录（Preflight#6）需用户在场配合输入验证码/二步验证密码——运维步骤，方案中应排期为显式人工环节。
- 既有运维异常（如 ki-resume LaunchAgent 上次退出码 78）——§30 已正确划出"Preflight 单独处理、不进 Telegram 状态机"。

## 4. 结论

V2 的六类新契约（Handoff v2 迁移面、SQLite 并发、Notification Runtime、Source AI Budget、target=k2c 回归契约、规则优先模型策略）经逐项与本机 `4ca311e` 代码核对，**无一处与实际实现冲突**；3 处清单遗漏/命名误导与 3 处节间措辞不一致均属编辑级，已修正并留痕。§15/§30 的事实声明与 2026-09-18 仓库状态完全一致。

**审阅通过，V2 冻结生效**：`docs/k2c-telegram-knowledge-source-v2-design.md`（状态 FROZEN，SHA-256 见文首）。该冻结稿可作为 Telegram Source V2 正式实施方案的唯一设计输入。
