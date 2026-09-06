"""Final reports, human status, and redacted event logs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from knowledge_ingest.models import JobManifest

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


def build_status(manifest: JobManifest) -> dict:
    media_out = len(manifest.media.outputs)
    media_total = int(manifest.routing.get("media") or 0) or media_out
    source_status = manifest.source.get("download_completed")
    transcribe = (f"{manifest.media.status} {media_out}/{media_total}"
                  if manifest.media.status != "pending" else "skipped")
    docchunk = (f"{manifest.docchunk.status}"
                + (f" {manifest.docchunk.verify}"
                   if manifest.docchunk.verify else ""))
    cangjie = manifest.cangjie.status + (
        f": {manifest.cangjie.waiting_for}"
        if manifest.cangjie.waiting_for else "")
    personal = manifest.personal.status + (
        f": {manifest.personal.waiting_for}"
        if manifest.personal.waiting_for else "")
    return {
        "job_id": manifest.job_id,
        "source": ("success" if source_status else
                   manifest.source.get("provider", "pending")),
        "transcribe": transcribe,
        "docchunk": docchunk,
        "cangjie": cangjie,
        "personal": personal,
        "overall": manifest.status,
        "errors": redact_deep(manifest.errors),
    }


def render_report(manifest: JobManifest) -> str:
    source = redact_deep(manifest.source)
    routing = redact_deep(manifest.routing)
    lines: list[str] = []
    lines.append(f"# Knowledge Ingest 报告 — {manifest.job_id}")
    lines.append("")
    lines.append(f"- 状态：{manifest.status}")
    lines.append(f"- 请求：{manifest.request.raw_prompt}")
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
    lines.append("## Cangjie 输出")
    lines.append(f"- 状态：{manifest.cangjie.status}")
    lines.append(f"- 产物：{manifest.cangjie.output_path}")
    lines.append(f"- 断点文件：{manifest.cangjie.pipeline_state}")
    lines.append("")
    lines.append("## Personal 输出")
    lines.append(f"- 状态：{manifest.personal.status}")
    lines.append(f"- 产物：{manifest.personal.output_path}")
    lines.append("")
    lines.append("## 显式排除项")
    excluded = routing.get("excluded") or []
    if excluded:
        for item in excluded:
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
    lines.append("## 可恢复信息")
    status = build_status(manifest)
    for key in ("source", "transcribe", "docchunk", "cangjie", "personal",
                "overall"):
        lines.append(f"- {key}: {status[key]}")
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
    path.write_text(render_report(manifest), encoding="utf-8")
    return path


class EventLog:
    """Append-only structured event log; sensitive values are redacted."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def log(self, event: str, job_id: str | None = None, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "event": event,
            "job_id": job_id,
            **redact_deep(fields),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
