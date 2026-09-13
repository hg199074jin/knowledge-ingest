"""Job Manifest data models (schema_version 2, generic target runtime).

v0.3 Part C（冻结规格 2/8/12/13）：
- OverallStatus: DISTILLING_CANGJIE/DISTILLING_PERSONAL → TARGET_RUNNING
- TargetStatus: target 级状态机（终态 COMPLETED/SKIPPED/FAILED；BLOCKED 可恢复）
- JobManifest.targets: dict[str, TargetState]（保持声明顺序）
- V1→V2 迁移：model_validator(mode=before) 原地改写旧结构
- 交叉不变式（规格 8）与依赖图校验（unknown dep/self dep/环）在此层强制
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from knowledge_ingest.targets import REGISTRY

OverallStatus = Literal[
    "CREATED", "DISCOVERING", "DOWNLOADING", "DOWNLOADED", "ROUTING",
    "TRANSCRIBING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
    "TARGET_RUNNING", "WAITING_USER",
    "COMPLETED", "PARTIAL", "BLOCKED", "FAILED",
]

# 规格 2：target 级状态。终态 = COMPLETED/SKIPPED/FAILED（不隐式复活）；
# BLOCKED = 可恢复非终态；依赖满足 ⇔ dep.status == COMPLETED（唯一词）。
TargetStatus = Literal[
    "PENDING", "READY", "RUNNING", "WAITING_USER",
    "COMPLETED", "SKIPPED", "FAILED", "BLOCKED",
]

TERMINAL_TARGET_STATUSES = frozenset(
    {"COMPLETED", "SKIPPED", "FAILED"})
ACTIVE_TARGET_STATUSES = frozenset({"RUNNING", "WAITING_USER"})

TargetName = str  # v0.3：合法名由 targets.REGISTRY 校验（CLI choices / get()）
ProviderName = Literal["local", "baidu", "quark"]

# v1 小写 target 状态字符串 → v2 枚举值（旧字符串原样映射，未知值 fail-fast）
_LEGACY_TARGET_STATUS = {
    "PENDING": "PENDING",
    "READY": "READY",
    "RUNNING": "RUNNING",
    "WAITING_USER": "WAITING_USER",
    "SUCCESS": "COMPLETED",
    "COMPLETED": "COMPLETED",
    "SKIPPED": "SKIPPED",
    "FAILED": "FAILED",
    "BLOCKED": "BLOCKED",
}


def _map_legacy_target_status(value: str) -> str:
    mapped = _LEGACY_TARGET_STATUS.get(str(value).strip().upper())
    if mapped is None:
        raise ValueError(
            f"cannot migrate legacy target status: {value!r}")
    return mapped


class JobRequest(BaseModel):
    raw_prompt: str
    provider: ProviderName
    source: str
    targets: list[str]


class StageState(BaseModel):
    status: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class MediaOutput(BaseModel):
    source_relative_path: str
    source_sha256: str
    transcript: Path
    transcript_sha256: str
    metadata: Path
    cache_key: str | None = None
    # v0.3 规格：transcribed | cache_reused | unknown
    # （V1 历史 manifest 读入即 unknown，禁反推）
    outcome: str = "unknown"


class MediaState(StageState):
    outputs: list[MediaOutput] = Field(default_factory=list)


class DocchunkState(StageState):
    corpus_path: Path | None = None
    verify: Literal["PASS", "FAIL"] | None = None
    cache_key: str | None = None
    reused: bool | None = None


class TargetState(StageState):
    status: str = "PENDING"  # TargetStatus 枚举值
    waiting_for: str | None = None
    output_path: Path | None = None
    pipeline_state: Path | None = None
    depth: str | None = None
    # 规格 2：依赖图（顺序即声明顺序；依赖满足 ≠ 运行排序）
    depends_on: list[str] = Field(default_factory=list)
    # SKIPPED 原因：user_rejected | dependency_skipped | dependency_failed；
    # BLOCKED 原因：budget_exhausted | breaker_open | case_retry_exceeded
    reason: str | None = None
    # 规格 15：checkpoint（不做 heartbeat）
    checkpoint_path: Path | None = None
    evidence_dir: Path | None = None
    checkpoint_at: datetime | None = None
    attempt: int | None = None
    # target complete 时登记的 output manifest 路径
    output_manifest: Path | None = None


class JobManifest(BaseModel):
    schema_version: int = 2
    job_id: str
    created_at: datetime
    updated_at: datetime
    status: OverallStatus = "CREATED"
    request: JobRequest
    source: dict = Field(default_factory=dict)
    routing: dict = Field(default_factory=dict)
    media: MediaState = Field(default_factory=MediaState)
    docchunk: DocchunkState = Field(default_factory=DocchunkState)
    # 规格 2：targets dict 保持声明顺序（pydantic dict 保插入序）
    targets: dict[str, TargetState] = Field(default_factory=dict)
    active_target: str | None = None
    # 规格 9：budget_state = {targets: {<name>: {calls, cases, hosts, permits}}}
    budget_state: dict = Field(default_factory=dict)
    # 规格 10：gate 预授权 grant 列表
    preauthorizations: list[dict] = Field(default_factory=list)
    gate_history: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)

    # ---------- V1 → V2 迁移 ----------

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1_to_v2(cls, data):
        if not isinstance(data, dict) or "targets" in data:
            return data  # 已是 v2（或非映射输入，交给字段校验报错）
        data = dict(data)
        overall = str(data.get("status") or "CREATED")
        active: str | None = None
        targets: dict = {}
        # 顶层 cangjie/personal dict → targets dict（保持 cangjie, personal 顺序）
        for name in ("cangjie", "personal"):
            raw = data.pop(name, None)
            if raw is None:
                continue
            state = dict(raw) if isinstance(raw, dict) else {"status": raw}
            state["status"] = _map_legacy_target_status(
                state.get("status") or "pending")
            targets[name] = state
        if overall in {"DISTILLING_CANGJIE", "DISTILLING_PERSONAL"}:
            name = ("cangjie" if overall == "DISTILLING_CANGJIE"
                    else "personal")
            overall = "TARGET_RUNNING"
            state = targets.get(name, {})
            # v1 语义：DISTILLING_* 即该 target 是当前蒸馏阶段
            # （即便 state 尚为 pending——v1 链式推进时就已切阶段）
            if state.get("status") not in TERMINAL_TARGET_STATUSES:
                active = name
        if overall == "WAITING_USER":
            waiting = [n for n, s in targets.items()
                       if s.get("status") == "WAITING_USER"]
            if waiting:
                active = waiting[0]
        data["status"] = overall
        data["targets"] = targets
        data["schema_version"] = 2
        if active is not None:
            data.setdefault("active_target", active)
        return data

    # ---------- entry 自动补齐 + 依赖图 + 交叉不变式 ----------

    @model_validator(mode="after")
    def _validate_v2(self):
        self._ensure_target_entries()
        self._validate_dependency_graph()
        self._validate_cross_invariants()
        return self

    def _new_target_state(self, name: str) -> TargetState:
        rt = REGISTRY.get(name)
        return TargetState(depends_on=list(rt.depends_on) if rt else [])

    def _ensure_target_entries(self) -> None:
        self.ensure_target_entries()

    def ensure_target_entries(self) -> None:
        """requested target 必须有 entry；其直接 depends_on 自动补 entry
        （规格 4：不递归补、不自动启动）。供 validator 与 amend 复用。"""
        requested = list(dict.fromkeys(self.request.targets))
        for name in requested:
            if name not in self.targets:
                self.targets[name] = self._new_target_state(name)
        for name in requested:
            rt = REGISTRY.get(name)
            for dep in (rt.depends_on if rt else ()):
                if dep not in self.targets:
                    self.targets[dep] = self._new_target_state(dep)

    def _validate_dependency_graph(self) -> None:
        for name, state in self.targets.items():
            for dep in state.depends_on:
                if dep == name:
                    raise ValueError(f"self dependency: {name}")
                if dep not in self.targets:
                    raise ValueError(
                        f"unknown dependency: {name} -> {dep}")
        # 环检测（含 2-cycle / 3-cycle）：沿 depends_on 走回自身即拒绝
        for start in self.targets:
            self._walk_deps(start, {start})

    def _walk_deps(self, node: str, path: frozenset) -> None:
        for dep in self.targets[node].depends_on:
            if dep in path:
                raise ValueError(
                    f"dependency cycle detected: {' -> '.join((*path, dep))}")
            self._walk_deps(dep, path | {dep})

    def _validate_cross_invariants(self) -> None:
        # 规格 8：同时最多一个 RUNNING/WAITING_USER
        active = [name for name, state in self.targets.items()
                  if state.status in ACTIVE_TARGET_STATUSES]
        if len(active) > 1:
            raise ValueError(
                f"at most one RUNNING/WAITING_USER target allowed, "
                f"got {active}")
        if active:
            name = active[0]
            # RUNNING/WAITING_USER ⇒ active_target==本 target 且 Overall 对应
            if self.active_target != name:
                raise ValueError(
                    f"target {name} is {self.targets[name].status} but "
                    f"active_target={self.active_target!r}")
            expected = ("TARGET_RUNNING"
                        if self.targets[name].status == "RUNNING"
                        else "WAITING_USER")
            if self.status != expected:
                raise ValueError(
                    f"target {name} is {self.targets[name].status} but "
                    f"overall status is {self.status} (expected {expected})")
        if self.active_target is not None:
            state = self.targets.get(self.active_target)
            if state is None:
                raise ValueError(
                    f"active_target {self.active_target!r} has no target "
                    f"state")
            # 终态不得为 active_target
            if state.status in TERMINAL_TARGET_STATUSES:
                raise ValueError(
                    f"terminal target {self.active_target!r} "
                    f"({state.status}) must not be active_target")
            if active and self.active_target != active[0]:
                raise ValueError(
                    f"active_target {self.active_target!r} conflicts with "
                    f"active target {active[0]!r}")
