from pathlib import Path

import pytest

from knowledge_ingest.adapters.docchunk import (
    CorpusPathNotFound,
    DocchunkAdapter,
    resolve_corpus_path,
)
from knowledge_ingest.runner import CommandResult
from datetime import datetime, timezone

PROJECT = Path("/Volumes/ORICO/Projects/docchunk")


def make_result(stdout: str = "", stderr: str = "") -> CommandResult:
    now = datetime.now(timezone.utc)
    return CommandResult(argv=("docchunk",), returncode=0, stdout=stdout,
                         stderr=stderr, started_at=now, ended_at=now)


def make_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "demo-abc123"
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "manifest.json").write_text("{}", encoding="utf-8")
    (corpus / "index.jsonl").write_text("", encoding="utf-8")
    return corpus


def test_resolve_corpus_path_prefers_last_valid(tmp_path: Path):
    corpus = make_corpus(tmp_path)
    bare = tmp_path / "not-a-corpus"
    bare.mkdir()
    result = make_result(
        stdout=f"first /tmp/does-not-exist\nthen {corpus}\nalso {bare}\n"
    )
    assert resolve_corpus_path(result) == corpus.resolve()


def test_resolve_corpus_path_raises_when_absent(tmp_path: Path):
    result = make_result(stdout="no paths here")
    with pytest.raises(CorpusPathNotFound):
        resolve_corpus_path(result)


def test_resolve_corpus_path_rejects_incomplete_corpus(tmp_path: Path):
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "manifest.json").write_text("{}", encoding="utf-8")
    result = make_result(stdout=str(partial))
    with pytest.raises(CorpusPathNotFound):
        resolve_corpus_path(result)


def test_verify_uses_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    corpus = make_corpus(tmp_path)

    def ok_run(argv, cwd, timeout=None):
        assert argv == ["uv", "run", "docchunk", "verify", str(corpus)]
        assert cwd == PROJECT
        return make_result(stdout="PASS")

    monkeypatch.setattr("knowledge_ingest.adapters.docchunk.run_checked", ok_run)
    adapter = DocchunkAdapter(project=PROJECT)
    assert adapter.verify(corpus) is True

    def bad_run(argv, cwd, timeout=None):
        return CommandResult(argv=tuple(argv), returncode=1, stdout="FAIL",
                             stderr="", started_at=make_result().started_at,
                             ended_at=make_result().ended_at)

    monkeypatch.setattr("knowledge_ingest.adapters.docchunk.run_checked", bad_run)
    assert adapter.verify(corpus) is False
