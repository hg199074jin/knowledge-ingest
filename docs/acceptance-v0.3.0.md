# knowledge-ingest v0.3.0 真实数据验收记录（Part E，darwin 协议验收层）

> 验收时间：2026-09-13。分支 `evolve/v0.3.0`，head `0d73590`。
> 验收对象：真实源 `baidu-9y`（民宿合集9y，243 文件 / 6.6 GB，含 GBK `目录.txt` 与 `目录.bat`）
> 与真实 COMPLETED Job `20260912-163657-baidu-9y`（v1 schema，cangjie+personal 双 target）。
> 全部项目走真实 CLI（`uv run knowledge-ingest --config config.example.yaml …`）。
> scratch 目录：`/Volumes/ORICO/KnowledgePipeline/tmp/acceptance-v03/`（保留作审计痕迹）。
> 真实源目录全程只读；真实生产 Job 仅做 E5 迁移（迁移前原文件字节级备份为
> `job.yaml.bak-v1`），E9/E11/E13/E14 全部在 scratch 副本上实验，生产 Job 未被重排。

## 结果总览

| 项 | 内容 | 结果 |
|---|---|---|
| E1 | 三段路由（discovered/excluded/effective 三桶保序） | ✅ PASS |
| E2 | 编码预检（GBK 拦截 / UTF-8 放行 / 全文件扫描 / BOM 证明 / .markdown） | ✅ PASS |
| E3 | 240 文件二次转写（transcript cache 全复用，两轮） | ✅ PASS |
| E4 | corpus 复用（docchunk.reused=true + verify PASS） | ✅ PASS |
| E5 | 真实 COMPLETED manifest V1→V2 迁移 | ✅ PASS |
| E6 | watchdog 实装（install/status/幂等/launchctl/doctor） | ✅ PASS |
| E7 | Target 顺序 round-trip（CLI create + load/save） | ✅ PASS |
| E8 | V1→V2 保持 precedence（COMPLETED 语义不变） | ✅ PASS |
| E9 | COMPLETED Job amend（scratch 副本方案） | ✅ PASS |
| E10 | output-manifest 决定性与字段完整性 | ⚠️ PASS（1 项缺陷如实记录，见 D1） |
| E11 | budget 熔断（quota/breaker/case-retry/幂等/冲突/amend+resume） | ✅ PASS |
| E12 | 审计报告渲染（运行审计段 + unknown 标注） | ✅ PASS |
| E13 | PARTIAL 聚合（单测 + scratch CLI） | ✅ PASS |
| E14 | 依赖传播（SKIPPED 链式） | ✅ PASS |

收尾：`uv run pytest -o addopts= -q` → **261 passed**（验收前基线同为 261 passed）。
`doctor --config config.example.yaml` → 16 checks，0 FAIL。

## 真实数据与 Job 清单（全部保留，不删除）

```text
真实源（只读）  /Volumes/ORICO/KnowledgePipeline/jobs/20260912-163657-baidu-9y/source
               243 文件 = 240 mp4 + 目录.txt(GBK) + 目录.utf8.txt(UTF-8) + 目录.bat
               注：真实 Job 当年 route 时为 242 文件（documents=1 只含 目录.utf8.txt，
               登记指纹 sha256:fc597fb3…）；其后 目录.txt(GBK) 被放回源目录成为
               243 文件，本次 E1 重新实测指纹 sha256:0a7f9254…（差异全部来自该
               文件，非源内容变动——240 个 mp4 与 目录.utf8.txt 未变）。
scratch 源     tmp/acceptance-v03/source-9y（240 mp4 硬链接零拷贝 + iconv GBK→UTF-8
               的 目录.txt + 原样 目录.utf8.txt，共 242 文件；.bat 不入 scratch）
fixtures       tmp/acceptance-v03/fixture-{3mb-ascii-gbk-tail, utf32le-bom, gbk-markdown}
E1/E2 job      20260913-111938-local-source-c43da7（source 指向真实源目录，只读扫描）
E3 job A/B     20260913-112149-local-source-9y-e3d5fd / 20260913-112436-local-source-9y-47e9ff
E2 fixture jobs 20260913-112230-local-fixture-3mb-ascii-gbk-tail-158200
               20260913-112241-local-fixture-utf32le-bom-7a30f8
               20260913-112242-local-fixture-gbk-markdown-3f8a15
E7 job         20260913-112604-local-acceptance-e7-order-roundtrip-611904
E9/E11/E14 job acceptance-e9-test（真实 Job 的 scratch 副本，job_id 已改写）
E13 job        acceptance-e13-test（同上）
备份           tmp/acceptance-v03/job-20260912-163657-baidu-9y.v1-original.yaml
               tmp/acceptance-v03/com.sandro.ki-resume.plist.pre-e6.bak（E6 前旧 plist）
               tmp/acceptance-v03/ki-resume.home-local.pre-e6.bak（E6 前 ~/.local/bin 旧脚本）
```

