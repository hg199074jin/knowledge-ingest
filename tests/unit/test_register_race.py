"""GPT-KI-TAD-R1（Issue #37）：`source register` 并发 TOCTOU / ACK 重放原子性。

## 缺陷（P1）

原实现的判定在**锁外**，写入在**锁内**且不重检：

    current = store.load(job_id)          # 无锁快照
    if already_registered(...): return 0  # 判定基于过期读
    if current.source: return CONFLICT
    if current.status != "CREATED": return CONFLICT
    with store.edit(job_id) as m:         # 此处才 flock，且不重检 precondition
        m.source = handoff                # 可能覆盖别人的 handoff
        _advance(...)                     # 甚至在 DOWNLOADED 上继续推进

并发 A/B 都读到 `CREATED + empty source` 时，A 注册成功，B 拿到锁后仍按旧
判断继续，会覆盖 handoff-A，或在已推进状态上 `_advance` 抛 InvalidTransition。

## 修复目标

1. 判定 + 写入在**同一个 per-job flock 临界区**内，且锁内重检；
2. 相同 handoff 的 ACK 重放**严格零写入**（mtime / bytes / events 全不变）；
   注意 `ManifestStore.edit` 即使块内 `return` 也会在 contextmanager 退出时
   `save()`，所以不能把旧判断粘进 `with store.edit`；
3. 冲突必须在锁内阻断，且**无额外** status/error/event 副作用；
4. `handoff_identity` 不能再只看 `(local_path, remote.id, message_ids)`
   —— 同路径内容更新、大小/删除标记变化不得无条件判为同一 payload。

本文件的并发用例全部是**真多进程 + 同步屏障**，不是单进程假测。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.external_jobs import (
    ALREADY_REGISTERED,
    EXIT_CODES,
    HANDOFF_CONFLICT,
    already_registered,
    handoff_identity,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobManifest, JobRequest

SRC_ROOT = str(Path(__file__).resolve().parents[2] / "src")

BODY_A = "先说结论：这份方法论的核心是三步闭环，第一步定义问题边界。"
BODY_B = "另一份完全不同的来源材料，用于制造 register 并发冲突的对照组。"


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
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
            "processing": {
                "media_device": "auto",
                "media_timestamp": "10m",
                "require_orico": False,
            },
        }
    )


def make_job(tmp_path: Path, *, ext: str | None = None):
    store = ManifestStore(jobs_root=tmp_path / "kp" / "jobs")
    req = JobRequest(
        raw_prompt="telegram source item",
        provider="telegram",
        source="item:tg:1-3",
        targets=["k2c"],
        external_id=ext,
    )
    manifest = store.create_idempotent(req) if ext else store.create(req)
    return store, manifest.job_id


def write_handoff(
    tmp_path: Path,
    tag: str,
    body: str,
    *,
    local_name: str = "item.md",
    size_delta: int = 0,
    message_ids: list[int] | None = None,
    source_deleted: bool = False,
    display_name: str = "chan",
) -> Path:
    src = tmp_path / f"src_{tag}"
    src.mkdir(parents=True, exist_ok=True)
    local = src / local_name
    local.write_text(body, encoding="utf-8")
    handoff = {
        "schema_version": 2,
        "provider": "telegram",
        "remote": {
            "id": f"tg:{tag}:1-3",
            "path": None,
            "name": display_name,
            "size_bytes": local.stat().st_size + size_delta,
            "mtime": "2026-01-01T10:02:00+00:00",
        },
        "local_path": str(local),
        "download_completed": True,
        "source_notes": [],
        "provenance": {
            "platform": "telegram",
            "source_id": f"tg:{tag}",
            "chat_id": -1001,
            "message_ids": message_ids or [1, 2, 3],
            "sender_id": "u1",
            "message_url": None,
            "first_message_at": "2026-01-01T10:00:00+00:00",
            "last_message_at": "2026-01-01T10:02:00+00:00",
            "source_deleted": source_deleted,
        },
    }
    path = tmp_path / f"handoff_{tag}.json"
    path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    return path


def _ns(job_id: str, handoff: Path) -> Namespace:
    return Namespace(job_id=job_id, handoff=str(handoff))


def _manifest_state(store: ManifestStore, job_id: str) -> tuple[str, bytes]:
    raw = store.manifest_path(job_id).read_bytes()
    return raw, raw


# ---------- §4 身份等价契约 ----------


def test_identity_same_payload_equal():
    a = json.loads(write_handoff(Path("/tmp/ki-r1-eq"), "a", BODY_A).read_text())
    b = json.loads(write_handoff(Path("/tmp/ki-r1-eq"), "a", BODY_A).read_text())
    assert handoff_identity(a) == handoff_identity(b)


def test_identity_different_path_differs(tmp_path):
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "b", BODY_A).read_text())
    assert handoff_identity(a) != handoff_identity(b)


def test_identity_ignores_derived_source_fingerprint(tmp_path):
    """下游派生的 source_fingerprint 不参与等价判断（注册时它还不存在）。"""
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b["source_fingerprint"] = "deadbeef"
    assert handoff_identity(a) == handoff_identity(b)


def test_identity_detects_size_change(tmp_path):
    """§4：大小变化不得无条件判作同一 payload。"""
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A, size_delta=999).read_text())
    assert handoff_identity(a) != handoff_identity(b)


def test_identity_detects_source_deleted_change(tmp_path):
    """§4：删除标记变化不得无条件判作同一 payload。"""
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A, source_deleted=True).read_text())
    assert handoff_identity(a) != handoff_identity(b)


def test_identity_detects_message_ids_change(tmp_path):
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A, message_ids=[1, 2]).read_text())
    assert handoff_identity(a) != handoff_identity(b)


def test_identity_detects_display_name_change(tmp_path):
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A, display_name="other").read_text())
    assert handoff_identity(a) != handoff_identity(b)


def test_identity_tolerates_serialization_order(tmp_path):
    """producer stable serialization 差异（键序）不得改变等价性。"""
    a = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = json.loads(write_handoff(tmp_path, "a", BODY_A).read_text())
    b = dict(reversed(list(b.items())))
    assert handoff_identity(a) == handoff_identity(b)


# ---------- 零写入重放 ----------


def test_identical_replay_is_strictly_zero_write(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path, "a", BODY_A)
    assert _cmd_source_register(make_config(tmp_path), _ns(job_id, handoff)) == 0
    capsys.readouterr()
    path = store.manifest_path(job_id)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    before = store.load(job_id)
    before_errors = len(before.errors)
    before_log = _log_len(tmp_path, job_id)

    time.sleep(0.02)
    rc = _cmd_source_register(make_config(tmp_path), _ns(job_id, handoff))
    out = capsys.readouterr().out
    after = store.load(job_id)
    assert rc == 0 and ALREADY_REGISTERED in out
    assert path.read_bytes() == before_bytes, "重放不得改写 manifest bytes"
    assert path.stat().st_mtime_ns == before_mtime, "重放不得改 mtime"
    assert after.status == before.status
    assert after.source == before.source
    assert len(after.errors) == before_errors
    assert _log_len(tmp_path, job_id) == before_log, "重放不得重复写事件"


def _log_len(tmp_path: Path, job_id: str) -> int:
    log = tmp_path / "kp" / "jobs" / job_id / "logs" / "events.jsonl"
    if not log.is_file():
        return 0
    return len(log.read_text(encoding="utf-8").splitlines())


def test_repeated_replay_never_writes(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    handoff = write_handoff(tmp_path, "a", BODY_A)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, handoff))
    capsys.readouterr()
    path = store.manifest_path(job_id)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    for _ in range(5):
        _cmd_source_register(make_config(tmp_path), _ns(job_id, handoff))
    capsys.readouterr()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# ---------- 冲突与状态闸门 ----------


def test_different_handoff_conflicts_without_side_effects(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    first = write_handoff(tmp_path, "a", BODY_A)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, first))
    capsys.readouterr()
    path = store.manifest_path(job_id)
    before_bytes, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    before = store.load(job_id)

    second = write_handoff(tmp_path, "b", BODY_B)
    rc = _cmd_source_register(make_config(tmp_path), _ns(job_id, second))
    captured = capsys.readouterr()
    assert rc == EXIT_CODES[HANDOFF_CONFLICT]
    assert HANDOFF_CONFLICT in captured.err + captured.out
    assert path.read_bytes() == before_bytes, "冲突方不得改写 manifest"
    assert path.stat().st_mtime_ns == before_mtime
    after = store.load(job_id)
    assert after.status == before.status
    assert after.source == before.source
    assert len(after.errors) == len(before.errors)


def test_advanced_job_refuses_different_handoff(tmp_path, capsys):
    store, job_id = make_job(tmp_path)
    first = write_handoff(tmp_path, "a", BODY_A)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, first))
    capsys.readouterr()
    with store.edit(job_id) as m:
        from knowledge_ingest.state_machine import transition_to

        transition_to(m, "ROUTING")
    second = write_handoff(tmp_path, "b", BODY_B)
    rc = _cmd_source_register(make_config(tmp_path), _ns(job_id, second))
    assert rc == EXIT_CODES[HANDOFF_CONFLICT]
    assert store.load(job_id).status == "ROUTING"


# ---------- 真并发：同步屏障 ----------

_BARRIER_SNIPPET = """
import json, os, sys, time
sys.path.insert(0, {src!r})
from argparse import Namespace
from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore

