# Routing — 文件类型路由

## 支持列表（router.py）

```python
DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MEDIA_EXTS    = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a",
                 ".wav", ".flac", ".aac"}
IGNORED_NAMES = {".DS_Store"}
```

另有约定：`._*`（AppleDouble）与隐藏目录整体忽略。

## 分流

| 输入 | 处理 |
|---|---|
| 单个文档 | 直接 `docchunk split` |
| 单个媒体 | `media-transcriber transcribe` → `.md`+`metadata.yaml` → docchunk |
| 目录（混合） | Collection：媒体逐个转写 → `handoff/document-set/`（symlink + map）→ 一次 `docchunk split`（docchunk 原生支持目录输入，symlink 实测可读） |
| 不支持格式 | `UNSUPPORTED`，写入报告；目录中存在未排除的 unsupported → `BLOCKED: unsupported_inputs`，须用户 `route --exclude` 明示排除 |

## 转写约定

- 命令固定：项目 cwd 内 `uv run media-transcriber transcribe <abs-path>
  --device auto --timestamp 10m`（值来自 config.processing）。
- **transcribe 直接收绝对路径**，无需文件先进 inbox；inbox/watcher 保留给人工场景。
- 产物只认 `output/<stem>/<stem>.md` + 同目录 `metadata.yaml` 成对出现。
- 缓存 key：source SHA-256 + media-transcriber git HEAD + config SHA + 实际参数。

## 指纹约定

- 文件：流式 SHA-256（8 MiB chunk），`sha256:` 前缀。
- 目录：递归枚举（忽略 `.DS_Store`/`._*`/隐藏目录），POSIX 相对路径排序，
  canonical JSON 再 SHA-256；**文件 symlink 按解析目标内容计**（handoff 由
  symlink 组成，指纹必须反映最终内容以支撑跨来源去重）。
