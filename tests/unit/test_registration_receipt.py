"""GPT-KI-TAD-FINAL（Issue #39 §1）：失败注册重放不得变成假成功。

## 缺陷（P1-A）真实链路

1. `_cmd_source_register()` 在**已锁定**会话内先 `manifest.source = handoff`，
   随后 `_handoff_validation_error()` 判失败 -> `_block()` + `commit()` + exit 1；
2. 用**同一个无效 handoff** 再 register，锁内 `already_registered()` 在任何
   validation / 成功证明**之前**返回 True -> 打印 `ALREADY_REGISTERED`、exit 0；
3. TAD 的 `KI_REGISTERED_STATUSES` 含 `BLOCKED`/`FAILED`，收到 exit 0 后
   lookup 到 `BLOCKED` 就可能把 ledger 标成 `DELIVERED`。

失败的 handoff 不能通过重放「变成」成功。

## 修复方向

`ALREADY_REGISTERED` 必须绑定到**可证明的历史成功注册凭据**（durable
receipt），而不是「`manifest.source` 等价」这一个事实。

- 新注册在**验证通过后**写入 minimal receipt（`manifest.source` 自由 dict，
  免 schema migration）；
- 无 receipt 的**旧 manifest** 用既有 `logs/events.jsonl` 里的
  `source_registered` 事件做可验证的兼容判定；
- 曾验证失败进入 BLOCKED 的相同 handoff -> 重放返回**非 0**，不改状态，
  不伪造 receipt，且不能借重放绕过原 BLOCKED（Human Gate 语义不变）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.external_jobs import (
    ALREADY_REGISTERED,
    EXIT_CODES,
    HANDOFF_CONFLICT,
    REGISTRATION_RECEIPT_KEY,
    already_registered,
    registration_receipt,
    source_without_receipt,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

SRC_ROOT = str(Path(__file__).resolve().parents[2] / "src")
BODY = "先说结论：这份方法论的核心是三步闭环，第一步定义问题边界。"


def config_dict(tmp_path: Path) -> dict:
    return {
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive",
            "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m", "require_orico": False},
    }


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(config_dict(tmp_path))


def make_job(tmp_path: Path):
    store = ManifestStore(jobs_root=tmp_path / "kp" / "jobs")
    manifest = store.create(
        JobRequest(
            raw_prompt="telegram source item",
            provider="telegram",
            source="item:tg:1-3",
            targets=["k2c"],
        )
    )
    return store, manifest.job_id


def valid_handoff(tmp_path: Path, tag: str = "a") -> Path:
    src = tmp_path / f"src_{tag}"
    src.mkdir(parents=True, exist_ok=True)
    local = src / "item.md"
    local.write_text(BODY, encoding="utf-8")
    return _dump(
        tmp_path / f"handoff_{tag}.json",
        {
            "schema_version": 2,
            "provider": "telegram",
            "remote": {
                "id": f"tg:{tag}:1-3",
                "path": None,
                "name": tag,
                "size_bytes": local.stat().st_size,
                "mtime": "2026-01-01T10:02:00+00:00",
            },
            "local_path": str(local),
            "download_completed": True,
            "source_notes": [],
            "provenance": {
                "platform": "telegram",
                "source_id": f"tg:{tag}",
                "chat_id": -1001,
                "message_ids": [1, 2, 3],
                "sender_id": "u1",
                "message_url": None,
                "first_message_at": "2026-01-01T10:00:00+00:00",
                "last_message_at": "2026-01-01T10:02:00+00:00",
                "source_deleted": False,
            },
        },
    )


def invalid_handoff(tmp_path: Path, tag: str = "bad") -> Path:
    """malformed TG provenance -> validation 失败 -> BLOCKED。"""
    src = tmp_path / f"src_{tag}"
    src.mkdir(parents=True, exist_ok=True)
    local = src / "item.md"
    local.write_text(BODY, encoding="utf-8")
    return _dump(
        tmp_path / f"handoff_{tag}.json",
        {
            "schema_version": 2,
            "provider": "telegram",
            "remote": {
                "id": f"tg:{tag}:1-3",
                "path": None,
                "name": tag,
                "size_bytes": local.stat().st_size,
                "mtime": "2026-01-01T10:02:00+00:00",
            },
            "local_path": str(local),
            "download_completed": True,
            "source_notes": [],
            # provenance 缺失 -> invalid_telegram_provenance
            "provenance": {},
        },
    )


def _dump(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _ns(job_id: str, handoff: Path) -> Namespace:
    return Namespace(job_id=job_id, handoff=str(handoff))


def _events(tmp_path: Path, job_id: str) -> list[str]:
    log = tmp_path / "kp" / "jobs" / job_id / "logs" / "events.jsonl"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


# ---------- 核心：无效 handoff 重放 ----------


def test_invalid_handoff_first_register_blocks(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    bad = invalid_handoff(tmp_path)
    rc = _cmd_source_register(make_config(tmp_path), _ns(job_id, bad))
    capsys.readouterr()
    assert rc != 0, "无效 handoff 首次注册必须失败"
    assert store.load(job_id).status == "BLOCKED"


def test_invalid_replay_is_not_already_registered(tmp_path, capsys):
    """P1-A 核心：同一无效 handoff 重放不得返回 ALREADY_REGISTERED / 0。"""
    store, job_id = make_job(tmp_path)
    bad = invalid_handoff(tmp_path)
    config = make_config(tmp_path)

    assert _cmd_source_register(config, _ns(job_id, bad)) != 0
    capsys.readouterr()
    path = store.manifest_path(job_id)
    before_bytes, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    before = store.load(job_id)

    rc = _cmd_source_register(config, _ns(job_id, bad))
    captured = capsys.readouterr()
    assert rc != 0, "重放不得返回 0"
    assert ALREADY_REGISTERED not in captured.out + captured.err
    after = store.load(job_id)
    assert after.status == before.status == "BLOCKED"
    assert path.read_bytes() == before_bytes, "重放不得改写 manifest"
    assert path.stat().st_mtime_ns == before_mtime


def test_invalid_replay_does_not_fabricate_receipt(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    bad = invalid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, bad))
    capsys.readouterr()
    _cmd_source_register(config, _ns(job_id, bad))
    capsys.readouterr()
    assert registration_receipt(store.load(job_id)) is None, "失败的注册不得伪造成功凭据"


def test_invalid_replay_cannot_bypass_block(tmp_path, capsys):
    """重放不得借机解除既有 BLOCKED（Human Gate 语义）。"""
    store, job_id = make_job(tmp_path)
    bad = invalid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, bad))
    capsys.readouterr()
    assert store.load(job_id).status == "BLOCKED"
    _cmd_source_register(config, _ns(job_id, bad))
    capsys.readouterr()
    assert store.load(job_id).status == "BLOCKED"


def test_invalid_replay_error_code_is_stable(tmp_path, capsys):
    _store, job_id = make_job(tmp_path)
    bad = invalid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, bad))
    capsys.readouterr()
    rc = _cmd_source_register(config, _ns(job_id, bad))
    captured = capsys.readouterr()
    assert rc == EXIT_CODES[HANDOFF_CONFLICT]
    assert HANDOFF_CONFLICT in captured.err + captured.out


# ---------- 成功注册 -> 下游 BLOCKED 仍算已注册 ----------


def test_success_then_downstream_blocked_still_already(tmp_path, capsys):
    """合法情况：注册成功，后续 K2C stage 进入 BLOCKED，重放仍 ALREADY。"""
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    config = make_config(tmp_path)
    assert _cmd_source_register(config, _ns(job_id, good)) == 0
    capsys.readouterr()

    with store.edit(job_id) as m:
        from knowledge_ingest.state_machine import transition_to

        transition_to(m, "ROUTING")
        m.status = "BLOCKED"
        m.errors.append({"reason": "k2c_stage_failed"})

    before = store.manifest_path(job_id).read_bytes()
    rc = _cmd_source_register(config, _ns(job_id, good))
    captured = capsys.readouterr()
    assert rc == 0
    assert ALREADY_REGISTERED in captured.out
    assert store.load(job_id).status == "BLOCKED", "不得复活下游 blocker"
    assert store.manifest_path(job_id).read_bytes() == before, "重放零写入"


def test_blocked_without_receipt_is_not_treated_as_registered(tmp_path, capsys):
    """§1.1：不能把所有历史 BLOCKED 都当成「注册失败」——
    判定必须基于**可证明**的凭据，而不是状态。"""
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, good))
    capsys.readouterr()
    # 手工抹掉 receipt，模拟旧 manifest / receipt 丢失
    with store.edit(job_id) as m:
        m.source.pop(REGISTRATION_RECEIPT_KEY, None)
    # 但 events 里仍有 source_registered -> 兼容判定应认定为已注册
    rc = _cmd_source_register(config, _ns(job_id, good))
    captured = capsys.readouterr()
    assert rc == 0, "有 source_registered 事件即视为曾成功注册"
    assert ALREADY_REGISTERED in captured.out


def test_no_receipt_and_no_event_is_not_registered(tmp_path, capsys):
    """既无 receipt 又无 source_registered 事件 -> 不可证明成功。"""
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, good))
    capsys.readouterr()
    with store.edit(job_id) as m:
        m.source.pop(REGISTRATION_RECEIPT_KEY, None)
        m.status = "BLOCKED"
    log = tmp_path / "kp" / "jobs" / job_id / "logs" / "events.jsonl"
    log.unlink()

    rc = _cmd_source_register(config, _ns(job_id, good))
    captured = capsys.readouterr()
    assert rc != 0
    assert ALREADY_REGISTERED not in captured.out + captured.err


# ---------- receipt 结构 ----------


def test_success_writes_receipt(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, good))
    capsys.readouterr()
    receipt = registration_receipt(store.load(job_id))
    assert receipt is not None
    assert receipt["validated"] is True
    assert receipt["protocol_version"] == 1
    assert len(receipt["handoff_sha256"]) == 64


def test_receipt_excludes_itself_from_identity(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, good))
    capsys.readouterr()
    manifest = store.load(job_id)
    stripped = source_without_receipt(manifest.source)
    assert REGISTRATION_RECEIPT_KEY not in stripped
    payload = json.loads(good.read_text(encoding="utf-8"))
    # proven=True 是调用方在确认 receipt 后传入的；默认 False（fail-closed）
    assert already_registered(manifest, payload) is False
    assert already_registered(manifest, payload, proven=True) is True


def test_receipt_tampered_and_no_event_is_not_proof(tmp_path, capsys):
    """凭据被篡改**且**没有 source_registered 事件 -> 不可证明成功。"""
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path)
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, good))
    capsys.readouterr()
    with store.edit(job_id) as m:
        m.source[REGISTRATION_RECEIPT_KEY]["handoff_sha256"] = "0" * 64
    (tmp_path / "kp" / "jobs" / job_id / "logs" / "events.jsonl").unlink()
    rc = _cmd_source_register(config, _ns(job_id, good))
    captured = capsys.readouterr()
    assert ALREADY_REGISTERED not in captured.out + captured.err
    assert rc != 0


def test_different_payload_still_conflicts(tmp_path, capsys):
    _store, job_id = make_job(tmp_path)
    first = valid_handoff(tmp_path, "a")
    config = make_config(tmp_path)
    _cmd_source_register(config, _ns(job_id, first))
    capsys.readouterr()
    other = valid_handoff(tmp_path, "b")
    rc = _cmd_source_register(config, _ns(job_id, other))
    captured = capsys.readouterr()
    assert rc == EXIT_CODES[HANDOFF_CONFLICT]
    assert HANDOFF_CONFLICT in captured.err + captured.out


def test_invalid_concurrent_does_not_shadow_valid(tmp_path):
    """两进程并发：valid 与 invalid，不得让 invalid 影子获胜并伪装已注册。"""
    store, job_id = make_job(tmp_path)
    good = valid_handoff(tmp_path, "a")
    bad = invalid_handoff(tmp_path, "bad")
    config = make_config(tmp_path)
    barrier = tmp_path / "barrier"

    snippet = f"""