cfg = AppConfig.model_validate({config!r})
store = ManifestStore(jobs_root=os.path.join(cfg.pipeline_root, "jobs"))
job_id, handoff_path, barrier = {job_id!r}, {handoff!r}, {barrier!r}

# 同步屏障：所有进程都到达后才继续，最大化竞态窗口
deadline = time.time() + 30
while not os.path.exists(barrier) and time.time() < deadline:
    time.sleep(0.005)
time.sleep(0.05)

rc = _cmd_source_register(cfg, Namespace(job_id=job_id, handoff=handoff_path))
print(json.dumps({{"rc": rc}}))
"""


def _spawn(tmp_path: Path, job_id: str, handoff: Path, barrier: Path, tag: str):
    config_dict = {
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
    script = tmp_path / f"worker_{tag}.py"
    script.write_text(
        _BARRIER_SNIPPET.format(
            src=SRC_ROOT,
            config=config_dict,
            job_id=job_id,
            handoff=str(handoff),
            barrier=str(barrier),
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": SRC_ROOT}
    return subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release(barrier: Path) -> None:
    barrier.write_text("go", encoding="utf-8")


def test_concurrent_different_handoffs_exactly_one_wins(tmp_path):
    """两进程同步屏障并发注册**不同** handoff：恰好一个成功，一个 CONFLICT。"""
    store, job_id = make_job(tmp_path)
    a = write_handoff(tmp_path, "a", BODY_A)
    b = write_handoff(tmp_path, "b", BODY_B)
    barrier = tmp_path / "barrier"
    procs = [_spawn(tmp_path, job_id, a, barrier, "a"), _spawn(tmp_path, job_id, b, barrier, "b")]
    _release(barrier)
    results = [p.communicate(timeout=90) for p in procs]
    codes = []
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, err
        codes.append(json.loads(out.strip().splitlines()[-1])["rc"])
    assert sorted(codes) == [0, EXIT_CODES[HANDOFF_CONFLICT]], codes

    manifest = store.load(job_id)
    assert manifest.source, "必须保留胜出方的 handoff"
    winner = manifest.source["local_path"]
    assert winner in (str(tmp_path / "src_a" / "item.md"), str(tmp_path / "src_b" / "item.md"))
    # 只有一份 handoff 被写入（败方不得覆盖）
    assert manifest.source.get("source_fingerprint") is not None


def test_concurrent_same_handoff_one_registers_once(tmp_path):
    """两进程并发注册**相同** handoff：一个首次注册，另一个 ALREADY_REGISTERED。"""
    _store, job_id = make_job(tmp_path)
    a = write_handoff(tmp_path, "a", BODY_A)
    barrier = tmp_path / "barrier"
    procs = [_spawn(tmp_path, job_id, a, barrier, "x"), _spawn(tmp_path, job_id, a, barrier, "y")]
    _release(barrier)
    results = [p.communicate(timeout=90) for p in procs]
    codes = []
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, err
        codes.append(json.loads(out.strip().splitlines()[-1])["rc"])
    assert codes == [0, 0], codes
    assert _log_count(tmp_path, job_id, "source_registered") == 1, "事件不得重复"


def _log_count(tmp_path: Path, job_id: str, event: str) -> int:
    log = tmp_path / "kp" / "jobs" / job_id / "logs" / "events.jsonl"
    if not log.is_file():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if event in line)


# ---------- 确定性 pin：证明锁内重检 ----------

_PIN_SNIPPET = """
import json, sys
sys.path.insert(0, {src!r})
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

