from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge_ingest.adapters.media import MediaAdapter, build_transcript_cache_key

PROJECT = Path("/Volumes/ORICO/Projects/media-transcriber")


def fake_run_factory(output_root: Path, stems: dict[str, str]):
    def fake_run(argv, cwd, timeout=None):
        assert cwd == PROJECT
        stem = stems["stem"]
        md = output_root / stem / f"{stem}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# 转写内容", encoding="utf-8")
        (output_root / stem / "metadata.yaml").write_text(
            "source: x\n", encoding="utf-8")
        now = datetime.now(timezone.utc)
        from knowledge_ingest.runner import CommandResult
        return CommandResult(argv=tuple(argv), returncode=0, stdout=str(md),
                             stderr="", started_at=now, ended_at=now)
    return fake_run


def test_transcribe_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output_root = tmp_path / "output"
    stems = {"stem": "lesson01"}
    monkeypatch.setattr(
        "knowledge_ingest.adapters.media.run_checked",
        fake_run_factory(output_root, stems),
    )
    monkeypatch.setattr(
        "knowledge_ingest.adapters.media.MediaAdapter.head",
        lambda self: "096e9e385c7885f0704788884571256cf1000ae3",
    )
    adapter = MediaAdapter(project=PROJECT, output_root=output_root)
    source = tmp_path / "lesson01.mp4"
    source.write_bytes(b"v")
    result = adapter.transcribe(source)
    assert result.markdown_path == output_root / "lesson01" / "lesson01.md"
    assert result.metadata_path == output_root / "lesson01" / "metadata.yaml"
    assert result.markdown_path.is_file()
    assert result.metadata_path.is_file()
    assert result.cache_key.startswith("sha256:")


def test_transcribe_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from knowledge_ingest.runner import CommandResult

    def fail_run(argv, cwd, timeout=None):
        now = datetime.now(timezone.utc)
        return CommandResult(argv=tuple(argv), returncode=1, stdout="",
                             stderr="asr boom", started_at=now, ended_at=now)

    monkeypatch.setattr("knowledge_ingest.adapters.media.run_checked", fail_run)
    adapter = MediaAdapter(project=PROJECT, output_root=tmp_path / "out")
    source = tmp_path / "bad.mp4"
    source.write_bytes(b"v")
    with pytest.raises(RuntimeError):
        adapter.transcribe(source)


def test_transcribe_missing_metadata_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output_root = tmp_path / "output"
    stems = {"stem": "lesson02"}

    def incomplete_run(argv, cwd, timeout=None):
        stem = stems["stem"]
        md = output_root / stem / f"{stem}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("x", encoding="utf-8")
        now = datetime.now(timezone.utc)
        from knowledge_ingest.runner import CommandResult
        return CommandResult(argv=tuple(argv), returncode=0, stdout=str(md),
                             stderr="", started_at=now, ended_at=now)

    monkeypatch.setattr("knowledge_ingest.adapters.media.run_checked", incomplete_run)
    monkeypatch.setattr(
        "knowledge_ingest.adapters.media.MediaAdapter.head",
        lambda self: "abc",
    )
    adapter = MediaAdapter(project=PROJECT, output_root=output_root)
    source = tmp_path / "lesson02.mp4"
    source.write_bytes(b"v")
    with pytest.raises(RuntimeError):
        adapter.transcribe(source)


def test_transcribe_argv_includes_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def capture_run(argv, cwd, timeout=None):
        seen["argv"] = list(argv)
        output_root = tmp_path / "output"
        md = output_root / "clip" / "clip.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("x", encoding="utf-8")
        (output_root / "clip" / "metadata.yaml").write_text("a: b\n", encoding="utf-8")
        now = datetime.now(timezone.utc)
        from knowledge_ingest.runner import CommandResult
        return CommandResult(argv=tuple(argv), returncode=0, stdout=str(md),
                             stderr="", started_at=now, ended_at=now)

    monkeypatch.setattr("knowledge_ingest.adapters.media.run_checked", capture_run)
    monkeypatch.setattr(
        "knowledge_ingest.adapters.media.MediaAdapter.head",
        lambda self: "abc",
    )
    adapter = MediaAdapter(project=PROJECT, output_root=tmp_path / "output")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"v")
    adapter.transcribe(source, device="cpu", timestamp="5m",
                       glossary="glossary.txt", hotwords="担保,代偿")
    argv = seen["argv"]
    assert argv[:4] == ["uv", "run", "media-transcriber", "transcribe"]
    assert "--device" in argv and argv[argv.index("--device") + 1] == "cpu"
    assert "--timestamp" in argv and argv[argv.index("--timestamp") + 1] == "5m"
    assert "--glossary" in argv
    assert "--hotword" in argv


def test_build_transcript_cache_key_changes_with_params():
    base = dict(source_sha256="sha256:a", mt_head="h1", config_sha=None,
                device="auto", timestamp="10m", glossary=None,
                hotwords=None)
    k1 = build_transcript_cache_key(**base)
    assert k1 == build_transcript_cache_key(**base)
    k2 = build_transcript_cache_key(**{**base, "timestamp": "5m"})
    k3 = build_transcript_cache_key(**{**base, "mt_head": "h2"})
    k4 = build_transcript_cache_key(**{**base, "glossary": "g.txt"})
    assert len({k1, k2, k3, k4}) == 4
    assert all(k.startswith("sha256:") for k in (k1, k2, k3, k4))
