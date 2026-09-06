# Architecture

```text
                         User
                          │
                          ▼
                knowledge-ingest (SKILL.md)
                          │  编排：意图/云盘 Skill/蒸馏 Skill/确认门
                          ▼
                knowledge-ingest (Python CLI)
                          │  状态/指纹/路由/调用/缓存/报告
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   baidu-drive       quarkclouddrive      Local
        └─────────────────┼─────────────────┘
                          ▼
              File Type Router (router.py)
                ├─ Document → docchunk
                └─ Media → media-transcriber → docchunk
                          ▼
              docchunk Corpus (verify PASS 硬门)
                  ├─ cangjie-skill
                  └─ personal-capability-distiller
                          ▼
                   Final Job Report
```

## 模块

| 模块 | 职责 |
|---|---|
| `config.py` | YAML 配置加载与路径展开 |
| `doctor.py` | 机器基线检查（14 项） |
| `models.py` / `manifest_store.py` | Job Manifest 数据模型与原子落盘 |
| `state_machine.py` | 显式状态机；非法跃迁抛 InvalidTransition |
| `fingerprint.py` | 文件/目录内容指纹（symlink 按目标内容计） |
| `router.py` | 文档/媒体/不支持 分类 |
| `adapters/local.py` | 本地 Source Handoff |
| `adapters/docchunk.py` | split/verify/status（含 corpus 路径解析） |
| `adapters/media.py` | transcribe + TranscriptResult |
| `runner.py` | 无 shell subprocess；spawn/poll 长任务；安全日志 |
| `cache.py` | Transcript/Corpus 缓存（复用前强制重校验） |
| `collection.py` | 混合课程 Document Set（symlink + provenance map） |
| `next_action.py` | next 协议、gate enter/resolve、target complete |
| `report.py` | status/report + 事件日志 + 脱敏 |

## 运行时目录

```text
/Volumes/ORICO/KnowledgePipeline/
├── jobs/<job-id>/{job.yaml, source/, handoff/{source.json, document-set/},
│                  reports/final.md, logs/events.jsonl}
├── cache/{transcript-index.json, corpus-index.json}
├── distill/<job-id>/{cangjie/, personal/}     # 原 Skill 运行 cwd（断点文件落这里）
└── tmp/
```

现有项目输出目录保持不变：`/Volumes/ORICO/MediaTranscriber`、`/Volumes/ORICO/LongDocCorpus`。
总控只保存路径引用与 provenance，不复制它们的产物。

## 设计原则

1. 现有工具各司其职，不重复造轮子（OCR/ASR/chunking/蒸馏全部复用）。
2. docchunk Corpus 是统一知识中间层；verify PASS 才能进蒸馏。
3. Chunking is lossless, distillation may be lossy。
4. 前处理全自动；蒸馏阶段保留真实人工确认门。
5. Local First；不自动上传/删除云盘文件。
