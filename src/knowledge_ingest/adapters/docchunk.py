"""DocChunk adapter: split / verify / status through the public CLI only."""

from __future__ import annotations

import re
from pathlib import Path

from knowledge_ingest.runner import CommandResult, run_checked

ABSOLUTE_PATH = re.compile(r"(/Volumes/[^\s'\"]+|/Users/[^\s'\"]+|/private/var/[^\s'\"]+|/tmp[^\s'\"]*)")


class CorpusPathNotFound(Exception):
    pass


def extract_absolute_paths(text: str) -> list[Path]:
    seen: list[Path] = []
    for raw in ABSOLUTE_PATH.findall(text or ""):
        candidate = Path(raw.rstrip(".,:;)"))
        if candidate not in seen:
            seen.append(candidate)
    return seen


def resolve_corpus_path(result: CommandResult) -> Path:
    candidates = extract_absolute_paths(result.stdout + "\n" + result.stderr)
    for path in reversed(candidates):
        if (path / "manifest.json").is_file() and (path / "index.jsonl").is_file():
            return path.resolve()
    raise CorpusPathNotFound(
        "no directory containing manifest.json and index.jsonl found in output"
    )


class DocchunkAdapter:
    def __init__(self, project: Path) -> None:
        self.project = Path(project)

    def _base(self) -> list[str]:
        return ["uv", "run", "docchunk"]

    def doctor(self, timeout: int = 300) -> CommandResult:
        return run_checked(self._base() + ["doctor"], cwd=self.project,
                           timeout=timeout)

    def head(self, timeout: int = 60) -> str:
        result = run_checked(["git", "rev-parse", "HEAD"], cwd=self.project,
                             timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError("cannot resolve docchunk git HEAD")
        return result.stdout.strip()

    def split(
        self,
        input_path: Path,
        corpus_root: Path | None = None,
        timeout: int | None = None,
    ) -> Path:
        argv = self._base() + ["split", str(Path(input_path).resolve())]
        if corpus_root is not None:
            argv += ["--corpus-root", str(Path(corpus_root).resolve())]
        result = run_checked(argv, cwd=self.project, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(
                f"docchunk split failed (exit {result.returncode}): "
                f"{result.stderr.strip()[-500:] or result.stdout.strip()[-500:]}"
            )
        return resolve_corpus_path(result)

    def verify(self, corpus: Path, timeout: int = 300) -> bool:
        result = run_checked(
            self._base() + ["verify", str(Path(corpus).resolve())],
            cwd=self.project, timeout=timeout,
        )
        return result.returncode == 0

    def status(self, corpus: Path, timeout: int = 120) -> CommandResult:
        return run_checked(self._base() + ["status", str(Path(corpus).resolve())],
                           cwd=self.project, timeout=timeout)
