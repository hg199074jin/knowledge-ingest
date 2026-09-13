"""v0.3 B: streaming strict UTF-8 preflight (frozen specs 1/14/16)."""

import json
from pathlib import Path

from knowledge_ingest.cli import _cmd_route
from knowledge_ingest.encoding import detect_bom, preflight_utf8
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

from tests.unit.test_preprocess_cli import make_config

GBK_HEAD = "第八课、实操如何参与平台活动_1.mp4".encode("gbk")


def _route_only(tmp_path: Path, layout: dict[str, bytes]):
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    source = tmp_path / "source"
    for rel, content in layout.items():
        p = source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="local", source=str(source),
        targets=["cangjie"]))
    handoff = tmp_path / "source.json"
    handoff.write_text(json.dumps({
        "schema_version": 1, "provider": "local", "remote": None,
        "local_path": str(source), "download_completed": True,
        "source_notes": []}), encoding="utf-8")
    return config, store, manifest.job_id, handoff


def _register_and_route(config, store, job_id, handoff):
    from knowledge_ingest.cli import _cmd_source_register

    _cmd_source_register(config, Namespace(job_id=job_id, handoff=str(handoff)))
    return _cmd_route(config, Namespace(job_id=job_id, excludes=[]))


from types import SimpleNamespace as Namespace  # noqa: E402


def test_detect_bom_priority(tmp_path: Path):
    assert detect_bom(b"\xFF\xFE\x00\x00abc") == "UTF-32LE"   # before UTF-16
    assert detect_bom(b"\x00\x00\xFE\xFFabc") == "UTF-32BE"
    assert detect_bom(b"\xFF\xFEa\x00b\x00") == "UTF-16LE"
    assert detect_bom(b"\xFE\xFFa\x00b\x00") == "UTF-16BE"
    assert detect_bom(b"\xEF\xBB\xBF# ok") == "UTF-8"
    assert detect_bom(b"plain") is None


def test_preflight_utf8_paths(tmp_path: Path):
    ok = tmp_path / "ok.md"
    ok.write_bytes(b"# fine\n")
    assert preflight_utf8(ok) is None
    bom8 = tmp_path / "bom8.md"
    bom8.write_bytes(b"\xEF\xBB\xBF# bom is fine\n")
    assert preflight_utf8(bom8) is None
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes(GBK_HEAD)
    assert preflight_utf8(gbk) == "unknown"      # no BOM -> unknown, no guessing
    u16 = tmp_path / "u16.txt"
    u16.write_bytes("标题".encode("utf-16-le"))  # with BOM:
    u16.write_bytes(b"\xFF\xFE" + "标题".encode("utf-16-le"))
    assert preflight_utf8(u16) == "UTF-16LE"
    u32 = tmp_path / "u32.txt"
    u32.write_bytes(b"\xFF\xFE\x00\x00" + "标题".encode("utf-32-le"))
    assert preflight_utf8(u32) == "UTF-32LE"     # UTF-32 wins over UTF-16


def test_preflight_catches_late_gbk_tail(tmp_path: Path):
    big = tmp_path / "big.md"
    big.write_bytes(b"a" * 300_000 + GBK_HEAD)
    assert preflight_utf8(big) == "unknown"      # full-file scan, O(1) memory


def test_route_blocks_gbk_document(tmp_path: Path):
    config, store, job_id, handoff = _route_only(
        tmp_path, {"目录.txt": GBK_HEAD, "ok.mp4": b"v"})
    rc = _register_and_route(config, store, job_id, handoff)
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.status == "BLOCKED"
    err = loaded.errors[-1]
    assert err["reason"] == "text_encoding_unsupported"
    assert err["detected_encoding"] == "unknown"
    assert err["remediation"].startswith("convert")
    events = (config.pipeline_root / "jobs" / job_id / "logs"
              / "events.jsonl").read_text(encoding="utf-8")
    assert "text_encoding_unsupported" in events


def test_route_utf8_bom_document_passes(tmp_path: Path):
    config, store, job_id, handoff = _route_only(
        tmp_path, {"目录.txt": b"\xEF\xBB\xBF" + "第一章 起点\n".encode("utf-8"),
                   "ok.mp4": b"v"})
    rc = _register_and_route(config, store, job_id, handoff)
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status == "ROUTING"
    assert len(loaded.routing["effective"]["documents"]) == 1


def test_route_markdown_variant_covered(tmp_path: Path):
    config, store, job_id, handoff = _route_only(
        tmp_path, {"notes.markdown": GBK_HEAD})
    rc = _register_and_route(config, store, job_id, handoff)
    assert rc == 1
    loaded = store.load(job_id)
    assert loaded.errors[-1]["reason"] == "text_encoding_unsupported"


def test_unsupported_beats_encoding_per_frozen_order(tmp_path: Path):
    config, store, job_id, handoff = _route_only(
        tmp_path, {"目录.txt": GBK_HEAD, "junk.bat": b"x"})
    rc = _register_and_route(config, store, job_id, handoff)
    assert rc == 1
    loaded = store.load(job_id)
    # frozen spec 1: unsupported_source is the single primary reason
    assert loaded.errors[-1]["reason"] == "unsupported_source"