import json, os, sys, time
sys.path.insert(0, {SRC_ROOT!r})
from argparse import Namespace
from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
cfg = AppConfig.model_validate({config_dict(tmp_path)!r})
deadline = time.time() + 30
while not os.path.exists({str(barrier)!r}) and time.time() < deadline:
    time.sleep(0.005)
time.sleep(0.05)
rc = _cmd_source_register(cfg, Namespace(job_id={job_id!r}, handoff={str(bad)!r}))
print(json.dumps({{"rc": rc}}))
"""
    script = tmp_path / "w.py"
    script.write_text(snippet, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC_ROOT}
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    barrier.write_text("go", encoding="utf-8")
    valid_rc = _cmd_source_register(config, _ns(job_id, good))
    out, err = proc.communicate(timeout=90)
    assert proc.returncode == 0, err
    invalid_rc = json.loads(out.strip().splitlines()[-1])["rc"]

    manifest = store.load(job_id)
    if valid_rc == 0:
        # valid 胜出：receipt 必须存在，重放 invalid 不得伪装已注册
        assert manifest.status == "DOWNLOADED"
        assert registration_receipt(manifest) is not None
        assert invalid_rc != 0
    else:
        assert invalid_rc != 0, "invalid 不得返回成功"
        assert registration_receipt(manifest) is None


def test_exit_codes_unchanged():
    assert ALREADY_REGISTERED == "ALREADY_REGISTERED"
    assert EXIT_CODES[HANDOFF_CONFLICT] == 6


def test_existing_zero_write_and_race_tests_still_defined():
    """§3：#37 的 mtime 零写入 / TOCTOU pins 不得被本轮破坏。"""
    race = Path(__file__).with_name("test_register_race.py")
    text = race.read_text(encoding="utf-8")
    assert "test_identical_replay_is_strictly_zero_write" in text
    assert "test_concurrent_different_handoffs_exactly_one_wins" in text
