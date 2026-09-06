# Knowledge Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **版本：V1.1（2026-09-06 依本机 Runtime Inventory 校订）。** 修订点已就地标注（"本机事实"/"V1.1"）；
> 实测基线见 `docs/runtime-inventory.md`，实施时以其为准。

**Goal:** 在 Mac mini 上实现一个独立的 `knowledge-ingest` 总控 Skill，把本地文件、夸克网盘、百度网盘资料统一送入 `media-transcriber → docchunk → cangjie-skill / personal-capability-distiller`，并提供可验证 Corpus、Job Manifest、断点续跑、去重、人工确认门和最终报告。

**Architecture:** `knowledge-ingest` 不重写 OCR、ASR、长文档切分或蒸馏算法。Python 小型 CLI 只负责本地状态、指纹、路由、调用 `media-transcriber`/`docchunk`、缓存与报告；网盘搜索下载、Cangjie 蒸馏、个人能力蒸馏继续由对应 Agent Skill 执行。Python CLI 与上层 Skill 之间通过稳定的 YAML/JSON handoff contract 连接，避免绑定第三方 Skill 的内部实现。

**Tech Stack:** macOS / Apple Silicon；Python 3.12；`uv`；stdlib `argparse` / `subprocess` / `hashlib` / `pathlib`；`PyYAML`；`pydantic>=2`；现有 `media-transcriber`、`docchunk`、`cangjie-skill`、`personal-capability-distiller`；百度 `baidu-drive`；夸克 `quarkclouddrive`。

**Spec:** `docs/knowledge-ingest-design-v1.md`

## Global Constraints

- 目标机：macOS / Apple Silicon，Python 3.12，`uv` 已安装（本机实测 3.12.14 / uv 0.12.5）。
- 数据根目录默认：`/Volumes/ORICO/KnowledgePipeline`。
- 现有项目继续使用自己的正式输出目录：`/Volumes/ORICO/MediaTranscriber` 与 `/Volumes/ORICO/LongDocCorpus`。
- `knowledge-ingest` 不修改 `media-transcriber`、`docchunk`、`cangjie-skill`、`personal-capability-distiller` 核心代码。
- `docchunk verify` 退出码非 0 时，禁止进入任何蒸馏步骤。
- 不自动删除或修改云端源文件，不自动上传蒸馏结果。
- Token/Cookie/授权码/密码不得写入 Job Manifest、日志、报告或测试 fixture。
- 所有 destructive 操作默认关闭。
- V1 单任务串行，不实现 Web UI、数据库服务、队列服务、多机调度和并发 Worker。
- Cangjie 与 personal-capability-distiller 的用户确认门必须保留，不能伪造用户确认。
- 百度官方 `baidu-drive` 当前操作范围限制在 `/apps/bdpan/`；V1 只支持该应用目录内资料，或用户提供的百度分享链接。不得把它描述为"可搜索整个百度网盘"。
- 夸克 Skill 的第三方 CLI 命令细节不写死进 Python 核心；由 `quarkclouddrive` Skill 负责搜索与下载，再向 `knowledge-ingest` 提交统一 Source Handoff。
- **（V1.1）遵本机全局规则（~/.codex/AGENTS.md）：docchunk 长任务不得同步阻塞（§17）；Skill 只装入实体库 `~/.agents/skills/`（§19）；实施代码前载入 claude-worker-router（§13）。**

## Pre-Implementation Gate: 本机 Runtime Inventory

**状态（2026-09-06）：已完成**，结果固化在 **`docs/runtime-inventory.md`**。以下命令保留给"组件升级后重新校验"场景：

```bash
sw_vers
uname -m
python3 --version
uv --version
mount | grep '/Volumes/ORICO' || true

git -C /Volumes/ORICO/Projects/docchunk rev-parse HEAD
git -C /Volumes/ORICO/Projects/media-transcriber rev-parse HEAD

cd /Volumes/ORICO/Projects/docchunk && uv run docchunk --help
cd /Volumes/ORICO/Projects/docchunk && uv run docchunk doctor
cd /Volumes/ORICO/Projects/media-transcriber && uv run media-transcriber --help
cd /Volumes/ORICO/Projects/media-transcriber && uv run media-transcriber doctor

# V1.1：包含全部视图目录（实体库为 ~/.agents/skills，其余为 symlink 视图）
find ~/.agents/skills ~/.zcode/skills ~/.claude/skills -maxdepth 2 -name SKILL.md 2>/dev/null | \
  grep -E 'baidu-drive|quarkclouddrive|cangjie-skill|personal-capability-distiller' || true

# V1.1：Skill 视图完整性体检（只读）
~/.local/bin/skill-sync
```

**实测基线摘要**（细节见 runtime-inventory）：docchunk main@e84e889（v1.1.0，uv 全局可编辑安装）；
media-transcriber main@096e9e3（项目内 `uv run` 调用）；云端两 Skill 已安装
（baidu-drive v1.7.5 / bdpan CLI 3.8.7，登录授权待用户；quarkclouddrive 1.0.17-ea0ddf3）。

**Gate：** 已通过。组件升级后必须先更新 `docs/runtime-inventory.md` 再开始相关 Task。

---

## 0. 先确定最终文件结构

创建独立仓库，建议路径：

```text
/Volumes/ORICO/Projects/knowledge-ingest/
├── pyproject.toml
├── README.md
├── SKILL.md
├── config.example.yaml
├── docs/
│   ├── knowledge-ingest-design-v1.md
│   ├── knowledge-ingest-implementation-v1.md
│   └── runtime-inventory.md
├── src/
│   └── knowledge_ingest/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── manifest_store.py
│       ├── state_machine.py
│       ├── doctor.py
│       ├── fingerprint.py
│       ├── router.py
│       ├── runner.py
│       ├── cache.py
│       ├── collection.py
│       ├── report.py
│       └── adapters/
│           ├── __init__.py
│           ├── local.py
│           ├── media.py
│           └── docchunk.py
├── references/
│   ├── architecture.md
│   ├── routing.md
│   ├── cloud-sources.md
│   ├── target-gates.md
│   ├── recovery.md
│   └── handoff-contracts.md
├── schemas/
│   ├── source-handoff.example.json
│   └── target-handoff.example.yaml
├── templates/
│   └── job.yaml
└── tests/
    ├── fixtures/
    │   ├── small.txt
    │   ├── small.md
    │   └── source-handoff.json
    ├── unit/
    │   ├── test_config.py
    │   ├── test_manifest_store.py
    │   ├── test_state_machine.py
    │   ├── test_fingerprint.py
    │   ├── test_router.py
    │   ├── test_cache.py
    │   ├── test_collection.py
    │   └── test_runner.py
    └── integration/
        ├── test_local_docchunk.py
        ├── test_media_handoff.py
        └── test_resume.py
```

运行时目录固定为：

```text
/Volumes/ORICO/KnowledgePipeline/
├── jobs/<job-id>/
│   ├── job.yaml
│   ├── source/
│   ├── handoff/
│   │   ├── source.json
│   │   └── document-set/
│   ├── reports/
│   │   └── final.md
│   └── logs/
│       └── events.jsonl
├── cache/
│   ├── corpus-index.json
│   └── transcript-index.json
├── distill/            # V1.1：cangjie/personal 的运行 cwd（PIPELINE_STATE.md 等落这里）
│   └── <job-id>/
│       ├── cangjie/
│       └── personal/
└── tmp/
```

`source/` 只保存本 Job 从云端下载的正式本地副本；本地输入不复制到 `source/`。

---

