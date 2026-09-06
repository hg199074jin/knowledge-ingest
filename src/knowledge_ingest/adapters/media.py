"""Media adapter: transcription via media-transcriber public CLI."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from knowledge_ingest.cache import build_transcript_cache_key
from knowledge_ingest.fingerprint import fingerprint_file
from knowledge_ingest.runner import run_checked


class TranscriptResult(BaseModel):
    source_path: Path
    markdown_path: Path
    metadata_path: Path
    source_sha256: str
    transcript_sha256: str
    cache_key: str


class MediaAdapter:
    def __init__(self, project: Path, output_root: Path) -> None:
        self.project = Path(project)
        self.output_root = Path(output_root)

    def head(self, timeout: int = 60) -> str:
        result = run_checked(["git", "rev-parse", "HEAD"], cwd=self.project,
                             timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError("cannot resolve media-transcriber git HEAD")
        return result.stdout.strip()

    def _file_sha(self, path: Path) -> str | None:
        if path is not None and Path(path).is_file():
            return fingerprint_file(path)
        return None

    def transcribe(
        self,
        path: Path,
        device: str = "auto",
        timestamp: str = "10m",
        glossary: str | None = None,
        hotwords: str | None = None,
        config_file: Path | None = None,
        timeout: int | None = None,
    ) -> TranscriptResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"media not found: {source}")

        argv = ["uv", "run", "media-transcriber", "transcribe", str(source),
                "--device", device, "--timestamp", timestamp]
        if glossary:
            argv += ["--glossary", glossary]
        if hotwords:
            argv += ["--hotword", hotwords]
        if config_file is not None:
            argv += ["--config", str(config_file)]

        result = run_checked(argv, cwd=self.project, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
                f"media-transcriber failed (exit {result.returncode}): "
                f"{result.stderr.strip()[-500:] or result.stdout.strip()[-500:]}"
            )

        stem = source.stem
        markdown_path = self.output_root / stem / f"{stem}.md"
        metadata_path = self.output_root / stem / "metadata.yaml"
        # 只接受 .md + 同目录 metadata.yaml 成对出现
        if not markdown_path.is_file() or not metadata_path.is_file():
            raise RuntimeError(
                f"transcript outputs incomplete: expected {markdown_path} "
                f"and {metadata_path}"
            )

        source_sha = fingerprint_file(source)
        key = build_transcript_cache_key(
            source_sha256=source_sha,
            mt_head=self.head(),
            config_sha=self._file_sha(config_file),
            device=device,
            timestamp=timestamp,
            glossary=glossary,
            hotwords=hotwords,
        )
        return TranscriptResult(
            source_path=source,
            markdown_path=markdown_path,
            metadata_path=metadata_path,
            source_sha256=source_sha,
            transcript_sha256=fingerprint_file(markdown_path),
            cache_key=key,
        )
