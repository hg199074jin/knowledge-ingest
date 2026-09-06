# knowledge-ingest V1 端到端验收记录

> 验收时间：2026-09-06。实施计划：`docs/knowledge-ingest-implementation-v1.md`（V1.1）。
> 全部可自动化 Case 均以真实本机数据执行（真实 PDF、真实语音 m4a、真实 docchunk/ASR/缓存）。

## 结果总览

| Case | 内容 | 结果 |
|---|---|---|
| A | Local PDF → docchunk → verify → CORPUS_READY | ✅ PASS |
| B | Local m4a → 转写 → docchunk；二次运行命中缓存，不重复 ASR | ✅ PASS |
| C | 夸克混合课程 → CORPUS_READY（双蒸馏就绪） | ✅ PASS（2026-09-06 云端补测） |
| D | Cangjie WAITING_USER 断点恢复 | ✅ PASS |
| E | 跨来源同 PDF 去重复用 | ✅ 机制实证（跨 Job 同 cache key + reused）；云端真实下载待 C 后补测 |
| F | 媒体失败阻断 | ✅ PASS（`test_preprocess_cli.py::test_media_failure_blocks`：BLOCKED，不进蒸馏） |
| G | verify FAIL 阻断 | ✅ PASS（`test_preprocess_cli.py::test_verify_fail_blocks`：BLOCKED，next 不返回蒸馏） |
| H | 百度普通网盘范围限制 | ✅ PASS（CLI 层越界下载亦被官方 CLI 自身拦截） |
| I | 敏感信息扫描 | ✅ PASS（rg 零命中） |
| J | 完整测试套件 + doctor | ✅ PASS（86 测试全绿；doctor 14 项无 FAIL） |

## Case A：Local PDF → CORPUS_READY

```text
Job      20260906-035210-local-acceptance-sample-pdf
材料     /Volumes/ORICO/KnowledgePipeline/tmp/acceptance/acceptance-sample.pdf
         （cupsfilter 生成的 1 页真实文本 PDF）
流程     job create → source register（含源指纹）→ route（documents=1）
         → preprocess（docchunk split 走 MinerU，PDF 真实解析）
结果     corpus: /Volumes/ORICO/LongDocCorpus/document-set-46504ad6242f
         docchunk.verify=PASS，overall=CORPUS_READY
验收点   本地原 PDF 未复制（只读引用）；Job 可恢复 ✓
```

## Case B：Local m4a → 转写 → docchunk → 缓存复用

```text
Job      20260906-035327-local-lesson01-m4a
材料     say + afconvert 生成的真实中文语音 lesson01.m4a（约 96 KB）
首次     media-transcriber 真实 ASR → output/lesson01/lesson01.md + metadata.yaml
         转写内容正确（"融资担保公司风险管理第一课，担保的核心是风险定价与风险缓释…"）
         → docchunk split → verify PASS → CORPUS_READY
二次     preprocess 再跑耗时 0.5s：
         - TranscriptCache 命中（相同 source sha + mt HEAD + 参数）→ 无重复 ASR
         - CorpusCache 命中且 verify 重校验 PASS → docchunk.reused=true
验收点   第二次同配置运行命中 transcript/cache，不重复 ASR ✓
```

## Case D：断点恢复

```text
Job      20260906-035858-local-acceptance-sample-pdf（targets: cangjie+personal）
流程     … → preprocess（CorpusCache 跨 Job 命中，零 split）→ target start cangjie
         → gate enter stage0_overview → WAITING_USER
新会话   knowledge-ingest status：Cangjie waiting_user: stage0_overview
         knowledge-ingest next --json：{"next_action":"ask_user",
         "target":"cangjie","gate":"stage0_overview"}
验收点   不重新下载、不转写、不 docchunk（media=success、docchunk=verified PASS）✓
```

## Case E：跨来源同内容去重（机制实证）

```text
Job A 与 Job D 来自同一 PDF（不同 Job）：
  docchunk.cache_key 均为 sha256:32c35d11ed67c60…（handoff 指纹 + docchunk HEAD + config）
  Job D 复用 Job A 的 verified Corpus（reused=true，corpus 路径相同）
  两份 Job 各自保留 provider provenance
剩余     百度 app 目录与夸克真实下载 → 待用户完成网盘授权后按同法验证
```

## Case H：百度范围限制

```text
Job      20260906-040008-baidu-pdf（remote.path = Downloads/课程.pdf，越界）
结果     source register → BLOCKED: baidu_scope_limited（exit 1）
         next → {"next_action":"resolve_blocked","reason":"baidu_scope_limited"}
恢复路径 ①移到"我的应用数据/bdpan" ②提供分享链接（已写入 cloud-sources.md）
```

## Case I：敏感信息扫描

```bash
rg -c -i 'access[_-]?token|refresh[_-]?token|cookie|authorization|password|secret' \
  /Volumes/ORICO/KnowledgePipeline/jobs/
# 退出码 1 = 零文件命中；jobs/*/logs/events.jsonl 为结构化脱敏 JSON
```

## Case J：完整测试套件

