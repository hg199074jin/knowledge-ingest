"""Final reports, human status, and redacted event logs."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from knowledge_ingest.models import JobManifest
from knowledge_ingest.router import effective_paths

REDACT_PATTERN = re.compile(
    r"(token|cookie|authorization|auth[_-]?code|password|secret|"
    r"access[_-]?token|refresh[_-]?token)\s*[:=]\s*(\S+)",
    re.IGNORECASE,
)
REDACT_KEYS = re.compile(
    r"token|cookie|authorization|auth[_-]?code|password|secret",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    return REDACT_PATTERN.sub(lambda m: f"{m.group(1)}{REDACTED}", text or "")


def redact_deep(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if REDACT_KEYS.search(str(key)):
                out[key] = REDACTED
            else:
                out[key] = redact_deep(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_deep(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _target_line(state) -> str:
    line = state.status
    if state.reason:
        line += f" ({state.reason})"
    if state.waiting_for:
        line += f": {state.waiting_for}"
    return line


# ---------- v0.3 D1：运行审计段（数据全部来自 manifest 已有字段） ----------


def _iso(ts) -> str:
    return (ts.isoformat(timespec="seconds")
            if isinstance(ts, datetime) else str(ts))


def _fmt_duration(started: datetime, completed: datetime) -> str:
    total = int((completed - started).total_seconds())
    total = max(total, 0)  # 时钟异常时钳为 0，不出负时长
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _stage_duration_line(label: str, stage) -> str:
    """started_at↔completed_at 耗时行；缺时间戳则如实标注，
    绝不用 created_at 冒充（v1 迁移 manifest 走 unknown 分支）。"""
    started, completed = stage.started_at, stage.completed_at
    if started is not None and completed is not None:
        return (f"- {label}：{_fmt_duration(started, completed)}"
                f"（started {_iso(started)} ↔ completed {_iso(completed)}）")
    if started is not None:
        return f"- {label}：完成时间未记录（started {_iso(started)}）"
    if completed is not None:
        return f"- {label}：无开始时间戳（completed {_iso(completed)}）"
    return f"- {label}：unknown / not instrumented"


def _transcript_counts(manifest: JobManifest) -> dict[str, int]:
    """规格 15：按 outputs[].outcome 分组；未知值一律归 unknown（禁反推）。"""
    counts = {"fresh": 0, "cache_reused": 0, "unknown": 0}
    for out in manifest.media.outputs:
        if out.outcome == "transcribed":
            counts["fresh"] += 1
        elif out.outcome in counts:
            counts[out.outcome] += 1
        else:
            counts["unknown"] += 1
    return counts


def _budget_max(job_dir: Path | None, target: str) -> int:
    """预算上限与 acquire 同源：handoff/target-<t>.yaml（缺省回退默认值）；
    manifest 本身不存上限，读取失败时同样回退，绝不让报告崩掉。"""
    if job_dir is not None:
        # 静默回退默认值是有意为之：报告渲染不允许因预算配置崩掉
        # （跳过 S110/BLE001——此处吞掉一切异常是为了保住报告输出）。
        try:
            from knowledge_ingest.budget import read_budget_config

            return int(read_budget_config(job_dir, target)
                       ["max_external_calls"])
        except Exception:  # noqa: S110, BLE001
            pass
    from knowledge_ingest.budget import DEFAULT_BUDGET

    return int(DEFAULT_BUDGET["max_external_calls"])


def _render_audit(manifest: JobManifest, lines: list[str],
                  job_dir: Path | None = None) -> None:
    lines.append("## 运行审计")
    lines.append("")
    lines.append("### 阶段耗时")
    acquired = manifest.source.get("acquired")
    if acquired:
        lines.append(f"- source：acquired {_iso(acquired)}")
    else:
        lines.append("- source：unknown / not instrumented")
    lines.append(_stage_duration_line("media", manifest.media))
    lines.append(_stage_duration_line("docchunk", manifest.docchunk))
    for name, state in manifest.targets.items():
        lines.append(_stage_duration_line(name, state))
    lines.append("")
    lines.append("### 转写口径")
    counts = _transcript_counts(manifest)
    lines.append(f"- fresh {counts['fresh']} / cache_reused "
                 f"{counts['cache_reused']} / unknown {counts['unknown']}"
                 f"（unknown=V1 历史，禁反推——规格 15）")
    lines.append("")
    lines.append("### 预算")
    entries = manifest.budget_state.get("targets") or {}
    if not entries:
        lines.append("- no external calls recorded")
    else:
        for name, entry in entries.items():
            permits = entry.get("permits") or {}
            pending = sum(1 for permit in permits.values()
                          if permit.get("outcome") is None)
            limit = _budget_max(job_dir, name)
            lines.append(f"- {name}: calls {entry.get('calls', 0)}/{limit}, "
                         f"permits {len(permits)}, "
                         f"pending outcome {pending}")
    lines.append("")
    lines.append("### 工具版本")
    lines.append("- recorded at preprocess (v0.3 起)；manifest 无现成字段，"
                 "本报告不列具体版本")
    for name, state in manifest.targets.items():
        scores = getattr(state, "scores", None)  # 模型无该字段时静默省略
        if scores:
            lines.append(f"- 评测分数 {name}：{scores}")
    lines.append("")


def build_status(manifest: JobManifest) -> dict:
    media_out = len(manifest.media.outputs)
    # 分母三级回退：v0.3 effective → v0.2 media_paths → v0.2 计数键
    media_total = (len(effective_paths(manifest.routing, "media"))
                   or len(manifest.routing.get("media_paths") or [])
                   or int(manifest.routing.get("media") or 0)
                   or media_out)
    source_status = manifest.source.get("download_completed")
    transcribe = (f"{manifest.media.status} {media_out}/{media_total}"
                  if manifest.media.status != "pending" else "skipped")
    docchunk = (f"{manifest.docchunk.status}"
                + (f" {manifest.docchunk.verify}"
                   if manifest.docchunk.verify else ""))
    # v0.3：per-target 分组行（声明顺序），保留 cangjie/personal 兼容键
    targets = {name: _target_line(state)
               for name, state in manifest.targets.items()}
    status = {
        "job_id": manifest.job_id,
        "source": ("success" if source_status else
                   manifest.source.get("provider", "pending")),
        "transcribe": transcribe,
        "docchunk": docchunk,
        "targets": targets,
        "overall": manifest.status,
        "errors": redact_deep(manifest.errors),
    }
    for legacy in ("cangjie", "personal"):
        if legacy in targets:
            status[legacy] = targets[legacy]
    return status


def render_report(manifest: JobManifest, job_dir: Path | None = None) -> str:
    source = redact_deep(manifest.source)
    routing = redact_deep(manifest.routing)
    lines: list[str] = []
    lines.append(f"# Knowledge Ingest 报告 — {manifest.job_id}")
    lines.append("")
    lines.append(f"- 状态：{manifest.status}")
    lines.append(f"- 请求：{redact_text(manifest.request.raw_prompt)}")
    lines.append(f"- 目标：{', '.join(manifest.request.targets)}")
    lines.append("")
    lines.append("## 来源")
    lines.append(f"- provider：{source.get('provider')}")
    lines.append(f"- 本地源路径：{source.get('local_path')}")
    lines.append(f"- 源指纹：{source.get('source_fingerprint')}")
    if source.get("remote"):
        lines.append(f"- 云端路径：{source['remote'].get('path')}")
    lines.append("")
    lines.append("## 路由统计")
    eff = routing.get("effective")
    if eff:
        lines.append(f"- collection：{routing.get('collection')}")
        for t in ("documents", "media", "unsupported"):
            disc = len((routing.get("discovered") or {}).get(t) or [])
            exc = len((routing.get("excluded") or {}).get(t) or [])
            lines.append(f"- {t}：发现 {disc} / 排除 {exc} / 进入 {len(eff.get(t) or [])}")
    else:
        lines.append(f"- collection：{routing.get('collection')}")
        lines.append(f"- documents：{routing.get('documents')}")
        lines.append(f"- media：{routing.get('media')}")
        lines.append(f"- unsupported：{routing.get('unsupported')}")
    lines.append("")
    lines.append("## 转写产物")
    if manifest.media.outputs:
        for out in manifest.media.outputs:
            lines.append(f"- {out.source_relative_path} → {out.transcript}"
                         f"（sha256 {out.transcript_sha256[:19]}…）")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## Corpus 与 verify")
    lines.append(f"- corpus_path：{manifest.docchunk.corpus_path}")
    lines.append(f"- verify：{manifest.docchunk.verify}")
    lines.append(f"- 复用缓存：{manifest.docchunk.reused}")
    lines.append(f"- cache_key：{manifest.docchunk.cache_key}")
    lines.append("")
    # v0.3：Cangjie/Personal 固定段改为 per-target 循环（display_name）
    from knowledge_ingest.targets import REGISTRY

    lines.append("## Targets")
    for name, state in manifest.targets.items():
        runtime = REGISTRY.get(name)
        label = runtime.display_name if runtime else name
        lines.append(f"### {label}")
        status_line = state.status
        if state.reason:
            status_line += f"（{state.reason}）"
        if state.waiting_for:
            status_line += f"：等待 {state.waiting_for}"
        lines.append(f"- 状态：{status_line}")
        lines.append(f"- 产物：{state.output_path}")
        if state.pipeline_state:
            lines.append(f"- 断点文件：{state.pipeline_state}")
        if state.output_manifest:
            lines.append(f"- 输出清单：{state.output_manifest}")
        if state.depends_on:
            lines.append(f"- 依赖：{', '.join(state.depends_on)}")
        lines.append("")
    completed_outputs = {name: state.output_path
                         for name, state in manifest.targets.items()
                         if state.output_path}
    lines.append("## target_outputs")
    if completed_outputs:
        for name, out in completed_outputs.items():
            lines.append(f"- {name}: {out}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 显式排除项")
    excluded = routing.get("excluded")
    excl_items: list = []
    if isinstance(excluded, dict):
        for t in ("documents", "media", "unsupported"):
            excl_items.extend(excluded.get(t) or [])
    elif isinstance(excluded, list):
        excl_items = excluded
    if excl_items:
        for item in excl_items:
            lines.append(f"- {item}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 失败/警告")
    if manifest.errors:
        for err in redact_deep(manifest.errors):
            lines.append(f"- {err}")
    else:
        lines.append("- 无")
    lines.append("")
    _render_audit(manifest, lines, job_dir=job_dir)
    lines.append("## 可恢复信息")
    status = build_status(manifest)
    for key in ("source", "transcribe", "docchunk", "overall"):
        lines.append(f"- {key}: {status[key]}")
    for name, line in status["targets"].items():
        lines.append(f"- target:{name}: {line}")
    if manifest.gate_history:
        lines.append("- gate 历史：")
        for gate in manifest.gate_history:
            lines.append(f"  - {gate}")
    lines.append("")
    return "\n".join(lines)


def write_report(manifest: JobManifest, job_dir: Path) -> Path:
    reports = Path(job_dir) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "final.md"
    path.write_text(render_report(manifest, job_dir=Path(job_dir)),
                    encoding="utf-8")
    return path


class EventLog:
    """Append-only structured event log; sensitive values are redacted."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def log(self, event: str, job_id: str | None = None, **fields) -> None:
        record = {
            "ts": datetime.now(UTC).astimezone().isoformat(),
            "event": event,
            "job_id": job_id,
            **redact_deep(fields),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # v0.3 A2：崩溃/SIGKILL 后事件不丢（append + flush + fsync）
            f.flush()
            os.fsync(f.fileno())