## E1：三段路由 ✅

```text
Job    20260913-111938-local-source-c43da7（--provider local，source 指向真实源目录）
流程   job create → source init --local-path（指纹 sha256:0a7f9254…）
       → source register → route
第一次 route（无 --exclude）exit=1：
  unsupported (exclude explicitly to continue): …/城市民宿平台运营，100%流量拿满分/目录.bat
  BLOCKED: unsupported_source
第二次 route --exclude <目录.bat 全路径> exit=1（转 E2 编码拦截），routing 落数：
  documents  discovered=2  excluded=0  effective=2（目录.txt + 目录.utf8.txt）
  media      discovered=240 excluded=0  effective=240
  unsupported discovered=1  excluded=1  effective=0（目录.bat 被排除）
保序断言（程序化）：
  - 每桶 effective == discovered 去掉 excluded 后按序子集：三桶均 True
  - 每桶 discovered 顺序 == sorted(rglob) 扫描顺序：三桶均 True（2/240/1）
```

## E2：编码预检 ✅（冻结规格 1/14）

```text
① GBK 目录.txt（真实源，同 E1 job --exclude 目录.bat 后 route）：
   BLOCKED: text_encoding_unsupported (…/目录.txt, detected_encoding=unknown)
   errors[-1] = {reason: text_encoding_unsupported, detected_encoding: unknown,
                 remediation: convert the file to UTF-8, then re-run route}   exit=1
   （file(1)：ISO-8859 text, CRLF——无 BOM 的 GBK，如实报 unknown，不猜编码）
② 手工 UTF-8 副本放行：iconv -f GBK -t UTF-8 写入 scratch 源（不碰原目录）→
   job A 20260913-112149-local-source-9y-e3d5fd route：
   "routed: discovered documents=2 media=240 unsupported=0 …" status=ROUTING  exit=0
③ 全文件扫描（非只查文件头）：fixture big.txt = 3 MiB 纯 ASCII + 末尾 GBK 字节
   （3145736 B，头 3MB 可过任何 head 检查）→
   BLOCKED: text_encoding_unsupported (…/big.txt, detected_encoding=unknown)    exit=1
④ UTF-32LE BOM fixture（FF FE 00 00 前缀）→
   BLOCKED: … (…/notes.txt, detected_encoding=UTF-32LE)                          exit=1
⑤ .markdown 扩展名 GBK fixture →
   BLOCKED: … (…/course.markdown, detected_encoding=unknown)                     exit=1
对应单测：tests/unit/test_encoding_preflight.py（7 项，含
test_preflight_catches_late_gbk_tail / test_route_blocks_gbk_document /
test_route_markdown_variant_covered / test_unsupported_beats_encoding_per_frozen_order）
```

## E3：240 文件二次转写（transcript cache 复用）✅

```text
方案  真实 ASR 数小时不可重跑 → 复用既有 transcript cache（media-transcriber HEAD
      未变：096e9e38，工作区 clean；A3 characterization 已锁键）。mp4 用同卷硬链接
      （ln，零拷贝；symlink 会被 route 的 rglob is_symlink() 跳过，故不可用）。
      240 个 stem（lowercase）无冲突（程序化核验）。
第一轮 job A（20260913-112149-local-source-9y-e3d5fd）preprocess exit=0，耗时 38s：
      transcribed=0  cache_reused=240  media_failed=0
      events: media_output_ready×240（outcome 全为 cache_reused）、单一 run_id
      （1a977c94…）、preprocess_finished outcome=completed
      docchunk：真实 split 一次（reused=false，corpus=document-set-69e5a6d2fc66，
      verify PASS）——text-only handoff，38s 内完成
第二轮 job B（20260913-112436-local-source-9y-47e9ff）preprocess exit=0，耗时 34s：
      transcribed=0  cache_reused=240  media_failed=0
      duplicate outputs=0（240 rel path / 240 cache_key 全唯一；与 job A 的 240 条
      transcript 路径完全一致）；manifest 重复=0（media_output_ready 事件 240 条，
      每文件恰好 1 条）；transcript-index 240/240 键命中、无重复条目（索引总量 275）
观察  media cache 键全部命中（源 sha + mt HEAD + device/timestamp 与真实 Job 一致，
      证明 A3 键不变约束）；corpus 键第一轮未命中属正常——corpus 键含
      docchunk_revision + config_fingerprint（真实 Job 当时以不同调用指纹入索引），
      与本验收同配置的第二轮即命中（见 E4）。
```