```text
uv run pytest -q                          → 全部通过（111 项）
uv run knowledge-ingest doctor --config … → 14 项检查，无 FAIL
```

## 发现并修复的缺陷（验收价值）

1. **preprocess 状态推进缺失**（commit 8ea472c）：有媒体的 Job 在转写完成后未推进
   TRANSCRIBING→DOCCHUNKING，导致 split 后非法跃迁崩溃；重入守卫同样漏了
   TRANSCRIBING。已修复并回归。
2. **缺 `target start` 命令**：Agent 无法合法从 CORPUS_READY 进入 DISTILLING_*。
   已补充（`knowledge-ingest target start JOB --target …`）。
3. `--config` 只在全局位置可用 → 改为挂在每个子命令上。

## 云端补测记录（2026-09-06，用户完成两网授权后）

### Case C：夸克混合课程 → CORPUS_READY ✅

```text
Job      20260906-092904-quark-job（targets: cangjie + personal）
来源     夸克网盘 /高考志愿/高报文件（张雪峰高考志愿系列节选）
选择     官方 Skill Search(dir) → Browse --all 逐级枚举（完整 Artifact，非 5 条预览）
         选定 2 mp4（12.0/11.8 MB）+ 1 pdf（0.7 MB），download --fid 逐个下载
         本地大小与云端一致 → download_completed 门通过
链路     source register（remote fids 留痕）→ route（collection=True, media=2,
         documents=1, unsupported=0）→ preprocess（2 个真实 ASR + PDF 走 MinerU）
结果     media success 2/2；docchunk verified PASS
         corpus: /Volumes/ORICO/LongDocCorpus/document-set-19f8485b2e9d
         next --json → {"next_action":"invoke_cangjie","targets_remaining":
         ["cangjie","personal"]}
溯源     document-set-map.yaml 保留 3 条完整 provenance（原文件名/相对路径/
         transcript 路径），未拼失去来源边界的 Markdown
说明     invoke_cangjie 起的蒸馏环节按设计等待用户发起（含人工确认门），
         属 Case C 的后半段，不在本次自动化补测范围
授权备注  quark CLI 必须带 CLAUDECODE=1 环境标记（-104 问题，见 cloud-sources.md）
```

### Case H 补充：百度 CLI 层越界拦截 ✅

```bash
bdpan download "/14.135KE/.../第44讲：重新定义输入能力_IMG.pdf" /tmp/bdpan-test/
# → Error: 远端路径无效: 路径超出授权目录范围
```

即使用户/Agent 想绕过总控直接越界，官方 bdpan CLI 自身也强制拦截——
与知识库 register 校验（baidu_scope_limited）形成双层防护。

### Case A 云端版 / Case E 跨来源：待一个入盘文件

百度授权有效（whoami 正常），但 `/apps/bdpan/`（我的应用数据/bdpan）当前为空，
全盘其余文件按设计不可下载（CLI 层已实证拦截）。待用户任选其一：
① 上传任意一个小 PDF 到"我的应用数据/bdpan"；② 提供一个百度分享链接（走
transfer 转存）。随后即可跑 Case A 云端版；若放的是与夸克相同内容的文件，
同时完成 Case E 跨来源去重的真实验证。

## 评审修复轮（v0.1.1，2026-09-06）

按 `requesting-code-review` skill 派独立评审代理对 62772d7..59991e0 全量复核
（无 Critical、10 项 Important），全部修复并复验：

| # | 修复 | commit |
|---|---|---|
| 2/6 | source register 完成门（schema/download_completed/provider/local_path）+ 百度路径正则边界、全路径规范 | 1b7a4b7 |
| 3/4 | gate 拒绝后链式进入剩余 target；`target complete --pipeline-state` 登记 Cangjie 断点文件 | 2c4a798 |
| 7 | 缓存索引原子写入 + 损坏自愈 | 7b81e16 |
| 5 | raw_prompt 入库前与报告渲染双脱敏 | c9d8bce |
| 1/8/9/10 | split 切换到 spawn/poll（长任务约束落地）+ 30s 进度事件；split/symlink 失败收敛为 BLOCKED；同名 stem 冲突门；MediaOutput 目录级 provenance；新增 `test_preprocess_cli.py` 6 项编排测试 | a497c85 |
| Minor | collection 先建目录、safe_argv 支持 `--k=v`、main 捕获 ValueError、test_resume 走合法跃迁、SKILL/README 补 `target start`、recovery.md 补 4 类恢复策略、handoff-contracts 定稿百度全路径规范 | 本提交 |

修复后真实链路复验：新 md 源 cache miss → 真实 docchunk split 经 spawn/poll
执行成功（5.3s）；事件日志出现 `docchunk_split_started` / `corpus_verified`；
m4a 新 Job 双缓存命中 + 二次调用幂等（already CORPUS_READY）。
已声明不采纳的评审项：doctor 增加 bdpan 登录检查（避免运行 bdpan 命令或读取
认证配置，保持 Skill 触发纪律与安全边界；登录状态在 runtime-inventory 人工跟踪）。
