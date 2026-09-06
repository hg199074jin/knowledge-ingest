from pathlib import Path

from knowledge_ingest.report import (
    EventLog,
    build_status,
    redact_deep,
    redact_text,
    render_report,
)


def make_manifest() -> "object":
    from datetime import datetime, timezone
    from knowledge_ingest.models import JobManifest, JobRequest

    now = datetime.now(timezone.utc)
    manifest = JobManifest(
        job_id="20260906-120000-quark-course",
        created_at=now, updated_at=now, status="CORPUS_READY",
        request=JobRequest(raw_prompt="处理课程", provider="quark",
                           source="/审计课程/融资担保",
                           targets=["cangjie", "personal"]),
    )
    manifest.source = {
        "provider": "quark",
        "local_path": "/Volumes/ORICO/KnowledgePipeline/jobs/j/source/x",
        "source_fingerprint": "sha256:f00dfeed0001",
        "download_completed": True,
    }
    manifest.routing = {"collection": True, "documents": 1, "media": 2,
                        "unsupported": 0}
    manifest.media.status = "success"
    manifest.docchunk.status = "verified"
    manifest.docchunk.corpus_path = Path("/Volumes/ORICO/LongDocCorpus/x")
    manifest.docchunk.verify = "PASS"
    manifest.personal.status = "pending"
    return manifest


def make_manifest_with_error(message: str):
    manifest = make_manifest()
    manifest.errors.append({"reason": "media_failed", "detail": message})
    return manifest


def test_report_does_not_leak_tokens():
    manifest = make_manifest_with_error("access_token=abc123")
    report = render_report(manifest)
    assert "abc123" not in report
    assert "[REDACTED]" in report


def test_redact_text_patterns():
    text = "failed: password=hunter2 and refresh_token=rt9; see auth_code=777"
    out = redact_text(text)
    assert "hunter2" not in out and "rt9" not in out and "777" not in out
    assert out.count("[REDACTED]") == 3


def test_redact_deep_redacts_sensitive_keys():
    data = {"access_token": "leak", "note": "cookie=jar; ok",
            "nested": {"secret": "s3cr3t"}, "list": [{"password": "p"}],
            "normal": "value"}
    out = redact_deep(data)
    assert out["access_token"] == "[REDACTED]"
    assert out["nested"]["secret"] == "[REDACTED]"
    assert out["list"][0]["password"] == "[REDACTED]"
    assert "jar" not in out["note"]
    assert out["normal"] == "value"


def test_event_log_appends_and_redacts(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.log("docchunk_verified", job_id="j1", corpus_path="/tmp/c",
            note="token=zzz")
    log.log("routed", job_id="j1", documents=1)
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "zzz" not in lines[0]
    assert "[REDACTED]" in lines[0]


def test_build_status_fields():
    manifest = make_manifest()
    manifest.cangjie.status = "waiting_user"
    manifest.cangjie.waiting_for = "stage0_overview"
    status = build_status(manifest)
    assert status["overall"] == "CORPUS_READY"
    assert status["transcribe"] == "success 0/2"
    assert status["docchunk"] == "verified PASS"
    assert status["cangjie"].startswith("waiting_user: stage0_overview")
    assert status["personal"] == "pending"
    assert status["source"].startswith("success")


def test_render_report_has_required_sections():
    manifest = make_manifest_with_error("boom")
    report = render_report(manifest)
    for marker in ("请求", "来源", "源指纹", "路由统计", "转写产物",
                   "Corpus", "Cangjie", "Personal", "排除", "失败", "恢复"):
        assert marker in report, f"missing section: {marker}"