## E4：corpus 复用 ✅

```text
job B preprocess："corpus reused (verified): /Volumes/ORICO/LongDocCorpus/document-set-69e5a6d2fc66"
manifest.docchunk: reused=true, verify=PASS, status=verified
corpus_cache_key（A=B）= sha256:5f8cc9da9b4d1e17c07d04837180529074a19db6a186a4bf84cb0739caefef86
复用前重新 docchunk verify 通过才放行（CorpusCache.lookup 内置 verify）。
```

## E5：真实 COMPLETED manifest V1→V2 迁移 ✅

```text
前置备份  迁移前 cp 原始 job.yaml → scratch（sha256 693bbd300b347b9f…，211104 B）
① 只读命令不迁移：knowledge-ingest status 20260912-163657-baidu-9y（exit 0，
   渲染 COMPLETED/COMPLETED）→ 磁盘 job.yaml 仍 schema_version: 1，无 bak 文件
② save 触发迁移：python -c store.load()+store.save()（ManifestStore.save 对 v1
   原始 bytes 先 backup_v1_manifest 再原子写）
③ 断言（程序化逐项核验）：
   - job.yaml.bak-v1 存在，与验收前备份逐字节相等（byte-equal: True，sha 同上）
   - 迁移后 schema_version=2
   - targets dict 含 cangjie/personal（顺序保持），status success→COMPLETED 正确映射
   - overall status=COMPLETED（不变），active_target=None
   - gate_history 18/18 完整保留（v1 原文件 18 条）
   - media.outputs 240 条，outcome 一律 unknown（V1 历史，禁反推——规格 15）
   - errors 2 条保留（unsupported_inputs / docchunk_split_failed）
④ 报告渲染成功：见 E12
```

## E6：watchdog 实装 ✅

```text
回滚保护  安装前备份旧 plist（~/Library/LaunchAgents/com.sandro.ki-resume.plist，
          v0.2 ops 模板：脚本指向 ~/.local/bin/ki-resume.sh、日志 /tmp）与
          ~/.local/bin/ki-resume.sh 各一份到 scratch
第一次 install：watchdog: plist updated (f0447d09102b774c -> 2a2b834d183d4d33)
          脚本 /Volumes/ORICO/KnowledgePipeline/bin/ki-resume.sh（动态路径，
          A4）；plist load 成功。exit=0
status：  installed, loaded（script sha256:b776b547f1a8767c / plist sha256:2a2b834d183d4d33）
第二次 install（幂等）：watchdog: already up to date (…)。exit=0
launchctl list 含 com.sandro.ki-resume（exit 0）
doctor：[PASS] watchdog_installed: …/com.sandro.ki-resume.plist（16 checks，0 FAIL）
对应单测：tests/unit/test_watchdog_v03.py（install/idempotent/status/uninstall，
launchctl 经 run_launchctl 注入，不触真实 launchd）
```

## E7：Target 顺序 round-trip ✅

```text
真实 CLI：job create --provider local --source acceptance-e7-order-roundtrip
          --target personal --target cangjie --with-router
→ job 20260913-112604-local-acceptance-e7-order-roundtrip-611904
job.yaml 落盘：request.targets = [personal, cangjie, family_router]
             targets dict 键序 = [personal, cangjie, family_router]（声明序保持）
             family_router.depends_on = [cangjie]（model validator 自动补建）
store.load()+store.save() round-trip 后再次读取：两处顺序均不变（断言通过）
对应单测：tests/unit/test_targets_v03.py::test_targets_declaration_order_survives_save_load_roundtrip
```

## E8：V1→V2 保持 precedence ✅

