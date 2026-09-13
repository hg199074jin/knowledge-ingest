"""v0.3 A2: run_id-threaded preprocess events + MediaOutput.outcome."""

import json

from tests.unit.test_preprocess_cli import FakeDocchunk, FakeMedia, make_media_job, run_preprocess


def _events(config, job_id) -> list[dict]:
    path = (config.pipeline_root / "jobs" / job_id / "logs"
            / "events.jsonl")
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(config, job_id, monkeypatch, fail_media=False):
    return run_preprocess(
        config, job_id, monkeypatch,
        FakeMedia(config.media_project, config.media_output_root,
                  fail=fail_media),
        FakeDocchunk(config.docchunk_project))


def test_run_id_invariant(tmp_path, monkeypatch):
    config, _store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    assert _run(config, job_id, monkeypatch) == 0
    events = _events(config, job_id)
    started = [e for e in events if e["event"] == "preprocess_started"]
    finished = [e for e in events if e["event"] == "preprocess_finished"]
    media = [e for e in events if e["event"].startswith("media_")]
    assert len(started) == 1
    assert len(finished) == 1
    run_id = started[0]["run_id"]
    assert finished[0]["run_id"] == run_id
    assert finished[0]["outcome"] == "completed"
    assert media
    assert all(e["run_id"] == run_id for e in media)


def test_media_output_ready_fields(tmp_path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    _run(config, job_id, monkeypatch)
    ready = [e for e in _events(config, job_id)
             if e["event"] == "media_output_ready"]
    assert len(ready) == 1
    ev = ready[0]
    assert ev["outcome"] == "transcribed"
    assert ev["source"] == "01.mp4"
    assert ev["source_sha256"].startswith("sha256:")
    assert ev["cache_key"].startswith("sha256:")
    assert ev["attempt"] == 1
    assert isinstance(ev["duration_ms"], int)
    loaded = store.load(job_id)
    assert loaded.media.outputs[0].outcome == "transcribed"


def test_media_failed_event_and_finished_failed(tmp_path, monkeypatch):
    config, _store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    rc = _run(config, job_id, monkeypatch, fail_media=True)
    assert rc == 1
    events = _events(config, job_id)
    failed = [e for e in events if e["event"] == "media_failed"]
    finished = [e for e in events if e["event"] == "preprocess_finished"]
    assert len(failed) == 1
    assert failed[0]["source"] == "01.mp4"
    assert finished[0]["outcome"] == "failed"
    assert finished[0]["reason"] == "media_failed"
    assert finished[0]["run_id"] == failed[0]["run_id"]


def test_second_run_emits_cache_reused(tmp_path, monkeypatch):
    config, store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    assert _run(config, job_id, monkeypatch) == 0
    loaded = store.load(job_id)
    loaded.status = "DOCCHUNKING"
    store.save(loaded)
    assert _run(config, job_id, monkeypatch) == 0
    events = _events(config, job_id)
    started = [e for e in events if e["event"] == "preprocess_started"]
    assert len(started) == 2
    reused = [e for e in events if e["event"] == "media_output_ready"
              and e["outcome"] == "cache_reused"]
    assert len(reused) == 1
    assert reused[0]["run_id"] == started[1]["run_id"]
    loaded = store.load(job_id)
    assert loaded.media.outputs[0].outcome == "cache_reused"


def test_route_emits_routed_event(tmp_path, monkeypatch):
    config, _store, job_id = make_media_job(tmp_path, {"01.mp4": b"v"})
    routed = [e for e in _events(config, job_id) if e["event"] == "routed"]
    assert len(routed) == 1
    assert routed[0]["effective_media"] == 1
    assert routed[0]["effective_documents"] == 0