### Task 1: 初始化仓库、配置与 Doctor

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.yaml`
- Create: `src/knowledge_ingest/config.py`
- Create: `src/knowledge_ingest/doctor.py`
- Create: `src/knowledge_ingest/cli.py`
- Create: `tests/unit/test_config.py`
- Create: `tests/unit/test_doctor.py`

**Interfaces:**
- Produces: `AppConfig.load(path: Path | None) -> AppConfig`
- Produces: `run_doctor(config: AppConfig) -> list[DoctorCheck]`
- Produces CLI: `knowledge-ingest doctor [--config PATH] [--json]`

- [ ] **Step 1: 创建 `pyproject.toml`**

```toml
[project]
name = "knowledge-ingest"
version = "0.1.0"
description = "Multi-source knowledge ingestion orchestrator"
requires-python = ">=3.12,<3.13"
dependencies = [
  "PyYAML>=6.0.2,<7",
  "pydantic>=2.9,<3",
]

[project.scripts]
knowledge-ingest = "knowledge_ingest.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: 创建默认配置**

（V1.1：skill_roots 覆盖实体库与全部视图；doctor 端做 resolve() 去重，避免同一实体被三个根重复检查。）

```yaml
pipeline_root: /Volumes/ORICO/KnowledgePipeline
media_project: /Volumes/ORICO/Projects/media-transcriber
docchunk_project: /Volumes/ORICO/Projects/docchunk
media_output_root: /Volumes/ORICO/MediaTranscriber/output
docchunk_corpus_root: /Volumes/ORICO/LongDocCorpus

skill_roots:
  - ~/.agents/skills    # 实体库（AGENTS.md §19）
  - ~/.zcode/skills     # 视图
  - ~/.claude/skills    # 视图

skills:
  baidu: baidu-drive
  quark: quarkclouddrive
  cangjie: cangjie-skill
  personal_distiller: personal-capability-distiller

processing:
  media_device: auto
  media_timestamp: 10m
  require_orico: true
```

- [ ] **Step 3: 先写配置失败测试**

```python
from pathlib import Path
from knowledge_ingest.config import AppConfig


def test_expand_user_and_paths(tmp_path: Path):
    cfg = AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills", "~/.zcode/skills", "~/.claude/skills"],
        "skills": {
            "baidu": "baidu-drive",
            "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m", "require_orico": False},
    })
    assert cfg.pipeline_root.is_absolute()
    assert cfg.skill_roots[0].is_absolute()
```

- [ ] **Step 4: 运行测试确认失败**

```bash
uv run pytest tests/unit/test_config.py -v
```

Expected: import 或模型不存在导致 FAIL。

- [ ] **Step 5: 实现 `AppConfig` 与 YAML 加载**

```python
from pathlib import Path
import yaml
from pydantic import BaseModel, field_validator

class SkillNames(BaseModel):
    baidu: str
    quark: str
    cangjie: str
    personal_distiller: str

class ProcessingConfig(BaseModel):
    media_device: str = "auto"
    media_timestamp: str = "10m"
    require_orico: bool = True

class AppConfig(BaseModel):
    pipeline_root: Path
    media_project: Path
    docchunk_project: Path
    media_output_root: Path
    docchunk_corpus_root: Path
    skill_roots: list[Path]
    skills: SkillNames
    processing: ProcessingConfig

    @field_validator("pipeline_root", "media_project", "docchunk_project", "media_output_root", "docchunk_corpus_root", mode="before")
    @classmethod
    def expand_path(cls, value):
        return Path(value).expanduser().resolve()

    @field_validator("skill_roots", mode="before")
    @classmethod
    def expand_roots(cls, values):
        return [Path(v).expanduser().resolve() for v in values]

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
```

- [ ] **Step 6: 编写 Doctor 测试与实现**

Doctor 必须检查：Darwin、arm64、Python 3.12、`uv`、ORICO、pipeline root 可写、media project、docchunk project、4 个目标 Skill、百度/夸克 Skill。

**（V1.1 修订检查语义）**

- Skill 检查：对每个 skill_root 先 `resolve()` 再去重（视图与实体库同源），在其中任一根找到
  `<skill>/SKILL.md` 即算存在。
- **media-transcriber / docchunk / Cangjie / personal-distiller 缺失或 doctor 不通过记为 FAIL**；
  **baidu-drive / quarkclouddrive 缺失记为 WARN**（本机 2026-09-06 已安装，安装后应 PASS；
  baidu 附加检查项"是否已完成 bdpan 登录"只记 WARN/信息，不阻断）。
- docchunk 项目检查：项目目录存在 + `uv run docchunk doctor` 通过（注意 docchunk 亦有全局
  `~/.local/bin/docchunk` 可编辑 shim，doctor 记录两种调用形态均可）。
- media 项目检查：项目目录存在 + `uv run media-transcriber doctor` 通过。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str
```

- [ ] **Step 7: CLI 接入 doctor**

```bash
uv run knowledge-ingest doctor --config ./config.yaml
uv run knowledge-ingest doctor --config ./config.yaml --json
```

JSON 输出必须是机器可消费对象，退出码规则：存在 FAIL -> 1；仅 WARN/PASS -> 0。

- [ ] **Step 8: 全部测试通过并提交**

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_doctor.py -v
git add .
git commit -m "feat: scaffold knowledge-ingest and doctor"
```

---

### Task 2: Job Manifest 数据模型、原子写入与状态机

**Files:**
- Create: `src/knowledge_ingest/models.py`
- Create: `src/knowledge_ingest/manifest_store.py`
- Create: `src/knowledge_ingest/state_machine.py`
- Create: `templates/job.yaml`
- Create: `tests/unit/test_manifest_store.py`
- Create: `tests/unit/test_state_machine.py`

**Interfaces:**
- Produces: `JobManifest`
- Produces: `ManifestStore.create(request: JobRequest) -> JobManifest`
- Produces: `ManifestStore.load(job_id: str) -> JobManifest`
- Produces: `ManifestStore.save(manifest: JobManifest) -> None`
- Produces: `transition(manifest, event: JobEvent) -> JobManifest`

- [ ] **Step 1: 定义最小模型**

```python
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal

OverallStatus = Literal[
    "CREATED", "DISCOVERING", "DOWNLOADING", "DOWNLOADED", "ROUTING",
    "TRANSCRIBING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
    "DISTILLING_CANGJIE", "DISTILLING_PERSONAL", "WAITING_USER",
    "COMPLETED", "PARTIAL", "BLOCKED", "FAILED"
]

class JobRequest(BaseModel):
    raw_prompt: str
    provider: Literal["local", "baidu", "quark"]
    source: str
    targets: list[Literal["cangjie", "personal"]]

class StageState(BaseModel):
    status: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

class DocchunkState(StageState):
    corpus_path: Path | None = None
    verify: Literal["PASS", "FAIL"] | None = None
    cache_key: str | None = None

class TargetState(StageState):
    waiting_for: str | None = None
    output_path: Path | None = None

class JobManifest(BaseModel):
    schema_version: int = 1
    job_id: str
    created_at: datetime
    updated_at: datetime
    status: OverallStatus = "CREATED"
    request: JobRequest
    source: dict = Field(default_factory=dict)
    routing: dict = Field(default_factory=dict)
    media: StageState = Field(default_factory=StageState)
    docchunk: DocchunkState = Field(default_factory=DocchunkState)
    cangjie: TargetState = Field(default_factory=TargetState)
    personal: TargetState = Field(default_factory=TargetState)
    errors: list[dict] = Field(default_factory=list)
```

- [ ] **Step 2: 写原子写入测试**

测试必须确认：写入后可 reload；保存过程使用同目录临时文件；异常不破坏旧 `job.yaml`。

```python
def test_save_and_reload_roundtrip(store, request):
    created = store.create(request)
    created.status = "ROUTING"
    store.save(created)
    loaded = store.load(created.job_id)
    assert loaded.status == "ROUTING"
    assert loaded.request.raw_prompt == request.raw_prompt
```

- [ ] **Step 3: 实现原子 YAML 写入**

```python
def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
```

