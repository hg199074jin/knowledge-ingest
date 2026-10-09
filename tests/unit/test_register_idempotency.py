"""§7.8/§7.9：`source register` 的 ACK 丢失幂等语义。

跨进程场景：register 成功但 ACK 丢失 → Bridge 重试。必须满足：

- 相同 handoff + 已推进 Job：幂等成功，**不改** status/mtime/event 次数；
- 不同 handoff：HANDOFF_CONFLICT，**不覆盖**旧记录；
- 已 BLOCKED / 下游运行中的 Job：不得被重复注册**复活或重置**。

隐私：输出不含 Telegram 正文。
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.external_jobs import HANDOFF_CONFLICT, already_registered
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

BODY = ("先说结论：这份方法论的核心是三步闭环。"
        "第一步定义问题边界，第二步构造最小实验。第三步才是规模化。")


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {"baidu": "baidu-drive", "quark": "quarkclouddrive",
                   "cangjie": "cangjie-skill",
                   "personal_distiller": "personal-capability-distiller"},
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


def make_job(tmp_path: Path) -> tuple[AppConfig, ManifestStore, str]:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider="telegram", source="item:tg:1-3",
        targets=["k2c"]))
    return config, store, manifest.job_id


def write_handoff(tmp_path: Path, *, local_text: str = BODY,
                  fingerprint: str | None = None) -> Path:
    """写入一个最小但合法的 telegram handoff（本地合成内容）。"""
    source_dir = tmp_path / "src"
    source_dir.mkdir(parents=True, exist_ok=True)
    local = source_dir / "item.md"
    local.write_text(local_text, encoding="utf-8")
    handoff = {
        "schema_version": 2,
        "provider": "telegram",
        "remote": {"id": "tg:chan:1-3", "path": None, "name": "chan",
                   "size_bytes": local.stat().st_size,
                   "mtime": "2026-01-01T10:02:00+00:00"},
        "local_path": str(local),
        "download_completed": True,
        "source_notes": [],
        "provenance": {"platform": "telegram", "source_id": "tg:chan",
                       "chat_id": -1001, "message_ids": [1, 2, 3],
                       "sender_id": "u1", "message_url": None,
                       "first_message_at": "2026-01-01T10:00:00+00:00",
                       "last_message_at": "2026-01-01T10:02:00+00:00",
                       "source_deleted": False},
    }
    path = tmp_path / "handoff" / "source.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    return path


def _ns_register(job_id: str, handoff: Path) -> Namespace:
    return Namespace(job_id=job_id, handoff=str(handoff))


# ---------- §7.8 相同 payload 重试 ----------

def test_first_register_succeeds(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path)
    assert _cmd_source_register(config, _ns_register(job_id, handoff)) == 0
    capsys.readouterr()
    assert store.load(job_id).status == "DOWNLOADED"


def test_identical_register_is_idempotent(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, handoff))
    capsys.readouterr()

    manifest_path = store.manifest_path(job_id)
    before_mtime = manifest_path.stat().st_mtime_ns
    before = store.load(job_id)

    rc = _cmd_source_register(config, _ns_register(job_id, handoff))
    out = capsys.readouterr().out
    assert rc == 0
    assert "ALREADY_REGISTERED" in out
    after = store.load(job_id)
    assert after.status == before.status
    assert after.source == before.source
    assert manifest_path.stat().st_mtime_ns == before_mtime
    assert len(after.gate_history) == len(before.gate_history)


def test_repeated_register_does_not_append_errors(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, handoff))
    capsys.readouterr()
    before = len(store.load(job_id).errors)
    for _ in range(3):
        _cmd_source_register(config, _ns_register(job_id, handoff))
    capsys.readouterr()
    assert len(store.load(job_id).errors) == before


def test_already_registered_predicate(tmp_path):
    config, store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, handoff))
    manifest = store.load(job_id)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    payload["source_fingerprint"] = manifest.source["source_fingerprint"]
    assert already_registered(manifest, payload) is True


# ---------- §7.8 不同 payload -> conflict ----------

def test_different_handoff_conflicts(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    first = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, first))
    capsys.readouterr()
    before = store.load(job_id)

    second = write_handoff(tmp_path / "b",
                           local_text="完全不同的另一份来源材料内容用于冲突测试。")
    rc = _cmd_source_register(config, _ns_register(job_id, second))
    captured = capsys.readouterr()
    assert rc != 0
    assert HANDOFF_CONFLICT in captured.err + captured.out
    after = store.load(job_id)
    # 旧记录不得被覆盖
    assert after.source["local_path"] == before.source["local_path"]
    assert after.status == before.status


def test_conflict_does_not_delete_old_source(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    first = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, first))
    capsys.readouterr()
    old = dict(store.load(job_id).source)
    second = write_handoff(tmp_path / "b", local_text="另一份完全不同的材料内容。")
    _cmd_source_register(config, _ns_register(job_id, second))
    capsys.readouterr()
    assert dict(store.load(job_id).source) == old


# ---------- §7.9 不得复活已推进 / BLOCKED Job ----------

#: 真实状态机路径（从 register 后的 DOWNLOADED 基线出发，均为 TRANSITIONS 允许）
_PATHS = {
    "BLOCKED": ["ROUTING", "BLOCKED"],
    "ROUTING": ["ROUTING"],
    "DOCCHUNKING": ["ROUTING", "DOCCHUNKING"],
    "VERIFYING": ["ROUTING", "DOCCHUNKING", "VERIFYING"],
    "CORPUS_READY": ["ROUTING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY"],
    "COMPLETED": ["ROUTING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
                  "COMPLETED"],
    "PARTIAL": ["ROUTING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
                "TARGET_RUNNING", "PARTIAL"],
    "WAITING_USER": ["ROUTING", "DOCCHUNKING", "VERIFYING", "CORPUS_READY",
                     "TARGET_RUNNING", "WAITING_USER"],
}


def _advance_to(store, job_id, target):
    """把一个已注册（DOWNLOADED）的 job 推进到目标状态。

    走真实 TRANSITIONS 路径，并让 target 级状态与 overall 一致
    （JobManifest 的 model validator 会拒绝不一致的组合）。
    """
    from knowledge_ingest.models import TargetState
    from knowledge_ingest.state_machine import transition_to
    path = _PATHS[target]
    tail = path[-1] if path[-1] in ("PARTIAL", "WAITING_USER") else None
    with store.edit(job_id) as manifest:
        if tail:
            # TARGET_RUNNING 需要一个已注册、非终态的 active target
            manifest.targets["k2c"] = TargetState(status="READY")
        for status in path:
            if status == tail:
                if tail == "WAITING_USER":
                    manifest.targets["k2c"].status = "WAITING_USER"
                    manifest.active_target = "k2c"
                else:
                    manifest.targets["k2c"].status = "FAILED"
                    manifest.active_target = None
                transition_to(manifest, status)
                break
            transition_to(manifest, status,
                          "k2c" if status == "TARGET_RUNNING" else None)


@pytest.mark.parametrize("status_before", sorted(_PATHS))
def test_re_register_never_regresses_status(tmp_path, capsys, status_before):
    """§5 / §7.9：已推进状态不得被重复 register 静默重置或复活。"""
    config, store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, handoff))
    capsys.readouterr()
    assert store.load(job_id).status == "DOWNLOADED"

    _advance_to(store, job_id, status_before)
    before = store.load(job_id)
    assert before.status == status_before

    rc = _cmd_source_register(config, _ns_register(job_id, handoff))
    capsys.readouterr()
    after = store.load(job_id)

    assert after.status == status_before, f"{status_before} 被重复注册改写"
    assert after.source == before.source
    assert dict(after.targets) == dict(before.targets)
    assert after.active_target == before.active_target
    assert len(after.errors) == len(before.errors)
    assert rc in (0, 1)


def test_blocked_job_is_not_reactivated(tmp_path, capsys):
    config, store, job_id = make_job(tmp_path)
    # 制造 BLOCKED：先写一个非法 handoff 让它进 BLOCKED
    bad = write_handoff(tmp_path / "bad", local_text="x")
    bad_payload = json.loads(bad.read_text(encoding="utf-8"))
    bad_payload["provenance"]["chat_id"] = None
    bad_payload["provenance"]["message_ids"] = []
    bad.write_text(json.dumps(bad_payload, ensure_ascii=False),
                   encoding="utf-8")
    _cmd_source_register(config, _ns_register(job_id, bad))
    capsys.readouterr()
    assert store.load(job_id).status == "BLOCKED"

    good = write_handoff(tmp_path / "good", local_text=BODY)
    rc = _cmd_source_register(config, _ns_register(job_id, good))
    capsys.readouterr()
    # BLOCKED 不得被自动解除
    assert store.load(job_id).status == "BLOCKED"
    assert rc != 0


# ---------- privacy ----------

def test_register_output_has_no_message_body(tmp_path, capsys):
    config, _store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path, local_text="机密正文内容不应出现在输出里。")
    _cmd_source_register(config, _ns_register(job_id, handoff))
    out = capsys.readouterr().out
    assert "机密正文内容不应出现在输出里" not in out


def test_conflict_output_has_no_message_body(tmp_path, capsys):
    config, _store, job_id = make_job(tmp_path)
    first = write_handoff(tmp_path)
    _cmd_source_register(config, _ns_register(job_id, first))
    capsys.readouterr()
    second = write_handoff(tmp_path / "b", local_text="另一份机密正文内容。")
    _cmd_source_register(config, _ns_register(job_id, second))
    captured = capsys.readouterr()
    assert "另一份机密正文内容" not in captured.out + captured.err