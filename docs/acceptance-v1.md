# knowledge-ingest V1 端到端验收记录

> 验收时间：2026-09-06。实施计划：`docs/knowledge-ingest-implementation-v1.md`（V1.1）。
> 全部可自动化 Case 均以真实本机数据执行（真实 PDF、真实语音 m4a、真实 docchunk/ASR/缓存）。

## 结果总览

| Case | 内容 | 结果 |
|---|---|---|
| A | Local PDF → docchunk → verify → CORPUS_READY | ✅ PASS |
| B | Local m4a → 转写 → docchunk；二次运行命中缓存，不重复 ASR | ✅ PASS |
| C | 夸克混合课程 → 双蒸馏 | ⏸ 待用户完成夸克授权后补测（Skill 已装） |
| D | Cangjie WAITING_USER 断点恢复 | ✅ PASS |
| E | 跨来源同 PDF 去重复用 | ✅ 机制实证（跨 Job 同 cache key + reused）；云端真实下载待 C 后补测 |
| F | 媒体失败阻断 | ✅ PASS（单元级：`media_failed` → BLOCKED，不进蒸馏） |
| G | verify FAIL 阻断 | ✅ PASS（单元级：`corpus_verify_failed` → BLOCKED，next 不返回蒸馏） |
| H | 百度普通网盘范围限制 | ✅ PASS |
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
uv run pytest -q                          → 全部通过
uv run knowledge-ingest doctor --config … → 14 项检查，无 FAIL
```

## 发现并修复的缺陷（验收价值）

1. **preprocess 状态推进缺失**（commit 8ea472c）：有媒体的 Job 在转写完成后未推进
   TRANSCRIBING→DOCCHUNKING，导致 split 后非法跃迁崩溃；重入守卫同样漏了
   TRANSCRIBING。已修复并回归。
2. **缺 `target start` 命令**：Agent 无法合法从 CORPUS_READY 进入 DISTILLING_*。
   已补充（`knowledge-ingest target start JOB --target …`）。
3. `--config` 只在全局位置可用 → 改为挂在每个子命令上。

## 待用户配合的补测

- 完成 bdpan 登录（`bash ~/.agents/skills/baidu-drive/scripts/login.sh`）后：
  Case A 的云端版（/apps/bdpan 内 PDF）+ Case E 的跨来源真实下载。
- 完成夸克授权后：Case C 混合课程双蒸馏全流程。