`ManifestStore.save()` 必须先更新时间、再序列化、再原子替换，不得先更新下一阶段后再落盘。

- [ ] **Step 4: 状态机只允许明确转换**

至少实现：

```text
CREATED -> DISCOVERING | DOWNLOADED | ROUTING
DISCOVERING -> DOWNLOADING | BLOCKED | FAILED
DOWNLOADING -> DOWNLOADED | FAILED
DOWNLOADED -> ROUTING
ROUTING -> TRANSCRIBING | DOCCHUNKING | BLOCKED
TRANSCRIBING -> DOCCHUNKING | BLOCKED | FAILED
DOCCHUNKING -> VERIFYING | FAILED
VERIFYING -> CORPUS_READY | BLOCKED
CORPUS_READY -> DISTILLING_CANGJIE | DISTILLING_PERSONAL | COMPLETED
DISTILLING_CANGJIE -> WAITING_USER | DISTILLING_PERSONAL | COMPLETED | FAILED
DISTILLING_PERSONAL -> WAITING_USER | COMPLETED | FAILED
WAITING_USER -> DISTILLING_CANGJIE | DISTILLING_PERSONAL | COMPLETED | BLOCKED
```

非法跃迁必须抛 `InvalidTransition`。

- [ ] **Step 5: 写非法跃迁测试**

```python
import pytest

def test_cannot_distill_before_corpus_ready(manifest):
    manifest.status = "ROUTING"
    with pytest.raises(InvalidTransition):
        transition_to(manifest, "DISTILLING_CANGJIE")
```

- [ ] **Step 6: 提交**

```bash
uv run pytest tests/unit/test_manifest_store.py tests/unit/test_state_machine.py -v
git add .
git commit -m "feat: add durable job manifest and state machine"
```

---

### Task 3: Source Handoff、Local Adapter、文件类型 Router 与源指纹

**Files:**
- Create: `src/knowledge_ingest/fingerprint.py`
- Create: `src/knowledge_ingest/router.py`
- Create: `src/knowledge_ingest/adapters/local.py`
- Create: `schemas/source-handoff.example.json`
- Create: `tests/unit/test_fingerprint.py`
- Create: `tests/unit/test_router.py`

**Interfaces:**
- Produces: `fingerprint_file(path: Path) -> FileFingerprint`
- Produces: `fingerprint_collection(root: Path) -> CollectionFingerprint`
- Produces: `route_source(path: Path) -> RoutingResult`
- Produces: canonical `source.json` contract used by local/baidu/quark.

- [ ] **Step 1: 固化 Source Handoff contract**

```json
{
  "schema_version": 1,
  "provider": "quark",
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

Local provider：`remote=null`，`local_path` 指向用户原路径，不复制。

- [ ] **Step 2: 指纹算法**

文件：流式 SHA-256，chunk 8 MiB。

目录：递归枚举普通文件，忽略 `.DS_Store`、`._*`；以 POSIX 相对路径排序，记录 `relative_path + size + sha256`，对 canonical JSON 再做 SHA-256。目录 Hash 必须与遍历顺序无关。

```python
def fingerprint_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(8 * 1024 * 1024):
            h.update(block)
    return f"sha256:{h.hexdigest()}"
```

- [ ] **Step 3: Router 支持列表**

```python
DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".txt"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".aac"}
IGNORED_NAMES = {".DS_Store"}
```

返回：`documents[]`、`media[]`、`unsupported[]`、`is_collection`。

- [ ] **Step 4: 默认阻断 unsupported collection**

如果用户请求的是整个目录，且其中存在非隐藏 unsupported 文件，Router 结果写入 manifest 后将总体状态设为 `BLOCKED`，`reason=unsupported_inputs`。只有用户明确排除这些文件后才能继续。

- [ ] **Step 5: 测试**

```python
def test_router_mixed_collection(tmp_path):
    (tmp_path / "01.mp4").write_bytes(b"video")
    (tmp_path / "02.pdf").write_bytes(b"pdf")
    (tmp_path / "03.xyz").write_bytes(b"x")
    result = route_source(tmp_path)
    assert len(result.media) == 1
    assert len(result.documents) == 1
    assert len(result.unsupported) == 1
```

- [ ] **Step 6: CLI 增加 source register 与 route**

```bash
knowledge-ingest job create --provider local --source /path/to/file --target cangjie
knowledge-ingest source register JOB_ID --handoff /path/to/source.json
knowledge-ingest route JOB_ID
```

`route` 成功后立即保存 Job Manifest。

- [ ] **Step 7: 提交**

```bash
uv run pytest tests/unit/test_fingerprint.py tests/unit/test_router.py -v
git add .
git commit -m "feat: add source handoff routing and fingerprints"
```

---

### Task 4: 安全 Subprocess Runner 与 DocChunk Adapter

**Files:**
- Create: `src/knowledge_ingest/runner.py`
- Create: `src/knowledge_ingest/adapters/docchunk.py`
- Create: `tests/unit/test_runner.py`
- Create: `tests/integration/test_local_docchunk.py`

**Interfaces:**
- Produces: `run_checked(argv: list[str], cwd: Path, timeout: int | None = None) -> CommandResult`
- Produces: `DocchunkAdapter.split(input_path: Path) -> Path`
- Produces: `DocchunkAdapter.verify(corpus_path: Path) -> bool`

- [ ] **Step 1: Runner 禁止 `shell=True`**

```python
@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    started_at: datetime
    ended_at: datetime
```

`run_checked` 必须使用：

```python
subprocess.run(
    argv,
    cwd=cwd,
    text=True,
    capture_output=True,
    check=False,
    timeout=timeout,
)
```

命令日志只记录 argv 的安全部分；不得记录认证参数或第三方网盘 token。

**（V1.1 新增长任务约束）** `run_checked` 只用于快命令（doctor/verify/status）。
`docchunk split` 处理 PDF 时走 MinerU，可达 10–20+ 分钟（AGENTS.md §17：不同步阻塞）。
Runner 必须另提供长任务入口：

```python
def spawn(argv: list[str], cwd: Path) -> Popen        # Popen 启动，写 pid/argv 到 job logs
def poll(handle) -> RunningProcess | FinishedProcess  # 轮询；进度事件写入 events.jsonl
```

轮询依据：进程退出码 + corpus 内 `logs/processing.jsonl`（docchunk 自带）+ `docchunk status <corpus>`。
禁止为了"等它结束"而无超时地阻塞调用方会话。

- [ ] **Step 2: DocChunk 命令固定由项目 cwd 执行**

```text
cwd=/Volumes/ORICO/Projects/docchunk
uv run docchunk doctor
uv run docchunk version
uv run docchunk split <input>
uv run docchunk status <corpus>
uv run docchunk verify <corpus>
```

**（本机事实）** docchunk 同时是 uv 全局可编辑安装（`~/.local/bin/docchunk`），任意目录可直调；
两种形态都允许，但 Adapter 内部固定其一（建议项目内 `uv run`，与 media-transcriber 对称）。
docchunk 为可编辑安装，**Corpus cache key 必须包含 docchunk git HEAD**（见 Task 7）。

- [ ] **Step 3: Corpus 路径解析不要依赖单一输出文案**

算法：
1. 从 stdout/stderr 中提取所有绝对路径候选；
2. 按倒序检查候选；
3. 只有 `candidate/manifest.json` 与 `candidate/index.jsonl` 同时存在才接受；
4. 没找到则抛 `CorpusPathNotFound`，不得猜目录名。

（本机事实：corpus 目录名模式为 `<标题≤48>-<源指纹前12>`，默认根
`/Volumes/ORICO/LongDocCorpus`；此算法与该结构兼容。）

```python
def resolve_corpus_path(result: CommandResult) -> Path:
    candidates = extract_absolute_paths(result.stdout + "\n" + result.stderr)
    for p in reversed(candidates):
        if (p / "manifest.json").is_file() and (p / "index.jsonl").is_file():
            return p.resolve()
    raise CorpusPathNotFound()
