# Recovery — 断点续跑策略

> 目标：任何时刻、任何会话中断后，Agent 都能从 Job Manifest 恢复到精确断点，
> 绝不重复下载、转写或 docchunk。

## 恢复流程（每次会话开始）

```text
1. knowledge-ingest doctor                     # 环境健康
2. knowledge-ingest resume                     # 一条命令：全部 Job 的下一步
3. 对未完成 Job：knowledge-ingest next JOB --json
4. 按 next_action 执行；绝不从 RAW 原料重跑已完成阶段
```

## 无人值守续跑（v0.2.0 内建）

- `resume --exec`：锁保护下自动续跑第一个可续跑 Job（ROUTING/TRANSCRIBING/
  DOCCHUNKING/VERIFYING）；preprocess 持 `.preprocess.lock`（pid 文件），
  运行中的 Job 自动跳过，陈旧锁不阻塞。
- 仓库 `ops/ki-resume.sh` + `ops/com.sandro.ki-resume.plist`：LaunchAgent 模板
  （RunAtLoad + 每 15 分钟触发；等 ORICO 挂载后 resume --exec）。
  首次生产运行（跨两次凌晨 3 点重启）已实战验证该机制。
- 转写逐文件落盘：中断后已完成的文件零丢失，`status` 与 events.jsonl 可核对。

## 各状态的恢复语义

| 状态 | 恢复动作 | 禁止 |
|---|---|---|
| CREATED | `next` → await_source；继续让云盘 Skill 下载/注册 | — |
| DISCOVERING / DOWNLOADING | 重新调用云盘 Skill 查询下载状态；已完整下载则 register | 重复下载已完成文件 |
| DOWNLOADED | `route` | — |
| ROUTING | `preprocess` | — |
| TRANSCRIBING | `preprocess`（media 子阶段先查 TranscriptCache；命中即跳过 ASR） | 重新转写已缓存媒体 |
| DOCCHUNKING | `preprocess`（重建 handoff 后先查 CorpusCache；命中且 verify PASS 即复用） | 重复 OCR/chunk |
| VERIFYING | `preprocess`（直接对 corpus_path 执行 verify） | 重新 split |
| CORPUS_READY | `next` → invoke target（从蒸馏开始） | 重新 preprocess |
| DISTILLING_CANGJIE / DISTILLING_PERSONAL | `next` → invoke target；**先读原 Skill 断点文件**：Cangjie 的 `PIPELINE_STATE.md`（manifest.cangjie.pipeline_state）；Personal 产物 YAML 元数据中的命名状态 | 从原 PDF/视频重新开始 |
| WAITING_USER | `next` → ask_user；把 gate 问题原样重新展示给用户 | 伪造用户确认 |
| BLOCKED | `next` → resolve_blocked；读 errors[-1].reason，按下方策略处理 | 静默忽略失败 |
| FAILED / COMPLETED | 终态；仅报告 | — |

## BLOCKED 恢复策略

| reason | 恢复方式 |
|---|---|
| `unsupported_inputs` | 与用户确认排除清单 → `route JOB --exclude PATH ...` 重新路由；状态机允许 BLOCKED→ROUTING |
| `media_failed` | 修复来源/重试该媒体；用户明确排除该文件后 `route --exclude` 重跑 |
| `corpus_verify_failed` | 排查 docchunk 输出（`docchunk doctor` / `docchunk status`）；必要时删除坏 corpus 后重跑 preprocess |
| `docchunk_split_failed` | 查看 job logs/docchunk-split.log 与 `docchunk doctor`；修复后重跑 preprocess（状态机允许 DOCCHUNKING→BLOCKED→DOCCHUNKING） |
| `media_stem_conflict` | 集合内存在同名 stem（如 A/01.mp4 与 B/01.mp4）：重命名源文件或经用户排除其一后重跑 |
| `collection_build_failed` | 检查源文件是否被移动/删除（symlink 失败）；恢复来源后重跑 |
| `source_incomplete` / `source_missing` / `provider_mismatch` / `invalid_handoff_schema` | Source Handoff 未达完成门：重新完成下载并生成合规 handoff 后再 register |
| `baidu_scope_limited` | 两种恢复：把文件移到"我的应用数据/bdpan"，或提供分享链接 |
| `collection_incomplete` | 补齐缺失转写/文档，或用户明确排除后重跑 |

## 状态文件分工

- **Job Manifest（job.yaml）**：总控唯一权威状态；每阶段成功立即原子落盘。
- **Cangjie PIPELINE_STATE.md**：原 Skill 内部断点；总控只登记路径、读取参考，不复制内容。
- **Personal 产物 YAML 元数据**：同上。
- **cache/*.json**：transcript/corpus 复用索引；lookup 时强制重校验（md 指纹 / corpus verify）。
