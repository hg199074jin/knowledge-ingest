# Handoff Contracts — 总控与各 Skill 的稳定接口

> Python CLI 与上层 Agent Skill 之间只通过这些 contract 通信；
> 任何一方内部实现变化都不应破坏此处定义的字段语义。

## 1. Source Handoff（云盘 Skill → knowledge-ingest）

canonical 文件：`jobs/<job-id>/handoff/source.json`（schema 见
`schemas/source-handoff.example.json`）。

```json
{
  "schema_version": 1,
  "provider": "quark | baidu | local",
  "remote": {
    "id": "provider-id-or-null",
    "path": "/审计课程/融资担保",
    "name": "融资担保",
    "size_bytes": null,
    "mtime": null
  },
  "local_path": "/Volumes/ORICO/KnowledgePipeline/jobs/<job>/source/融资担保",
  "download_completed": true,
  "source_notes": []
}
```

规则：

- Local provider：`remote=null`，`local_path` 指向用户原路径，**不复制**。
- 云端 provider：只有 Skill 明确下载完成、本地文件存在且大小稳定后才生成；
  `download_completed=false` 时总控拒绝 register。
- 百度：`remote.path` **必须写应用目录全路径**（`/apps/bdpan/...` 或
  `apps/bdpan/...`），register 用正则边界校验（`apps/bdpan-evil` 等仿冒前缀
  与裸相对路径一律拒绝），否则 `BLOCKED: baidu_scope_limited`。
- 不得包含任何 Token/Cookie/授权码。

## 2. Target Handoff（knowledge-ingest → 蒸馏 Skill）

canonical 文件：`jobs/<job-id>/handoff/target-<target>.yaml`
（schema 见 `schemas/target-handoff.example.yaml`）。

```yaml
job_id: <job-id>
target: cangjie | personal | family_router
corpus_path: /Volumes/ORICO/LongDocCorpus/<corpus-id>   # 已 verify PASS
source:
  provider: quark
  title: 融资担保课程
  author_or_speaker: null    # 缺失保持 null，禁止猜测
  publication_date: null
first_pilot: true            # cangjie：是否首次试点
depth: null                  # personal：A/B/C；null=原 Skill 默认（实测默认 C）
obsidian_vault: /Volumes/ORICO/Obsidian/Skill_Library   # personal 专用
input_manifest: <job-dir>/handoff/cangjie-output-manifest.json   # family_router 专用（cangjie COMPLETED 才指向，否则 null）
budget:                      # family_router 专用（规格 9，两阶段 acquire/outcome）
  mode: balanced             # economy | balanced | full
  max_external_calls: 20
  max_retries_per_case: 1
  breaker: {consecutive_empty: 3, consecutive_rate_limit: 2}
purpose: 用户原始要求
provenance_manifest: /Volumes/ORICO/KnowledgePipeline/jobs/<job>/job.yaml
output_preference: auto
```

## 3. Cangjie 消费规则（Agent 层）

1. 输入必须是 verify PASS 的 Corpus；`corpus_path` 传给 Cangjie 后，要求其
   **通过 longdoc-router 按 Batch 阅读**：
   - 先读 `manifest.json` / `index.jsonl`；
   - 按 B0001 → B0002 顺序消费 Reading Batches；
   - `overlap_atomic_ids` 只是上下文桥，不算新材料；
   - 全部 new_atomic_ids 读完再进入跨文档蒸馏。
2. 元信息（书名/作者/出版年 或 标题/讲者/发布时间）来自 handoff；缺失即 null。
3. 运行 cwd 固定：`/Volumes/ORICO/KnowledgePipeline/distill/<job-id>/cangjie/`
   （其产物落 cwd 下 `books/<slug>/`）。
4. 完成后：`knowledge-ingest target complete JOB --target cangjie --output-path ...`
   并把 `books/<slug>/PIPELINE_STATE.md` 记入 `cangjie.pipeline_state`。
5. 确认点映射见 `references/target-gates.md`。

## 4. Personal 消费规则（Agent 层）

1. 复用同一 Corpus（`combined.md` / `atomic/*.md` 满足其 Markdown-only 输入要求），
   **不**重新读取原 PDF/视频。
2. `obsidian_vault` 显式传本机路径（Skill 内部写死 Windows `E:\`，必须 override）。
3. 运行 cwd 固定：`/Volumes/ORICO/KnowledgePipeline/distill/<job-id>/personal/`。
4. depth=null 时遵循原 Skill 默认（C 档）；用户指定优先。
5. workflow-states 命名门映射见 `references/target-gates.md`；安装必须用户明示授权。
6. 完成后：`knowledge-ingest target complete JOB --target personal --output-path ...`。

## 5. 依赖与顺序（依赖 ≠ 排序）

依赖只决定"能不能启动"（`depends_on` 全 COMPLETED 才 READY）；
**声明顺序决定执行顺序**（一次只允许一个 RUNNING/WAITING_USER target）：

| target | depends_on | 说明 |
|---|---|---|
| `cangjie` | `[]` | 独立可启动 |
| `family_router` | `[cangjie]` | cangjie COMPLETED 后才 READY |
| `personal` | `[]` | 独立可启动；与 cangjie 同时请求时按声明顺序先 cangjie 后 personal |

`CORPUS_READY → 按声明顺序逐 target → COMPLETED / PARTIAL / FAILED`；
不并行，不重复 docchunk。

## 6. Family Router 契约（knowledge-ingest → family-router-builder）

target handoff：`jobs/<job-id>/handoff/target-family_router.yaml`
（`distill prepare --target family_router` 自动生成），在通用字段之上追加：

```yaml
input_manifest: /Volumes/ORICO/KnowledgePipeline/jobs/<job>/handoff/cangjie-output-manifest.json
budget:
  mode: balanced
  max_external_calls: 20
  max_retries_per_case: 1
  breaker:
    consecutive_empty: 3
    consecutive_rate_limit: 2
```

规则：

1. **input_manifest** 引用 cangjie 的 output manifest
   （`cangjie-output-manifest.json`，cangjie COMPLETED 时由
   `target complete` 事务生成并登记到 `targets.cangjie.output_manifest`）；
   cangjie 未完成时写 `null`——router 不得自行猜测输入，宁可 BLOCKED。
2. **budget 是协作式预算声明**（规格 9/16）：router 每次外部调用前
   `budget acquire`（`request_id` 幂等，重放不重复计数），调用后
   `budget outcome --result success|empty|rate_limit`；quota/breaker/case-retry
   任一触发 → target BLOCKED。改上限用 `budget amend`（**改 ≠ 恢复**），
   恢复必须显式 `target resume`。KI 不拦截宿主任意其他外部调用——绕过
   acquire 的调用不受预算保护、不进审计（诚实边界见 SKILL.md）。
3. 运行 cwd：`<pipeline_root>/distill/<job>/family_router/`；
   `evidence_root = <pipeline_root>/distill/<job>/family_router/evidence/`
   （逐案例证据落这里；`target checkpoint --evidence` 登记进 manifest）。
4. **ROUTER_BUILD_STATE.md 约定**：router 自身断点文件放 cwd 根，记录
   已完成 case 与当前 phase；中断恢复时先读它 + manifest 的
   `checkpoint_path/evidence_dir`，再续跑，绝不重头跑已完成 case。
5. 确认门 4 门顺序（necessity_gate → cost_budget_confirmed →
   acceptance_report_reviewed → review_disposition_reviewed）与预授权语义见
   `references/target-gates.md`；完成后
   `target complete JOB --target family_router --output-path ...` 登记。