```

- [ ] **Step 4: Verify 是硬门**

```python
def verify(self, corpus: Path) -> bool:
    result = run_checked(["uv", "run", "docchunk", "verify", str(corpus)], cwd=self.project)
    return result.returncode == 0
```

只用退出码作为硬判据；日志中保留 stdout/stderr 供诊断。

**（本机事实）** `docchunk split` 成功退出已内含一次 verify；本 Adapter 仍显式调用
`docchunk verify` 作为独立硬门，二者不冲突，属幂等二次校验。

- [ ] **Step 5: Mock 单元测试**

测试 0 exit、1 exit、路径找不到、路径存在但无 manifest。

- [ ] **Step 6: 真实 Local TXT 集成测试**

```bash
uv run knowledge-ingest job create --provider local --source tests/fixtures/small.txt --target cangjie
uv run knowledge-ingest route JOB_ID
uv run knowledge-ingest preprocess JOB_ID
uv run knowledge-ingest status JOB_ID
```

Expected：`docchunk.status=verified`，`status=CORPUS_READY`，`corpus_path` 存在。

- [ ] **Step 7: 提交**

```bash
uv run pytest tests/unit/test_runner.py tests/integration/test_local_docchunk.py -v
git add .
git commit -m "feat: integrate verified docchunk preprocessing"
```

---

### Task 5: Media Adapter、Transcript 缓存与单媒体链路

**Files:**
- Create: `src/knowledge_ingest/adapters/media.py`
- Create: `src/knowledge_ingest/cache.py`
- Create: `tests/unit/test_cache.py`
- Create: `tests/integration/test_media_handoff.py`

**Interfaces:**
- Produces: `MediaAdapter.transcribe(path: Path) -> TranscriptResult`
- Produces: `TranscriptCache.lookup(key: str) -> TranscriptCacheEntry | None`
- Produces: `TranscriptCache.put(entry) -> None`

- [ ] **Step 1: 定义 Transcript cache key**

不要只按文件名缓存。Key 必须包含：

```text
source_sha256
media-transcriber git HEAD
config 文件 SHA-256（若使用配置）
实际 CLI 参数：device / timestamp / glossary / hotwords
```

Canonical payload JSON 后 SHA-256。

（本机事实：media-transcriber 未全局安装，固定 `uv run` 于项目 cwd；
`--device` 默认 auto、`--timestamp` 默认 10m（可选 none/5m/10m/15m）。）

- [ ] **Step 2: Media 命令**

```text
cwd=/Volumes/ORICO/Projects/media-transcriber
uv run media-transcriber doctor
uv run media-transcriber transcribe <absolute-source> --device auto --timestamp 10m
```

（本机事实：`transcribe` 直接收绝对路径，不需要文件先进 inbox——设计文档 §12 的
"不复制进 Inbox"要求已由官方 CLI 原生满足。产物固定为
`/Volumes/ORICO/MediaTranscriber/output/<stem>/<stem>.md` + 同目录 `metadata.yaml`。）

若项目配置要求 glossary，由上层 Skill 明确传入；V1 总控不自动猜 glossary。

- [ ] **Step 3: TranscriptResult 必须同时找到 Markdown 与 metadata**

```python
class TranscriptResult(BaseModel):
    source_path: Path
    markdown_path: Path
    metadata_path: Path
    source_sha256: str
    transcript_sha256: str
    cache_key: str
```

解析结果时只接受：`.md` 存在 + 同目录 `metadata.yaml` 存在。

- [ ] **Step 4: 缓存复用前校验**

命中 cache 后重新检查：
- Markdown 存在；
- metadata 存在；
- Markdown 当前 SHA-256 与 cache 中一致。

任一不满足则 cache miss，正常重跑。

- [ ] **Step 5: 单媒体 handoff**

单个 MP4 的 transcript 不直接送 Cangjie。应先：

```text
media-transcriber -> transcript.md -> docchunk split transcript.md -> verify
```

- [ ] **Step 6: 测试缓存语义**

```python
def test_cache_rejects_modified_transcript(tmp_path):
    entry = make_entry(tmp_path)
    cache.put(entry)
    entry.markdown_path.write_text("changed", encoding="utf-8")
    assert cache.lookup(entry.cache_key) is None
```

- [ ] **Step 7: 使用一个短测试媒体进行真实 smoke test**

必须选择用户有权处理的短中文测试媒体，运行：

```bash
knowledge-ingest job create --provider local --source /absolute/path/sample.mp4 --target cangjie
knowledge-ingest route JOB_ID
knowledge-ingest preprocess JOB_ID
```

Expected：`media.status=success`、`docchunk.verify=PASS`、总体 `CORPUS_READY`。

- [ ] **Step 8: 提交**

```bash
uv run pytest tests/unit/test_cache.py tests/integration/test_media_handoff.py -v
git add .
git commit -m "feat: add media transcription handoff and cache"
```

---

### Task 6: 混合课程 Collection Handoff

**Files:**
- Create: `src/knowledge_ingest/collection.py`
- Create: `tests/unit/test_collection.py`
- Modify: `src/knowledge_ingest/cli.py`

**Interfaces:**
- Produces: `build_document_set(job, routing, transcripts) -> DocumentSetResult`
- Produces: `handoff/document-set/` containing only docchunk-supported files.

- [ ] **Step 1: Document Set 不拼成长 Markdown**

输入：

```text
source/课程/
├── 01.mp4
├── 02.mp4
├── 03.pdf
└── 04.docx
```

Handoff：

```text
handoff/document-set/
├── 01.md -> MediaTranscriber/output/.../01.md
├── 02.md -> MediaTranscriber/output/.../02.md
├── 03.pdf -> source/课程/03.pdf
└── 04.docx -> source/课程/04.docx
```

- [ ] **Step 2: 使用稳定自然排序与可追溯映射**

同时写：

```yaml
handoff/document-set-map.yaml:
  - handoff_name: 01.md
    source_relative_path: 01.mp4
    kind: transcript
    transcript_path: /Volumes/ORICO/MediaTranscriber/output/.../01.md
  - handoff_name: 03.pdf
    source_relative_path: 03.pdf
    kind: document
    source_path: /Volumes/.../03.pdf
```

- [ ] **Step 3: V1 使用 symlink，启动时做能力验证**

（本机事实：`docchunk split` 官方支持目录输入；symlink 能力仍需一次本地 integration test
验证——docchunk 以 `readable` 路径读入，macOS 上 symlink 一般可读，但必须实测。）

实现一次本地 integration test：DocChunk 能否读取 handoff 目录中的文件 symlink。若 test FAIL，Collection Builder 的正式 fallback 顺序固定为：
1. symlink；
2. 若 docchunk 无法读取 symlink，则 hardlink（同文件系统普通文件）；
3. hardlink 不可用则 copy 文档/转写文本到 handoff。

媒体原文件绝不复制进 handoff。

- [ ] **Step 4: 防止文件名冲突**

不同子目录出现同名文件时，handoff 名称使用：

```text
<natural-index>__<sanitized-relative-path>
```

原始路径永远保存在 `document-set-map.yaml`，因此重命名不损失 provenance。

- [ ] **Step 5: 集合完整性规则**

任一媒体转写失败、任一文档缺失、任一 unsupported 未被用户明确排除时，不构建最终 Corpus；总体状态 `BLOCKED`。

- [ ] **Step 6: 将整个 handoff 目录送入一次 docchunk split**

```bash
uv run docchunk split /Volumes/ORICO/KnowledgePipeline/jobs/<job>/handoff/document-set
uv run docchunk verify <corpus-path>
```

- [ ] **Step 7: 提交**

```bash
uv run pytest tests/unit/test_collection.py -v
git add .
git commit -m "feat: build provenance-preserving document sets"
```

---

### Task 7: Corpus 去重、幂等与复用

**Files:**
- Modify: `src/knowledge_ingest/cache.py`
- Modify: `src/knowledge_ingest/adapters/docchunk.py`
- Create: `tests/unit/test_corpus_cache.py`

**Interfaces:**
- Produces: `build_corpus_cache_key(handoff_fingerprint, docchunk_revision, config_fingerprint) -> str`
- Produces: `CorpusCache.lookup(key) -> Path | None`

- [ ] **Step 1: Corpus cache 不用 raw source hash 作为唯一 key**

必须使用最终 handoff 内容：

```text
handoff_fingerprint
+ docchunk git HEAD
+ docchunk config fingerprint
```

这样视频转写变化后不会错误复用旧 Corpus。

（本机事实：docchunk 是 uv 可编辑安装，仓库 HEAD 变化会即时生效——cache key 里的
git HEAD 是正确性要求，不是可选优化。另外 docchunk split 自身对未变更源自动复用，
与本层缓存互补。）

- [ ] **Step 2: 复用前必须再次 verify**

```python
cached = corpus_cache.lookup(key)
if cached is not None and docchunk.verify(cached):
    return cached