```text
迁移后的真实 Job：knowledge-ingest next 20260912-163657-baidu-9y（exit 0）
  {"status":"COMPLETED","next_action":"report",
   "target_outputs":{"cangjie":"…/distill/20260912-163657-baidu-9y/cangjie/books/minsu-heji-9y",
                     "personal":"…/Obsidian/Skill_Library/03_能力卡片/side-hustle"},
   "cangjie_output":"…","personal_output":"…"}
语义与迁移前一致：COMPLETED + 双输出 + v0.2 兼容键（cangjie_output/personal_output）。
```

## E9：COMPLETED Job amend（scratch 副本方案）✅

```text
按验收计划采用推荐安全方案：cp 真实 Job 目录（job.yaml/handoff/logs/reports，不含
6.6GB source 与锁文件）→ jobs/acceptance-e9-test，job_id 字段改写为 acceptance-e9-test。
真实生产 Job 未被改动（验收后复核：仍 schema 2 / COMPLETED / targets=[cangjie,personal]）。
CLI：job amend acceptance-e9-test --add-target family_router →
  targets: ['cangjie', 'personal', 'family_router']            exit=0
断言（程序化）：
  - family_router entry：depends_on=[cangjie]，status=READY（cangjie COMPLETED 满足依赖）
  - overall：COMPLETED → CORPUS_READY 显式重入可调度态；active_target=null
  - cangjie/personal 保持 COMPLETED
  - gate_history 追加 {"action":"amend_reenter","target":"family_router"}
  - next --json → {"next_action":"invoke_family_router","targets_remaining":["family_router"]}
```

## E10：output-manifest ✅（附 1 项缺陷记录 D1）

```text
对象：真实 cangjie 产物 distill/20260912-163657-baidu-9y/cangjie/books/minsu-heji-9y
程序化调用 build_output_manifest()+render_output_manifest() 两次：
  byte-identical: True（12,028 B；canonical JSON：sort_keys、无 generated_at）
  sha256: 8e9575ce51f90d0418af5a18599492898a884437a60de4f41a5bf19988fcc0c2
字段完整性：
  schema_version=1；skills=17（与真实产物 17 个 skill 一致，name 排序）
  每 skill 含 64 位 sha256（skill_tree_sha256）+ description + artifacts 标志
  （如 minsu-buy-customer-marketing-loop: test_prompts=True, test_results=True）
事务失败注入：单测覆盖 tests/unit/test_output_manifest.py::
  test_target_complete_rename_failure_keeps_target_state（rename 失败 TargetState 不变）
【D1·缺陷，未修】top_level_files 全 False（present 0/4）：真实 cangjie 产物的顶层
  文件是 DIGEST.md / INDEX.md / GLOSSARY.md / PIPELINE_STATE.md（带 .md），而
  output_manifest.py TOP_LEVEL_FILES 检查的是无扩展名 DIGEST/INDEX/GLOSSARY/
  PIPELINE_STATE。单测（test_build_output_manifest_fields）按无扩展名构造并通过——
  实现与单测自洽，但与真实产物命名不符，真实产物一律被判 0/4。属冻结规格的命名
  约定问题（改判定或改产物命名需规格决定），本次验收如实记录、不静默修改。
```

## E11：budget 熔断（scratch job acceptance-e9-test，全部真实 CLI）✅

```text
前置  target start family_router（CORPUS_READY→TARGET_RUNNING，依赖满足）
      distill prepare 写入 handoff/target-family_router.yaml budget 段
      （balanced / max_external_calls=20 / max_retries_per_case=1 / breaker 3空2限流）
① acquire：{"allowed":true,"permit_id":"bp_2ef86376…","call_no":1}          exit=0
② request_id 重放幂等：同 req-r1 二次 acquire → 同 permit bp_2ef86376… 同
   call_no=1，不重复计数
③ outcome：success → noop:false；重复 success → noop:true（幂等）；
   冲突（success 后再报 empty）→ "error: conflicting outcome for permit …:
   'success' != 'empty'" exit=2
④ quota 熔断：budget amend --max-external-calls 3 → acquire 至 3/3 后第 4 次
   → {"allowed":false,"reason":"budget_exhausted"} exit=1；
   target family_router=BLOCKED(budget_exhausted)、overall=BLOCKED、active_target=null
⑤ case retry 超限：resume→start 后 acquire 已 2 attempts 的 case c1
   （上限 max_retries_per_case 1+1=2）→ {"allowed":false,
   "reason":"case_retry_exceeded"} exit=1 → BLOCKED
⑥ breaker 熔断：host h2 连续 3 个 empty outcome → 第 4 次 acquire（新 case）
   → {"allowed":false,"reason":"breaker_open"} exit=1 → BLOCKED；
   budget_state：calls=6，h2 {consecutive_empty:3, consecutive_rate_limit:0}
⑦ amend ≠ 自动恢复：每次 budget amend 后 target 仍 BLOCKED（输出明确提示走
   target resume）；target resume → READY（依赖满足）/ overall CORPUS_READY，
   再 target start → TARGET_RUNNING，全链路可重入
注：acquire 校验顺序为 quota → breaker → case-retry（budget.py 冻结实现），
   quota 耗尽时即使 case 同时超限也先报 budget_exhausted（观察项，非缺陷）。
对应单测：tests/unit/test_budget_v03.py（13 项，含 replay/quota/breaker/case-retry/
conflict/amend 不恢复状态）
```

