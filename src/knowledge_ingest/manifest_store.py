"""Durable, atomically written Job Manifest store."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from knowledge_ingest.models import JobManifest, JobRequest


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32] or "job"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ManifestStore:
    def __init__(self, jobs_root: Path) -> None:
        self.jobs_root = Path(jobs_root)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    @property
    def manifest_path_pattern(self) -> str:
        return "job.yaml"

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.yaml"

    def create(self, request: JobRequest) -> JobManifest:
        now = _now()
        stem = Path(request.source.replace("\\", "/")).name or "source"
        job_id = f"{now:%Y%m%d-%H%M%S}-{request.provider}-{_slugify(stem)}"
        manifest = JobManifest(
            job_id=job_id, created_at=now, updated_at=now, request=request
        )
        job_dir = self.job_dir(job_id)
        (job_dir / "source").mkdir(parents=True, exist_ok=True)
        (job_dir / "handoff" / "document-set").mkdir(parents=True, exist_ok=True)
        (job_dir / "reports").mkdir(parents=True, exist_ok=True)
        (job_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.save(manifest)
        return manifest

    def load(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"job not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return JobManifest.model_validate(data)

    def save(self, manifest: JobManifest) -> None:
        manifest.updated_at = _now()
        payload = manifest.model_dump(mode="json")
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        atomic_write_text(self.manifest_path(manifest.job_id), text)

    def list_jobs(self) -> list[str]:
        if not self.jobs_root.is_dir():
            return []
        return sorted(
            p.parent.name
            for p in self.jobs_root.glob("*/job.yaml")
        )