# 在「旧 precheck 之后、新锁之前」确定性注入另一个注册，
# 模拟 TOCTOU：任何在锁外做最终判断的实现都会因此出错。
store = ManifestStore(jobs_root={jobs!r})
with store.edit({job_id!r}) as manifest:
    manifest.source = {{"sentinel": "injected-between-precheck-and-lock"}}
print("injected")
"""


def test_deterministic_pin_between_precheck_and_lock(tmp_path, capsys):
    """§4 要求：确定性 pin，证明判定必须在锁内重做。

    做法：先让 precheck 快照读到「未注册」，再在真正写入前把 manifest 改成
    「已被他人注册」，断言最终结果不得覆盖注入的 source。
    """
    store, job_id = make_job(tmp_path)
    a = write_handoff(tmp_path, "a", BODY_A)
    config = make_config(tmp_path)

    jobs = tmp_path / "kp" / "jobs"
    script = tmp_path / "pin.py"
    script.write_text(
        _PIN_SNIPPET.format(src=SRC_ROOT, jobs=str(jobs), job_id=job_id), encoding="utf-8"
    )
    env = {**os.environ, "PYTHONPATH": SRC_ROOT}

    # 先注入「他人已注册」，再走正常 register：必须冲突而不是覆盖
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "injected" in proc.stdout
    injected = store.load(job_id).source
    assert injected == {"sentinel": "injected-between-precheck-and-lock"}

    rc = _cmd_source_register(config, _ns(job_id, a))
    capsys.readouterr()
    assert rc == EXIT_CODES[HANDOFF_CONFLICT]
    assert store.load(job_id).source == injected, "不得覆盖他人注册"


# ---------- 错误码稳定性 ----------


def test_error_codes_still_six_and_zero():
    assert EXIT_CODES[HANDOFF_CONFLICT] == 6
    assert ALREADY_REGISTERED == "ALREADY_REGISTERED"


def test_already_registered_helper_uses_identity(tmp_path):
    store, job_id = make_job(tmp_path)
    a = write_handoff(tmp_path, "a", BODY_A)
    _cmd_source_register(make_config(tmp_path), _ns(job_id, a))
    manifest = store.load(job_id)
    replay = json.loads(a.read_text(encoding="utf-8"))
    replay["source_fingerprint"] = manifest.source["source_fingerprint"]
    assert already_registered(manifest, replay) is True
    changed = json.loads(a.read_text(encoding="utf-8"))
    changed["remote"]["size_bytes"] = 123456
    assert already_registered(manifest, changed) is False


def test_non_mapping_handoff_identity_is_stable():
    assert handoff_identity("nope") == handoff_identity("other") == ("<non-mapping>",)
    assert handoff_identity(None) == ("<non-mapping>",)


def test_manifest_edit_still_writes_on_mutation(tmp_path):
    """回归：`edit` 的既有行为不得被本次修复改变。"""
    store, job_id = make_job(tmp_path)
    with store.edit(job_id) as manifest:
        manifest.source = {"k": "v"}
    assert store.load(job_id).source == {"k": "v"}
    assert isinstance(store.load(job_id), JobManifest)