## E12：审计报告渲染 ✅

```text
knowledge-ingest report 20260912-163657-baidu-9y → reports/final.md（332 行）exit=0
- 含 "## 运行审计" 段（final.md:289）
- 转写口径：fresh 0 / cache_reused 0 / unknown 240（unknown=V1 历史，禁反推——规格 15）
- 阶段耗时对 v1 迁移 manifest 如实标注 "unknown / not instrumented"
  （source/docchunk/cangjie/personal），绝不用 created_at 冒充
- 渲染无崩溃；预算段 "no external calls recorded"
```

## E13：PARTIAL 聚合 ✅

```text
单测：tests/unit/test_target_runtime_e2e.py::test_e14_failed_cangjie_propagates_skip_partial
     （cangjie FAILED → family_router SKIPPED(dependency_failed) → personal COMPLETED
     → Job PARTIAL）
真实 CLI 复验（scratch 副本 acceptance-e13-test）：程序化把 cangjie 置 FAILED
（settle 为公共收敛入口，亦即程序化入口的既定用法）→
  cangjie=FAILED, personal=COMPLETED, overall=PARTIAL；
  knowledge-ingest next acceptance-e13-test → {"status":"PARTIAL"}  exit=0
说明：副本先把 overall 复位为 TARGET_RUNNING/cangjie RUNNING（真实历史中该 Job
cangjie 进行时确实处于该态，见 gate_history 2026-09-12T23:19 start），
使 TARGET_RUNNING→PARTIAL 为合法跃迁；COMPLETED 是终态、无出边，程序无法也
不应从 COMPLETED 直接聚合出 PARTIAL。
```

## E14：依赖传播 ✅

```text
scratch job acceptance-e9-test（family_router 先 resume→READY）：
程序化等价于 gate resolve --decision rejected 的终态效果（cangjie 已 COMPLETED
无法再进 live gate）：cangjie 置 SKIPPED(user_rejected) → settle() →
  family_router：SKIPPED(reason=dependency_skipped)   ← 传播自动完成
  personal：COMPLETED（不受影响）
  overall：COMPLETED（SKIPPED 计入完成聚合）；next --json → next_action=report
```

## 发现的缺陷清单

| # | 项 | 描述 | 处置 |
|---|---|---|---|
| D1 | E10 | output_manifest 的 top_level_files 检查无扩展名（DIGEST/INDEX/GLOSSARY/PIPELINE_STATE），真实 cangjie 产物为 .md 后缀 → 真实产物恒判 0/4 present。实现与单测自洽、与真实产物不符 | **未修**（改判定或改产物命名属冻结规格决定，留规格层裁决）；验收数字如实记录 |
| — | — | 其余 13 项未发现缺陷（含架构级检查点：三桶路由保序、编码预检全文件扫描、缓存键稳定性、迁移字节级备份、budget 三类熔断与幂等） | — |

## 收尾状态

- `uv run pytest -o addopts= -q` → 261 passed（验收前后一致）
- 真实生产 Job `20260912-163657-baidu-9y`：仅发生 E5 迁移（v1 原件字节级留底
  `job.yaml.bak-v1`），状态 COMPLETED、targets=[cangjie,personal] 未被重排；
  E9/E11/E13/E14 全部在 `jobs/acceptance-e9-test`、`jobs/acceptance-e13-test` scratch 副本进行
- 验收产物（scratch 目录、fixtures、acceptance-* job、本报告）全部保留作审计痕迹
- watchdog：A4 动态路径版已安装并 loaded（旧 v0.2 版 plist/脚本备份于 scratch）
