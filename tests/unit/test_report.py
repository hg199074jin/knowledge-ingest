from datetime import UTC
from pathlib import Path

from knowledge_ingest.report import (
    EventLog,
    build_status,
    redact_deep,
    redact_text,
    render_report,
)


def make_manifest() -> "object":
    from datetime import datetime

    from knowledge_ingest.models import JobManifest, JobRequest

    now = datetime.now(UTC)
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
    manifest.targets["personal"].status = "PENDING"
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
    manifest.targets["cangjie"].status = "WAITING_USER"
    manifest.targets["cangjie"].waiting_for = "stage0_overview"
    status = build_status(manifest)
    assert status["overall"] == "CORPUS_READY"
    assert status["transcribe"] == "success 0/2"
    assert status["docchunk"] == "verified PASS"
    assert status["targets"]["cangjie"].startswith(
        "WAITING_USER: stage0_overview")
    assert status["targets"]["personal"] == "PENDING"
    # 兼容键
    assert status["cangjie"] == status["targets"]["cangjie"]
    assert status["personal"] == status["targets"]["personal"]
    assert status["source"].startswith("success")


def test_build_status_includes_reason():
    manifest = make_manifest()
    manifest.targets["family_router"] = type(
        manifest.targets["cangjie"])(depends_on=["cangjie"])
    manifest.targets["family_router"].status = "SKIPPED"
    manifest.targets["family_router"].reason = "dependency_skipped"
    status = build_status(manifest)
    assert status["targets"]["family_router"] == \
        "SKIPPED (dependency_skipped)"


def test_render_report_has_required_sections():
    manifest = make_manifest_with_error("boom")
    report = render_report(manifest)
    for marker in ("请求", "来源", "源指纹", "路由统计", "转写产物",
                   "Corpus", "Cangjie", "Personal", "Targets",
                   "target_outputs", "排除", "失败", "恢复"):
        assert marker in report, f"missing section: {marker}"


def test_raw_prompt_redacted_in_report():
    from knowledge_ingest.report import redact_text
    manifest = make_manifest()
    manifest.request.raw_prompt = "帮我把 access_token=abc123 的课程做成 skill"
    report = render_report(manifest)
    assert "abc123" not in report
    assert "[REDACTED]" in report
    assert redact_text(manifest.request.raw_prompt) != manifest.request.raw_prompt


# ---------- v0.3 D1：运行审计段 ----------


def _audit_manifest():
    from datetime import timedelta

    from knowledge_ingest.models import MediaOutput

    manifest = make_manifest()
    now = manifest.created_at
    manifest.media.started_at = now - timedelta(minutes=30)
    manifest.media.completed_at = now - timedelta(minutes=18)
    manifest.media.outputs = [
        MediaOutput(
            source_relative_path="a.mp4", source_sha256="aa",
            transcript="/t/a.md", transcript_sha256="ta",
            metadata="/t/a.json", outcome="transcribed"),
        MediaOutput(
            source_relative_path="b.mp4", source_sha256="bb",
            transcript="/t/b.md", transcript_sha256="tb",
            metadata="/t/b.json", outcome="cache_reused"),
        MediaOutput(
            source_relative_path="v1.mp4", source_sha256="cc",
            transcript="/t/c.md", transcript_sha256="tc",
            metadata="/t/c.json", outcome="unknown"),
    ]
    manifest.docchunk.started_at = now - timedelta(minutes=10)
    manifest.docchunk.completed_at = now - timedelta(minutes=8)
    # cangjie：有 per-target 时间戳；personal：v1 迁移态（无时间戳）
    manifest.targets["cangjie"].status = "COMPLETED"
    manifest.targets["cangjie"].started_at = now - timedelta(hours=2)
    manifest.targets["cangjie"].completed_at = now - timedelta(minutes=30)
    manifest.budget_state = {"targets": {"family_router": {
        "calls": 8, "cases": {}, "hosts": {},
        "permits": {
            "bp_ok": {"outcome": "success", "call_no": 1},
            "bp_open": {"outcome": None, "call_no": 2},
        },
    }}}
    return manifest


def test_render_report_audit_section_full():
    manifest = _audit_manifest()
    report = render_report(manifest)
    assert "## 运行审计" in report
    assert report.index("## 运行审计") < report.index("## 可恢复信息")
    # source 无 acquired 时间戳 → 不得用 created_at 冒充
    assert "- source：unknown / not instrumented" in report
    assert "created_at" not in report
    # media/docchunk/target 耗时行
    assert "- media：12m00s" in report
    assert "- docchunk：2m00s" in report
    assert "- cangjie：1h30m00s" in report
    assert "- personal：unknown / not instrumented" in report
    # 转写口径（规格 15）
    assert "fresh 1 / cache_reused 1 / unknown 1" in report
    assert "禁反推" in report
    # 预算行
    assert ("- family_router: calls 8/20, permits 2, pending outcome 1"
            in report)
    # 工具版本占位（诚实标注）
    assert "recorded at preprocess (v0.3 起)" in report


def test_render_report_audit_v1_migration_renders_unknown():
    """v1 迁移 manifest：无 per-target 时间戳/outcome → 不崩且如实标 unknown。"""
    from knowledge_ingest.models import JobManifest

    now = _audit_manifest().created_at
    v1 = {
        "schema_version": 1,
        "job_id": "20260901-090000-v1-legacy",
        "created_at": now,
        "updated_at": now,
        "status": "COMPLETED",
        "request": {
            "raw_prompt": "处理课程", "provider": "local",
            "source": "/tmp/x", "targets": ["cangjie", "personal"],
        },
        "source": {"provider": "local", "download_completed": True},
        "routing": {"collection": False, "documents": 1, "media": 2,
                    "unsupported": 0},
        "cangjie": {"status": "SUCCESS", "output_path": "/out/cangjie"},
        "personal": {"status": "PENDING"},
    }
    manifest = JobManifest(**v1)
    report = render_report(manifest)
    assert "## 运行审计" in report
    assert "- source：unknown / not instrumented" in report
    assert "- media：unknown / not instrumented" in report
    assert "- docchunk：unknown / not instrumented" in report
    assert "- cangjie：unknown / not instrumented" in report
    assert "- personal：unknown / not instrumented" in report
    assert "fresh 0 / cache_reused 0 / unknown 0" in report
    assert "- no external calls recorded" in report


def test_render_report_audit_source_acquired_shown_when_present():
    manifest = _audit_manifest()
    manifest.source["acquired"] = "2026-09-13T01:00:00+00:00"
    report = render_report(manifest)
    assert "- source：acquired 2026-09-13T01:00:00+00:00" in report