```

Verify 失败就移除 cache entry 并重建。

- [ ] **Step 3: 跨来源同内容复用**

百度与夸克下载得到同一 PDF，只要最终 handoff fingerprint 相同，即可复用同一 verified Corpus。Job Manifest 各自保留自己的 source provenance。

- [ ] **Step 4: 测试**

```python
def test_same_content_different_paths_same_key(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert fingerprint_file(a) == fingerprint_file(b)
```

- [ ] **Step 5: 提交**

```bash
uv run pytest tests/unit/test_corpus_cache.py -v
git add .
git commit -m "feat: add verified corpus reuse"
```

---

### Task 8: Next-Action 协议、人工确认门与 Resume

**Files:**
- Create: `src/knowledge_ingest/next_action.py`
- Modify: `src/knowledge_ingest/cli.py`
- Create: `tests/integration/test_resume.py`
- Create: `references/target-gates.md`
- Create: `references/recovery.md`

**Interfaces:**
- Produces CLI: `knowledge-ingest next JOB_ID --json`
- Produces CLI: `knowledge-ingest gate enter JOB_ID --target ... --name ...`
- Produces CLI: `knowledge-ingest gate resolve JOB_ID --target ... --name ... --decision confirmed|rejected`
- Produces CLI: `knowledge-ingest target complete JOB_ID --target ... --output-path ...`

- [ ] **Step 1: `next` 返回动作，而不是自动模拟用户**

示例：

```json
{
  "job_id": "20260905-233500-quark-guarantee-course",
  "status": "CORPUS_READY",
  "next_action": "invoke_cangjie",
  "corpus_path": "/Volumes/ORICO/LongDocCorpus/...",
  "targets_remaining": ["cangjie", "personal"]
}
```

等待用户时：

```json
{
  "status": "WAITING_USER",
  "next_action": "ask_user",
  "target": "cangjie",
  "gate": "stage1_5_candidates"
}
```

（V1.1：gate 名称取自 target-gates.md 的映射表——cangjie 侧为
`stage0_overview` / `stage1_5_candidates` / `stage5_install_location`，
personal 侧为原 Skill 的 workflow-states 命名状态，见 Task 9/10。）

- [ ] **Step 2: Gate enter 必须先落 Manifest**

当 Cangjie/Distiller 要求确认：

```bash
knowledge-ingest gate enter JOB --target cangjie --name stage1_5_candidates
```

Manifest 保存 `WAITING_USER` 后，Agent 才向用户展示问题。

- [ ] **Step 3: 用户回复后只记录真实 decision**

```bash
knowledge-ingest gate resolve JOB --target cangjie --name stage1_5_candidates --decision confirmed
```

没有真实用户消息时禁止执行此命令。

- [ ] **Step 4: Resume 测试**

构造：media success + docchunk verified + cangjie waiting_user。重新加载 Job 后 `next` 必须返回 `ask_user`；绝不能返回 `transcribe` 或 `docchunk`。

（V1.1：Cangjie 分支的 resume 还要求读其 `PIPELINE_STATE.md`（Task 9 Step 2），
确认原 Skill 内部断点后决定重入点。）

- [ ] **Step 5: 提交**

```bash
uv run pytest tests/integration/test_resume.py -v
git add .
git commit -m "feat: add resumable user gates and next-action protocol"
```

---

### Task 9: Cangjie Handoff

**Files:**
- Create: `references/handoff-contracts.md`
- Modify: `SKILL.md`
- Create: `schemas/target-handoff.example.yaml`

**Interfaces:**
- Agent-level handoff only; Python core不实现 Cangjie 方法论。
- Consumes: verified `corpus_path` + Job provenance.
- Produces: target state updates via CLI gate/complete commands.

- [ ] **Step 1: 固化给 Cangjie 的输入包**

（V1.1：字段对齐 cangjie-skill 实际输入要求——内容文本来源 + 元信息 + 首次试点。）

```yaml
job_id: <job-id>
target: cangjie
corpus_path: /Volumes/ORICO/LongDocCorpus/...   # 内容文本来源：经 longdoc-router 按 Batch 消费
source:
  provider: quark
  title: 融资担保课程          # 书名 或 标题
  author_or_speaker: null     # 作者 或 讲者；缺失保持 null，禁止猜测
  publication_date: null      # 出版年 或 发布时间；缺失保持 null
first_pilot: true             # 是否首次试点
purpose: 用户原始要求
provenance_manifest: /Volumes/ORICO/KnowledgePipeline/jobs/<job>/job.yaml
output_preference: auto
```

- [ ] **Step 2: Cangjie 必须通过 longdoc-router 读取 Corpus**

`SKILL.md` 明确要求：

```text
先读取 docchunk manifest/index，按 B0001、B0002……顺序消费 Reading Batches；
overlap_atomic_ids 仅作为上下文桥；
全部 new_atomic_ids 完成后再进入跨文档蒸馏。
```

**（V1.1 新增：固定运行 cwd 与断点文件）**

- Cangjie 的运行 cwd 固定为 `/Volumes/ORICO/KnowledgePipeline/distill/<job-id>/cangjie/`
  （其产物按约定写入 cwd 下 `books/<slug>/`）。
- Job Manifest 登记其断点文件：`cangjie.pipeline_state = books/<slug>/PIPELINE_STATE.md`。
- 恢复 Job 时**先读 PIPELINE_STATE.md** 确认原 Skill 内部断点，再决定重入点；
  总控 Manifest 与该文件互补，不复制其内容。

- [ ] **Step 3: Cangjie 确认点映射到 Job gate**

**（V1.1 修订）** cangjie-skill 内部没有命名门；真实确认点 3 处，映射如下
（gate 名称是总控自定义，映射表维护在 `references/target-gates.md`；
原 Skill 的 Gate 条件本身不得改变）：

| Job gate 名称 | Cangjie 真实确认点 |
|---|---|
| `stage0_overview` | 阶段 0：展示 BOOK_OVERVIEW.md 骨架，用户确认后才进阶段 1 |
| `stage1_5_candidates` | 阶段 1.5：候选清单轻确认（捞回/砍掉） |
| `stage5_install_location` | 阶段 5：询问安装位置（本机推荐答案：实体库 `~/.agents/skills/`，视图自动同步；须用户真实确认） |

~~`stage_5_compile_mode_confirmation`~~：**删除**——原 Skill 无"编译模式"概念。

当 Cangjie 到达上述确认点时：

```bash
knowledge-ingest gate enter JOB --target cangjie --name stage1_5_candidates
```

Manifest 保存 `WAITING_USER` 后，Agent 才向用户展示问题。

- [ ] **Step 4: 完成后登记输出**

```bash
knowledge-ingest target complete JOB --target cangjie --output-path /absolute/path/to/cangjie/output
```

如果该 Job 还有 personal target，`next` 返回 `invoke_personal_distiller`；否则进入 COMPLETED。

- [ ] **Step 5: 手工 smoke test**

使用 `small.txt` 形成的 verified Corpus，跑到 Cangjie 第一个确认点（`stage0_overview`），退出会话，再恢复；确认不会重新 docchunk。

- [ ] **Step 6: 提交**

```bash
git add SKILL.md references schemas
git commit -m "feat: add cangjie corpus handoff"
```

---

### Task 10: Personal Capability Distiller Handoff

**Files:**
- Modify: `SKILL.md`
- Modify: `references/handoff-contracts.md`
- Modify: `references/target-gates.md`

**Interfaces:**
- Agent-level handoff only。
- Consumes同一 verified Corpus，不重新读取原 PDF/视频。

- [ ] **Step 1: 输入 contract**

```yaml
job_id: <job-id>
target: personal
corpus_path: /Volumes/ORICO/LongDocCorpus/...   # Markdown 输入：combined.md / atomic 满足其 Markdown-only 要求
source_title: 融资担保课程
depth: null
provenance_manifest: /Volumes/ORICO/KnowledgePipeline/jobs/<job>/job.yaml
obsidian_vault: /Volumes/ORICO/Obsidian/Skill_Library
```

用户没有指定 A/B/C 时 `depth=null`，遵循原 Skill 默认行为（**实测默认 C 档**并显式
声明，可一键升档；用户显式指定优先）；总控不自行改成 A。

**（V1.1 新增：vault 路径 override）** 原 Skill 内部把 vault 根写死为
`E:\Obsidian\Skill_Library`（Windows）。本机 vault 为
`/Volumes/ORICO/Obsidian/Skill_Library`（00_能力地图 … 07_应用反馈 +
90_模板与配置 结构完整）。总控调用时必须显式传入 `obsidian_vault` 本机路径；
vault 缺失时沿用原 Skill 暂存 `drafts/` 的行为。

- [ ] **Step 2: 保留原 Skill 的阶段体系与用户 Gate**

**（V1.1 修订）** 不硬编码阶段数（原 Skill 的"阶段产出"表 13 行、典型轨迹 14 阶段
两种口径并存）。总控只映射其 workflow-states 命名状态；需要用户明示动作的关键门：

- `inventory_reviewed`（资料清点后确认主领域与档位）
- `human_material_approved`（须用户明示"定稿"）
- `installation_approved`（须用户明示授权；绝不自动安装到平台 Skills 目录）

原 Skill 每次等待时：

```bash
knowledge-ingest gate enter JOB --target personal --name <workflow-state 名>
```

原 Skill 要求"定稿"或"安装"明确授权时，必须等用户真实回复。

- [ ] **Step 3: Cangjie 和 Personal 默认串行**

若 targets 同时包含两者：

```text
CORPUS_READY -> Cangjie -> Personal -> COMPLETED
```

不并行，不重复 docchunk。

（Personal 运行 cwd 同样固定在 `/Volumes/ORICO/KnowledgePipeline/distill/<job-id>/personal/`。）

- [ ] **Step 4: 完成登记**

```bash
knowledge-ingest target complete JOB --target personal --output-path /absolute/path/to/capability/output
```

- [ ] **Step 5: 提交**

```bash
git add SKILL.md references
git commit -m "feat: add personal capability distillation handoff"
```

---

### Task 11: 百度网盘 Source Adapter（Agent 层）

**Files:**
- Create: `references/cloud-sources.md`
- Modify: `SKILL.md`
- Add test fixture documentation: `tests/fixtures/baidu-source-handoff.json`

**前置状态（V1.1）：** `baidu-drive` Skill v1.7.5 与 bdpan CLI 3.8.7 已安装于实体库
`~/.agents/skills/baidu-drive`（2026-09-06）。**网盘登录授权需用户扫码完成
（`scripts/login.sh`），未登录前真实下载 smoke test 保持 BLOCKED。**

**Interfaces:**
- Consumes official `baidu-drive` Skill.
- Produces canonical `source.json` only after download completed.

- [ ] **Step 1: 明确 V1 百度范围**

`SKILL.md` 必须写清：官方 `bdpan` 的普通文件路径均相对于 `/apps/bdpan/`。因此 V1 支持两种模式：

```text
A. app_path：文件已在“我的应用数据/bdpan”
B. share_link：用户提供百度分享链接，baidu-drive 负责转存到应用目录后下载
```

用户要求搜索普通百度网盘其它目录时：

```text
status=BLOCKED
reason=baidu_scope_limited
```

并告诉用户两种恢复方式：把文件移到"我的应用数据/bdpan"，或提供该资料的百度分享链接。

- [ ] **Step 2: 不在 Python 中解析百度 Token/config**

禁止读取 `~/.config/bdpan/config.json`。认证与下载全部交给 `baidu-drive` Skill。

- [ ] **Step 3: 下载目的地固定**

```text
/Volumes/ORICO/KnowledgePipeline/jobs/<job-id>/source/
```

Skill 调用 baidu-drive 完成下载后，检查本地目标存在、大小稳定，再生成 canonical `source.json`。

- [ ] **Step 4: Canonical Handoff 示例**

```json
{
  "schema_version": 1,
  "provider": "baidu",
  "remote": {
    "id": null,
    "path": "审计/课程.pdf",
    "name": "课程.pdf",
    "size_bytes": 123456,
    "mtime": null
  },
  "local_path": "/Volumes/ORICO/KnowledgePipeline/jobs/<job>/source/课程.pdf",
  "download_completed": true,
  "source_notes": ["baidu app scope"]
}
```

- [ ] **Step 5: 手工 smoke tests**（前置：用户已完成 bdpan 登录授权）

Case 1：`/apps/bdpan/` 中一个小 PDF -> 下载 -> register -> route -> docchunk verify。

Case 2：一个用户提供的分享链接 -> 下载 -> register -> route。

Case 3：用户要求"搜索整个百度网盘 Downloads 目录" -> 必须 BLOCKED，不得假装可以搜索。

- [ ] **Step 6: 提交**

```bash
git add SKILL.md references tests/fixtures/baidu-source-handoff.json
git commit -m "feat: add safe baidu source integration"
```

---

### Task 12: 夸克网盘 Source Adapter（Agent 层）

**Files:**
- Modify: `references/cloud-sources.md`
- Modify: `SKILL.md`
- Add: `tests/fixtures/quark-source-handoff.json`

**前置状态（V1.1）：** `quarkclouddrive` Skill/CLI 1.0.17-ea0ddf3 已安装于实体库
`~/.agents/skills/quarkclouddrive`（2026-09-06），`quark-drive.cjs --version` 自检通过。
首次身份验证按该 Skill 自身流程进行。

**Interfaces:**
- Consumes official `quarkclouddrive` Skill.
- Produces canonical `source.json` after selected file/folder is fully downloaded.

- [ ] **Step 1: 不把 Quark CLI 内部命令写死进 Python**

原因：官方 Skill 会更新 CLI/协议，且要求自己的 install/session 参数。总控只要求：

```text
调用 quarkclouddrive Skill 搜索/浏览
→ 由 quarkclouddrive Skill 按自身当前文档选择文件
→ 下载到 Job source 目录
→ knowledge-ingest 接管本地阶段
```

- [ ] **Step 2: 搜索文件夹时保留官方 Artifact 完整性语义**

上层 Skill 调用 Quark 时遵循官方规则：
- 找文件夹：Search dir；
- 枚举直接子项：Browse all；
- Search/Browse 完整结果以 Artifact 为准；
- 后续下载使用完整结果，不把最多 5 条预览当成全部候选。

- [ ] **Step 3: Session 参数由 Quark Skill 自己维护**

总控 Job Manifest 不保存 Quark OAuth Token；可以保存非敏感的 `remote fid`/路径/名称，但用户面向报告默认不展示内部 ID。

- [ ] **Step 4: 下载完成门**

只有 Quark Skill 明确下载完成且本地文件存在，才生成 Source Handoff。下载中断/未完成时维持 `DOWNLOADING` 或 `FAILED`，不得 route 半文件。

- [ ] **Step 5: 手工 smoke tests**

Case 1：搜索并下载一个 PDF。

Case 2：搜索并下载一个 MP4，进入 media-transcriber。

Case 3：选择一个含 2 视频 + 1 PDF 的目录，完整下载后形成 mixed Document Set。

- [ ] **Step 6: 提交**

```bash
git add SKILL.md references tests/fixtures/quark-source-handoff.json
git commit -m "feat: add quark source integration"
```

---

### Task 13: 最终报告、状态查询、事件日志与敏感信息脱敏

**Files:**
- Create: `src/knowledge_ingest/report.py`
- Modify: `src/knowledge_ingest/manifest_store.py`
- Modify: `src/knowledge_ingest/cli.py`
- Create: `tests/unit/test_report.py`

**Interfaces:**
- Produces CLI: `knowledge-ingest status JOB_ID [--json]`
- Produces CLI: `knowledge-ingest report JOB_ID`
- Produces: `reports/final.md`
- Produces: `logs/events.jsonl`

- [ ] **Step 1: 事件日志只记结构化非敏感事件**

```json
{"ts":"2026-09-05T23:35:00+08:00","event":"docchunk_verified","job_id":"...","corpus_path":"/Volumes/..."}
```

Redactor 至少过滤 key 名匹配：

```text
token
cookie
authorization
auth_code
password
secret
access_token
refresh_token
```

- [ ] **Step 2: status 人类可读输出**

```text
Job            20260905-233500-quark-guarantee-course
Source         success
Transcribe     success 8/8
DocChunk       verified
Cangjie        waiting_user: stage1_5_candidates
Personal       pending
Overall        WAITING_USER
```

- [ ] **Step 3: final.md 必须包含**

```text
请求
来源
本地源路径
源指纹
路由统计
转写产物
Corpus 路径及 verify 结果
Cangjie 输出
Personal 输出
显式排除项
失败/警告
可恢复信息
```

不得包含 Token/Cookie/授权码。

- [ ] **Step 4: 测试报告脱敏**

```python
def test_report_does_not_leak_tokens(tmp_path):
    manifest = make_manifest_with_error("access_token=abc123")
    report = render_report(manifest)
    assert "abc123" not in report
    assert "[REDACTED]" in report
```

- [ ] **Step 5: 提交**

```bash
uv run pytest tests/unit/test_report.py -v
git add .
git commit -m "feat: add status reporting and redacted event logs"
```

---

### Task 14: `SKILL.md` 总控流程定稿与安装

**Files:**
- Finalize: `SKILL.md`
- Finalize: `references/architecture.md`
- Finalize: `references/routing.md`
- Finalize: `references/cloud-sources.md`
- Finalize: `references/target-gates.md`
- Finalize: `references/recovery.md`
- Finalize: `references/handoff-contracts.md`
- Create: `README.md`

**Interfaces:**
- Natural-language entry point for Claude Code / Codex compatible Agent Skills.

- [ ] **Step 1: SKILL 触发语义**

应覆盖：

```text
“把百度网盘里的 XX 做成 skill”
“把夸克网盘这个课程学习掉并蒸馏”
“处理这个本地 PDF，做成 Cangjie Skill”
“继续刚才那个知识摄取任务”
“刚才那个课程处理到哪一步了”
```

- [ ] **Step 2: 总控主流程写成严格决策树**

```text
1. doctor
2. 查找可恢复 Job
3. create/resume
4. source acquire/register
5. route
6. preprocess
7. 确认 docchunk verify PASS
8. next
9. invoke target skill
10. gate enter / 用户确认 / gate resolve
11. target complete
12. next
13. final report
```

- [ ] **Step 3: 明确绝对禁止行为**

```text
禁止重新实现 OCR/ASR/chunking
禁止 verify FAIL 后蒸馏
禁止伪造用户确认
禁止读取云盘 Token 配置
禁止自动删除/移动云端源文件
禁止把百度能力范围描述成整个个人网盘
禁止用 Quark 5 条 preview 代替完整 Artifact
禁止把多课程粗暴拼成一个失去来源边界的 Markdown
```

- [ ] **Step 4: 全局安装使用实体库 symlink**

**（V1.1 修订，遵 AGENTS.md §19）** 本机唯一 Skill 实体库是 `~/.agents/skills/`，
`~/.zcode/skills`、`~/.claude/skills`、`~/.codex/skills` 是 symlink 视图（skill-sync 管理）。
因此：

```bash
ln -sfn /Volumes/ORICO/Projects/knowledge-ingest ~/.agents/skills/knowledge-ingest
~/.local/bin/skill-sync        # 体检；视图缺链接时用 skill-sync fix
```

**禁止**直接向视图目录复制或链接实体内容；**禁止**复制两份源码。

- [ ] **Step 5: CLI 安装**

```bash
cd /Volumes/ORICO/Projects/knowledge-ingest
uv sync
uv run knowledge-ingest doctor --config ./config.yaml
```

可选创建一个本机 wrapper 到 `~/.local/bin/knowledge-ingest`，但 wrapper 只执行：

```bash
#!/bin/zsh
cd /Volumes/ORICO/Projects/knowledge-ingest || exit 1
exec uv run knowledge-ingest "$@"
```

- [ ] **Step 6: 提交**

```bash
git add .
git commit -m "docs: finalize knowledge-ingest orchestration skill"
```

---

### Task 15: V1 端到端验收

**Files:**
- Create: `docs/acceptance-v1.md`
- No core code changes unless a test exposes a defect.

**Interfaces:**
- Validates the full design specification.

- [ ] **Case A：Local PDF → Cangjie**

```text
Local PDF
→ route=document
→ docchunk split
→ verify PASS
→ CORPUS_READY
→ Cangjie
→ 人工确认门
→ complete
```

验收：不复制本地原 PDF；Job 可恢复。

- [ ] **Case B：Local MP4 → Cangjie**

```text
MP4
→ media-transcriber
→ Markdown + metadata
→ docchunk
→ verify PASS
→ Cangjie
```

验收：第二次同配置运行命中 transcript/cache，不重复 ASR。

- [ ] **Case C：Quark 混合课程 → 双蒸馏**

目录：

```text
01.mp4
02.mp4
03.pdf
04.docx
```

验收：
- 下载完整；
- 2 个 media 转写；
- handoff Document Set 保持每个文件身份；
- 只有一个 verified Corpus；
- 先 Cangjie 后 Personal；
- 两者复用同一个 Corpus。

- [ ] **Case D：断点恢复**

在 Cangjie `WAITING_USER` 时退出 Agent 会话。新会话执行：

```bash
knowledge-ingest status JOB
knowledge-ingest next JOB --json
```

验收：next=ask_user；不重新下载、不转写、不 docchunk。

- [ ] **Case E：跨来源同 PDF 去重**

Quark 和 Baidu app/share-link 下载得到 SHA-256 相同 PDF。

验收：第二个 Job 复用 verified Corpus，但两份 Job 各自保留 provider provenance。

- [ ] **Case F：媒体失败阻断**

混合课程中故意让一个媒体转写失败。

验收：总体 `BLOCKED` 或 `PARTIAL`；不得进入最终蒸馏；报告列出失败文件。

- [ ] **Case G：DocChunk verify 失败阻断**

使用可控 fixture/mock 让 verify exit=1。

验收：`docchunk.verify=FAIL`、总体 `BLOCKED`、`next_action` 不得返回任何 distillation target。

- [ ] **Case H：百度普通网盘范围限制**

用户要求读取 `/apps/bdpan` 外部未提供分享链接的资料。

验收：`BLOCKED: baidu_scope_limited`，给出"移动到我的应用数据/bdpan"或"提供分享链接"两种恢复路径。

- [ ] **Case I：敏感信息扫描**

```bash
rg -n -i 'access[_-]?token|refresh[_-]?token|cookie|authorization|password|secret' \
  /Volumes/ORICO/KnowledgePipeline/jobs/<test-job>
```

验收：不存在真实凭据；允许出现字段名或 `[REDACTED]`。

- [ ] **Case J：完整测试套件**

```bash
cd /Volumes/ORICO/Projects/knowledge-ingest
uv run pytest -q
uv run knowledge-ingest doctor --config ./config.yaml
```

Expected：pytest PASS；Doctor 无 FAIL。

- [ ] **Step 11: 最终提交 Tag**

```bash
git status --short
git add docs/acceptance-v1.md
git commit -m "test: record knowledge-ingest v0.1.0 acceptance"
git tag v0.1.0
git log --oneline -5
```

Tag 必须指向包含最终验收记录的 commit。

---

## 上层 Skill 与 Python CLI 的职责边界

### Python CLI 负责

```text
Job Manifest
原子状态写入
本地文件路由
SHA-256 指纹
本地/下载结果注册
media-transcriber 调用
DocChunk 调用
Corpus verify
cache / dedup
mixed collection handoff
status / next-action
gate state
final report
```

### Agent Skill 负责

```text
理解用户自然语言
调用 baidu-drive / quarkclouddrive
选择云端具体文件
将下载结果注册进 Job
调用 cangjie-skill
调用 personal-capability-distiller
将原 Skill 的确认问题展示给用户
收到真实确认后更新 Job gate
```

这个边界是 V1 的关键：**不要让 Python 代码变成另一个 Agent，也不要让 SKILL.md 自己承担持久状态数据库的职责。**

---

## Job Manifest 建议最终形态

```yaml
schema_version: 1
job_id: 20260905-233500-quark-guarantee-course
created_at: 2026-09-05T23:35:00+08:00
updated_at: 2026-09-05T23:48:00+08:00
status: WAITING_USER

request:
  raw_prompt: 把夸克网盘审计课程/融资担保里的资料做成skill并蒸馏
  provider: quark
  source: /审计课程/融资担保
  targets: [cangjie, personal]

source:
  provider: quark
  local_path: /Volumes/ORICO/KnowledgePipeline/jobs/20260905-233500-quark-guarantee-course/source/融资担保
  source_fingerprint: sha256:...
  remote:
    id: provider-id
    path: /审计课程/融资担保
    name: 融资担保
  download_completed: true

routing:
  collection: true
  documents: 4
  media: 8
  unsupported: 0

media:
  status: success
  started_at: 2026-09-05T23:36:00+08:00
  completed_at: 2026-09-05T23:42:00+08:00
  outputs:
    - source_relative_path: 01.mp4
      source_sha256: sha256:...
      transcript: /Volumes/ORICO/MediaTranscriber/output/.../01.md
      transcript_sha256: sha256:...
      metadata: /Volumes/ORICO/MediaTranscriber/output/.../metadata.yaml

docchunk:
  status: verified
  corpus_path: /Volumes/ORICO/LongDocCorpus/...
  verify: PASS
  cache_key: sha256:...

cangjie:
  status: waiting_user
  waiting_for: stage1_5_candidates
  pipeline_state: /Volumes/ORICO/KnowledgePipeline/distill/<job>/cangjie/books/<slug>/PIPELINE_STATE.md
  output_path: null

personal:
  status: pending
  waiting_for: null
  output_path: null

errors: []
```

---

## 推荐 CLI 表面

```bash
knowledge-ingest doctor --config ./config.yaml
knowledge-ingest job create --provider local|baidu|quark --source "..." --target cangjie --target personal
knowledge-ingest source register JOB --handoff source.json
knowledge-ingest route JOB
knowledge-ingest preprocess JOB
knowledge-ingest status JOB
knowledge-ingest status JOB --json
knowledge-ingest next JOB --json
knowledge-ingest gate enter JOB --target cangjie|personal --name GATE
knowledge-ingest gate resolve JOB --target cangjie|personal --name GATE --decision confirmed|rejected
knowledge-ingest target complete JOB --target cangjie|personal --output-path PATH
knowledge-ingest report JOB
```

`preprocess` 的边界固定为：**Source Ready → Route → Media（必要时）→ Document Set → DocChunk → Verify → CORPUS_READY**。它永远不自动通过下游蒸馏 Gate。

---

## 不采用的实现方式

1. **不让 knowledge-ingest 直接 import 另外四个项目的 Python 内部模块。** 使用公开 CLI/Skill 边界，降低升级耦合。
2. **不建 SQLite 总状态库。** V1 单任务串行，Job YAML + cache JSON 足够；media-transcriber 自己的 SQLite 不复制。
3. **不把所有云盘 CLI 语法写进 Python Adapter。** 云盘由官方 Skill 自己维护版本差异。
4. **不把转写、PDF、Chunk 产物再复制一份进 Job。** Job 只保存路径引用和 provenance。
5. **不把 Cangjie 与 Personal 并行跑。** 先保证确认门、断点与上下文稳定。
6. **不自动忽略失败文件。** 完整课程默认全量硬门；只有用户明确排除才继续。

---

## 实施顺序与 Reviewer Gate

严格按以下顺序：

```text
Task 1  Scaffold/Doctor
  ↓ review
Task 2  Manifest/State Machine
  ↓ review
Task 3  Source/Router/Fingerprint
  ↓ review
Task 4  DocChunk Happy Path
  ↓ LOCAL PDF 可用
Task 5  Media
  ↓ LOCAL MP4 可用
Task 6  Mixed Collection
  ↓ 混合课程本地可用
Task 7  Cache/Dedup
  ↓ 幂等可用
Task 8  Resume/Gates
  ↓ 可中断恢复
Task 9  Cangjie
Task 10 Personal
  ↓ 双蒸馏闭环
Task 11 Baidu
Task 12 Quark
  ↓ 云端 Source 完整
Task 13 Report
Task 14 Skill 安装
Task 15 Acceptance
```

**不要先做百度/夸克。** 先证明 Local PDF 和 Local MP4 的 Job/恢复/verify 架构正确，否则网盘问题会掩盖总控自身问题。

---

## 给 Mac Agent 的执行要求

实施时必须在独立 worktree 或独立新仓库中进行。每个 Task：

```text
写失败测试
→ 跑到 FAIL
→ 写最小实现
→ 跑到 PASS
→ 跑相关回归
→ reviewer gate
→ commit
```

发现现有仓库 CLI 与本文档不一致时，优先以本机当前 `--help` / README / SKILL.md 为准，但必须满足本文档定义的 **handoff contract、状态门、verify 硬门、安全边界和不修改现有核心项目**。接口差异只能收敛在 Adapter/Skill 层，不得侵入 Manifest 核心。

**（V1.1 新增）本机全局规则约束（~/.codex/AGENTS.md）：**

- §13：实施/调试/测试/重构/评审代码前，载入 claude-worker-router 并按其路由策略执行。
- §17：docchunk split 长任务后台运行并轮询进度，不同步阻塞；`docchunk verify` PASS 是下游消费硬门；`overlap_atomic_ids` 只作上下文桥。
- §19：Skill 一律装入实体库 `~/.agents/skills/`，视图由 skill-sync 管理，绝不复制实体内容到视图目录。
- §4：Git identity hg199074jin，默认分支 main；本文档各 Task 的 commit 属任务明确要求。
