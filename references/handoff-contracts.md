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
- 百度：`remote.path` 必须位于 `apps/bdpan`（或 `/apps/bdpan`）之下，否则
  register 直接 `BLOCKED: baidu_scope_limited`。
- 不得包含任何 Token/Cookie/授权码。

## 2. Target Handoff（knowledge-ingest → 蒸馏 Skill）

canonical 文件：`jobs/<job-id>/handoff/target-<target>.yaml`
（schema 见 `schemas/target-handoff.example.yaml`）。

```yaml
job_id: <job-id>
target: cangjie | personal
corpus_path: /Volumes/ORICO/LongDocCorpus/<corpus-id>   # 已 verify PASS
source:
  provider: quark
  title: 融资担保课程
  author_or_speaker: null    # 缺失保持 null，禁止猜测
  publication_date: null
first_pilot: true            # cangjie：是否首次试点
depth: null                  # personal：A/B/C；null=原 Skill 默认（实测默认 C）
obsidian_vault: /Volumes/ORICO/Obsidian/Skill_Library   # personal 专用
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

## 5. 串行顺序

`CORPUS_READY → Cangjie → Personal → COMPLETED`；不并行，不重复 docchunk。
