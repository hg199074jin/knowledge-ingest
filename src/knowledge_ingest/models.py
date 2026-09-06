"""Job Manifest data models (schema_version 1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

OverallStatus = Literal[
    "CREATED", "DISCOVERING", "DOWNLOADING", "DOWNLOADED", "ROUTING",
    "TRANSCRIBING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
    "DISTILLING_CANGJIE", "DISTILLING_PERSONAL", "WAITING_USER",
    "COMPLETED", "PARTIAL", "BLOCKED", "FAILED",
]

TargetName = Literal["cangjie", "personal"]
ProviderName = Literal["local", "baidu", "quark"]


class JobRequest(BaseModel):
    raw_prompt: str
    provider: ProviderName
    source: str
    targets: list[TargetName]


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


class MediaState(StageState):
    outputs: list[MediaOutput] = Field(default_factory=list)


class DocchunkState(StageState):
    corpus_path: Path | None = None
    verify: Literal["PASS", "FAIL"] | None = None
    cache_key: str | None = None
    reused: bool | None = None


class TargetState(StageState):
    waiting_for: str | None = None
    output_path: Path | None = None
    pipeline_state: Path | None = None
    depth: str | None = None


class JobManifest(BaseModel):
    schema_version: int = 1
    job_id: str
    created_at: datetime
    updated_at: datetime
    status: OverallStatus = "CREATED"
    request: JobRequest
    source: dict = Field(default_factory=dict)
    routing: dict = Field(default_factory=dict)
    media: MediaState = Field(default_factory=MediaState)
    docchunk: DocchunkState = Field(default_factory=DocchunkState)
    cangjie: TargetState = Field(default_factory=TargetState)
    personal: TargetState = Field(default_factory=TargetState)
    gate_history: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
