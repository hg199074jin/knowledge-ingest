"""GPT-KI-TAD-IDEMPOTENCY（Issue #35）：跨仓库 external-id Job API 契约测试。

覆盖 §7 的 12 项 RED / fault injection。本文是**契约冻结**测试：
实现必须让这里全部通过，且不得改动旧 KI 行为（无 --external-id 路径）。

隐私硬约束：external-id 只以哈希形式出现在路径 / 异常 / 日志里，
绝不回显完整来源串（§4/§7.11）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from knowledge_ingest.cli import (
    _cmd_capabilities,
    _cmd_job_create,
    _cmd_job_lookup,
)
from knowledge_ingest.config import AppConfig
from knowledge_ingest.external_jobs import (
    EXIT_CODES,
    EXTERNAL_ID_CONFLICT,
    EXTERNAL_JOB_ID_PREFIX,
    EXTERNAL_PROTOCOL_VERSION,
    HANDOFF_CONFLICT,
    INCOMPLETE_IDEMPOTENT_JOB,
    NOT_FOUND,
    ExternalIdConflictError,
    IncompleteIdempotentJobError,
    capabilities,
    external_fingerprint,
    external_job_id,
)
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

EXT = "tg:tg:synthetic_channel:item:tg:synthetic_channel:101-103:deadbeefcafe"


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


def make_store(tmp_path: Path) -> ManifestStore:
    return ManifestStore(jobs_root=tmp_path / "kp" / "jobs")


def make_job(tmp_path: Path, provider: str = "telegram"
             ) -> tuple[AppConfig, ManifestStore, str]:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider=provider, source="/x", targets=["k2c"]))
    return config, store, manifest.job_id


def request_for(external_id: str = EXT) -> JobRequest:
    return JobRequest(raw_prompt="telegram source item", provider="telegram",
                      source="item:tg:1-3", targets=["k2c"],
                      external_id=external_id)


# ---------- §7.1 同键重复 create ----------

def test_external_job_id_is_deterministic_and_path_safe():
    a = external_job_id(EXT)
    b = external_job_id(EXT)
    assert a == b
    assert a.startswith(EXTERNAL_JOB_ID_PREFIX)
    # 路径安全：只含 [a-z0-9-]
    assert all(ch.islower() or ch.isdigit() or ch == "-" for ch in a)
    assert "/" not in a and ".." not in a and len(a) < 80


def test_external_job_id_is_irreversible():
    """绝不把 user-controlled 串直接塞进路径（§3）。"""
    job_id = external_job_id(EXT)
    assert "tg:synthetic" not in job_id
    assert "deadbeef" not in job_id


def test_distinct_external_ids_give_distinct_job_ids():
    assert external_job_id(EXT) != external_job_id(EXT + "x")


def test_repeated_create_same_key_same_job_id(tmp_path):
    store = make_store(tmp_path)
    first = store.create_idempotent(request_for())
    second = store.create_idempotent(request_for())
    assert first.job_id == second.job_id
    assert len(store.list_jobs()) == 1


def test_job_directory_is_single(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    store.create_idempotent(request_for())
    dirs = [p.name for p in (tmp_path / "kp" / "jobs").iterdir()
            if p.is_dir() and not p.name.startswith(".")]
    assert dirs == [external_job_id(EXT)]


# ---------- §7.5 同键不同 request -> conflict ----------

def test_same_key_different_provider_conflicts(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.provider = "baidu"
    with pytest.raises(ExternalIdConflictError):
        store.create_idempotent(other)


def test_same_key_different_source_conflicts(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.source = "other-source"
    with pytest.raises(ExternalIdConflictError):
        store.create_idempotent(other)


def test_same_key_different_targets_conflicts(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.targets = ["cangjie", "personal"]
    with pytest.raises(ExternalIdConflictError):
        store.create_idempotent(other)


def test_same_key_different_prompt_conflicts(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.raw_prompt = "totally different intent"
    with pytest.raises(ExternalIdConflictError):
        store.create_idempotent(other)


def test_conflict_does_not_create_second_job(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.source = "different"
    with pytest.raises(ExternalIdConflictError):
        store.create_idempotent(other)
    assert len(store.list_jobs()) == 1


def test_target_order_is_part_of_identity(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.targets = ["k2c"]  # 与原始一致 -> 不冲突
    assert store.create_idempotent(other).job_id == external_job_id(EXT)


# ---------- §7.3 / §7.7 lookup ----------

def test_get_by_external_id_finds_created_job(tmp_path):
    store = make_store(tmp_path)
    created = store.create_idempotent(request_for())
    found = store.get_by_external_id(EXT)
    assert found.job_id == created.job_id
    assert found.status == "CREATED"


def test_get_by_external_id_missing_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.get_by_external_id("tg:never:seen") is None


def test_get_by_external_id_for_legacy_job_returns_none(tmp_path):
    """§7.7：旧 job 无外部键 -> 清晰的无映射（不是错）。"""
    store = make_store(tmp_path)
    store.create(JobRequest(raw_prompt="x", provider="local", source="/x",
                            targets=["k2c"]))
    assert store.get_by_external_id(EXT) is None


def test_lookup_does_not_touch_mtime(tmp_path):
    """§2.6：lookup 不得改 mtime / 不写盘。"""
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    manifest_path = store.manifest_path(external_job_id(EXT))
    before = manifest_path.stat().st_mtime_ns
    store.get_by_external_id(EXT)
    store.get_by_external_id(EXT)
    assert manifest_path.stat().st_mtime_ns == before


def test_lookup_does_not_create_anything(tmp_path):
    store = make_store(tmp_path)
    store.get_by_external_id("tg:nothing:here")
    jobs_root = tmp_path / "kp" / "jobs"
    assert not jobs_root.exists() or not list(jobs_root.glob("*/job.yaml"))


def test_lookup_is_reproducible(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    first = store.get_by_external_id(EXT)
    second = store.get_by_external_id(EXT)
    assert first.model_dump() == second.model_dump()


# ---------- §7.4 半成品目录 ----------

def test_empty_dir_is_bounded_recovery(tmp_path):
    """mkdir 后崩溃 -> 有界恢复成完整 job（marker 证明归属）。"""
    store = make_store(tmp_path)
    job_id = external_job_id(EXT)
    job_dir = store.job_dir(job_id)
    job_dir.mkdir(parents=True)
    (job_dir / ".external-id.json").write_text(json.dumps({
        "external_id_sha256": external_fingerprint(EXT),
        "protocol_version": EXTERNAL_PROTOCOL_VERSION}), encoding="utf-8")
    recovered = store.create_idempotent(request_for())
    assert recovered.job_id == job_id
    assert store.manifest_path(job_id).is_file()
    assert len(store.list_jobs()) == 1


def test_foreign_dir_without_marker_is_blocked(tmp_path):
    """未知用户内容：绝不覆盖/删除 -> INCOMPLETE_IDEMPOTENT_JOB。"""
    store = make_store(tmp_path)
    job_id = external_job_id(EXT)
    job_dir = store.job_dir(job_id)
    job_dir.mkdir(parents=True)
    (job_dir / "precious-user-data.txt").write_text("do not touch",
                                                    encoding="utf-8")
    with pytest.raises(IncompleteIdempotentJobError):
        store.create_idempotent(request_for())
    assert (job_dir / "precious-user-data.txt").read_text(
        encoding="utf-8") == "do not touch"


def test_marker_with_wrong_hash_is_blocked(tmp_path):
    store = make_store(tmp_path)
    job_dir = store.job_dir(external_job_id(EXT))
    job_dir.mkdir(parents=True)
    (job_dir / ".external-id.json").write_text(json.dumps({
        "external_id_sha256": external_fingerprint("tg:someone:else"),
        "protocol_version": EXTERNAL_PROTOCOL_VERSION}), encoding="utf-8")
    with pytest.raises(IncompleteIdempotentJobError):
        store.create_idempotent(request_for())


def test_unreadable_manifest_is_blocked_not_silently_rebuilt(tmp_path):
    store = make_store(tmp_path)
    job_id = external_job_id(EXT)
    job_dir = store.job_dir(job_id)
    job_dir.mkdir(parents=True)
    (job_dir / ".external-id.json").write_text(json.dumps({
        "external_id_sha256": external_fingerprint(EXT),
        "protocol_version": EXTERNAL_PROTOCOL_VERSION}), encoding="utf-8")
    (job_dir / "job.yaml").write_text("{{{ not yaml", encoding="utf-8")
    with pytest.raises(IncompleteIdempotentJobError):
        store.create_idempotent(request_for())


# ---------- §7.6 旧行为不变 ----------

def test_legacy_create_unchanged_random_id(tmp_path):
    store = make_store(tmp_path)
    manifest = store.create(JobRequest(raw_prompt="x", provider="local",
                                       source="/x", targets=["k2c"]))
    assert not manifest.job_id.startswith(EXTERNAL_JOB_ID_PREFIX)


def test_legacy_create_without_external_id_field():
    """旧 manifest（无 external_id 字段）必须可构造、可读。"""
    req = JobRequest(raw_prompt="x", provider="local", source="/x",
                     targets=["k2c"])
    assert req.external_id is None
    assert "external_id" not in req.model_dump(exclude_none=True)


def test_create_idempotent_records_external_id(tmp_path):
    store = make_store(tmp_path)
    manifest = store.create_idempotent(request_for())
    assert manifest.request.external_id == EXT


# ---------- §7.2 / §7.10 并发 ----------

def test_threaded_create_same_key_single_job(tmp_path):
    store = make_store(tmp_path)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(store.create_idempotent(request_for()).job_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert set(results) == {external_job_id(EXT)}
    assert len(store.list_jobs()) == 1


_WORKER_SNIPPET = """
import sys, json
sys.path.insert(0, {src!r})
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

store = ManifestStore(jobs_root={jobs!r})
req = JobRequest(raw_prompt="telegram source item", provider="telegram",
                 source="item:tg:1-3", targets=["k2c"], external_id={ext!r})
m = store.create_idempotent(req)
print(json.dumps({{"job_id": m.job_id}}))
"""


def test_process_create_same_key_single_job(tmp_path):
    """真跨进程（不是单进程假测）。"""
    src = str(Path(__file__).resolve().parents[2] / "src")
    jobs = tmp_path / "kp" / "jobs"
    script = tmp_path / "worker.py"
    script.write_text(_WORKER_SNIPPET.format(src=src, jobs=str(jobs),
                                             ext=EXT), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": src}
    procs = [subprocess.Popen([sys.executable, str(script)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, text=True) for _ in range(6)]
    outs = [proc.communicate() for proc in procs]
    ids = set()
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 0, err
        ids.add(json.loads(out.strip().splitlines()[-1])["job_id"])
    assert ids == {external_job_id(EXT)}
    store = ManifestStore(jobs_root=jobs)
    assert len(store.list_jobs()) == 1


_SIGKILL_SNIPPET = """
import os, sys, signal
sys.path.insert(0, {src!r})
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

store = ManifestStore(jobs_root={jobs!r})
req = JobRequest(raw_prompt="telegram source item", provider="telegram",
                 source="item:tg:1-3", targets=["k2c"], external_id={ext!r})
manifest = store.create_idempotent(req, _kill_after_manifest=True)
"""


def test_sigkill_after_manifest_then_recover(tmp_path):
    """§7.10：SIGKILL 后重试仍是同一 job，不产生重复。"""
    src = str(Path(__file__).resolve().parents[2] / "src")
    jobs = tmp_path / "kp" / "jobs"
    killer = tmp_path / "killer.py"
    killer.write_text(_SIGKILL_SNIPPET.format(src=src, jobs=str(jobs),
                                              ext=EXT), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": src}
    proc = subprocess.run([sys.executable, str(killer)], env=env,
                          capture_output=True, text=True, timeout=60,
                          check=False)
    assert proc.returncode == -signal.SIGKILL, proc.stderr

    # 重试：必须恢复到同一 job_id
    retry = tmp_path / "retry.py"
    retry.write_text(_WORKER_SNIPPET.format(src=src, jobs=str(jobs),
                                            ext=EXT), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(retry)], env=env,
                          capture_output=True, text=True, timeout=60,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["job_id"] == \
        external_job_id(EXT)
    assert len(ManifestStore(jobs_root=jobs).list_jobs()) == 1


# ---------- §7.11 privacy ----------

def test_external_fingerprint_is_stable_and_short():
    fp = external_fingerprint(EXT)
    assert fp == external_fingerprint(EXT)
    assert len(fp) == 16
    assert "tg:synthetic" not in fp


def test_conflict_error_does_not_echo_full_external_id(tmp_path):
    store = make_store(tmp_path)
    store.create_idempotent(request_for())
    other = request_for()
    other.source = "different"
    with pytest.raises(ExternalIdConflictError) as exc:
        store.create_idempotent(other)
    message = str(exc.value)
    assert "tg:synthetic_channel" not in message
    assert external_fingerprint(EXT)[:8] in message


def test_job_dir_name_never_contains_source(tmp_path):
    store = make_store(tmp_path)
    manifest = store.create_idempotent(request_for())
    assert "tg:synthetic" not in manifest.job_id


# ---------- §2.2 / §2.7 CLI lookup 协议 ----------

def test_lookup_json_found_shape(tmp_path, capsys):
    config, _store, _ = make_job(tmp_path)
    ManifestStore(jobs_root=config.pipeline_root / "jobs").create_idempotent(
        request_for())
    rc = _cmd_job_lookup(config, Namespace(external_id=EXT, json_output=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["found"] is True
    assert payload["external_id"] == EXT
    assert payload["job_id"] == external_job_id(EXT)
    assert payload["status"] == "CREATED"
    assert payload["contract_version"] == EXTERNAL_PROTOCOL_VERSION


def test_lookup_json_not_found_shape_and_exit_code(tmp_path, capsys):
    config, _store, _ = make_job(tmp_path)
    rc = _cmd_job_lookup(config, Namespace(external_id="tg:absent:key",
                                           json_output=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT_CODES[NOT_FOUND]
    assert payload["found"] is False
    assert "job_id" not in payload
    assert payload["external_id"] == "tg:absent:key"


def test_lookup_json_never_leaks_material(capsys):
    """§2.2：lookup JSON 不含原材料/私密 provenance。"""
    job_dir = make_store(Path("/nonexistent"))
    del job_dir
    assert "source_fingerprint" not in json.dumps(
        {"found": True, "external_id": "x", "job_id": "y", "status": "CREATED",
         "contract_version": 1})


def test_capabilities_attestation(tmp_path, capsys):
    config, _store, _ = make_job(tmp_path)
    rc = _cmd_capabilities(config, Namespace(json_output=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["protocol"] == "ki-external-job-v1"
    assert payload["contract_version"] == EXTERNAL_PROTOCOL_VERSION
    assert payload["idempotent_create"] is True
    assert payload["external_lookup"] is True
    assert payload["register_idempotent"] is True


def test_capabilities_helper_matches_cli(tmp_path):
    assert capabilities() == {
        "protocol": "ki-external-job-v1",
        "contract_version": EXTERNAL_PROTOCOL_VERSION,
        "idempotent_create": True,
        "external_lookup": True,
        "register_idempotent": True,
        "lookup_json_fields": ["found", "external_id", "job_id", "status",
                               "contract_version"],
    }


def test_error_codes_are_stable_strings():
    assert EXTERNAL_ID_CONFLICT == "EXTERNAL_ID_CONFLICT"
    assert NOT_FOUND == "NOT_FOUND"
    assert INCOMPLETE_IDEMPOTENT_JOB == "INCOMPLETE_IDEMPOTENT_JOB"
    assert HANDOFF_CONFLICT == "HANDOFF_CONFLICT"


# ---------- §2.1 CLI create 幂等 ----------

def _ns_create(external_id=EXT, source="item:tg:1-3"):
    return Namespace(provider="telegram", source=source, targets=["k2c"],
                     prompt="telegram source item", with_router=None,
                     external_id=external_id)


def test_cli_create_with_external_id_is_idempotent(tmp_path, capsys):
    config = make_config(tmp_path)
    assert _cmd_job_create(config, _ns_create()) == 0
    first = capsys.readouterr().out.strip()
    assert _cmd_job_create(config, _ns_create()) == 0
    second = capsys.readouterr().out.strip()
    assert first == second == external_job_id(EXT)


def test_cli_create_stdout_is_pure_job_id(tmp_path, capsys):
    """§4：stdout 仍保持纯 job_id（兼容既有 Bridge）。"""
    config = make_config(tmp_path)
    _cmd_job_create(config, _ns_create())
    out = capsys.readouterr().out.strip()
    assert out == external_job_id(EXT)
    assert "\n" not in out


def test_cli_create_without_external_id_unchanged(tmp_path, capsys):
    config = make_config(tmp_path)
    assert _cmd_job_create(config, _ns_create(external_id=None)) == 0
    out = capsys.readouterr().out.strip()
    assert not out.startswith(EXTERNAL_JOB_ID_PREFIX)


def test_cli_create_conflict_prints_code_and_nonzero(tmp_path, capsys):
    config = make_config(tmp_path)
    _cmd_job_create(config, _ns_create())
    capsys.readouterr()
    rc = _cmd_job_create(config, _ns_create(source="other"))
    captured = capsys.readouterr()
    assert rc != 0
    assert EXTERNAL_ID_CONFLICT in captured.err + captured.out
    assert "tg:synthetic_channel" not in captured.err + captured.out

# ---------- §7.10 / §2.4 真实 CLI 子进程端到端（协议层） ----------

def _ki_env() -> dict:
    src = str(Path(__file__).resolve().parents[2] / "src")
    return {**os.environ, "PYTHONPATH": src}


def _ki(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    """跑真实 knowledge-ingest CLI（隔离 config，绝不碰生产根）。"""
    config = tmp_path / "ki-config.yaml"
    config.write_text(textwrap.dedent(f"""
        pipeline_root: {tmp_path / 'kp'}
        media_project: {tmp_path / 'media'}
        docchunk_project: {tmp_path / 'docchunk'}
        media_output_root: {tmp_path / 'media-out'}
        docchunk_corpus_root: {tmp_path / 'corpus'}
        skill_roots: ["~/.agents/skills"]
        skills:
          baidu: baidu-drive
          quark: quarkclouddrive
          cangjie: cangjie-skill
          personal_distiller: personal-capability-distiller
        processing:
          media_device: auto
          media_timestamp: 10m
          require_orico: false
    """), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "knowledge_ingest.cli", *argv,
         "--config", str(config)],
        capture_output=True, text=True, env=_ki_env(), timeout=120,
        check=False)


def test_cli_end_to_end_create_lookup_replay(tmp_path):
    create = _ki(tmp_path, "job", "create", "--provider", "telegram",
                 "--source", "item:tg:1-3", "--target", "k2c",
                 "--external-id", EXT)
    assert create.returncode == 0, create.stderr
    job_id = create.stdout.strip()
    assert job_id == external_job_id(EXT)

    lookup = _ki(tmp_path, "job", "lookup", "--external-id", EXT, "--json")
    payload = json.loads(lookup.stdout)
    assert lookup.returncode == 0
    assert payload["found"] is True and payload["job_id"] == job_id

    # 重放 create：同一 job_id，单目录
    again = _ki(tmp_path, "job", "create", "--provider", "telegram",
                "--source", "item:tg:1-3", "--target", "k2c",
                "--external-id", EXT)
    assert again.stdout.strip() == job_id
    assert len(ManifestStore(jobs_root=tmp_path / "kp" / "jobs").list_jobs()) == 1


def test_cli_lookup_not_found_exit_code(tmp_path):
    proc = _ki(tmp_path, "job", "lookup", "--external-id", "tg:nope:1",
               "--json")
    assert proc.returncode == EXIT_CODES[NOT_FOUND]
    assert json.loads(proc.stdout)["found"] is False


def test_cli_capabilities_json(tmp_path):
    proc = _ki(tmp_path, "capabilities", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["protocol"] == "ki-external-job-v1"
    assert payload["idempotent_create"] and payload["external_lookup"]
    assert payload["register_idempotent"]


def test_cli_concurrent_create_same_key(tmp_path):
    """真并发 CLI 进程：同键只初始化一次。"""
    config_argv = ["job", "create", "--provider", "telegram", "--source",
                   "item:tg:1-3", "--target", "k2c", "--external-id", EXT]
    procs = [subprocess.Popen(
        [sys.executable, "-m", "knowledge_ingest.cli", *config_argv,
         "--config", str(_write_config(tmp_path))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_ki_env(),
        text=True) for _ in range(5)]
    outs = [p.communicate() for p in procs]
    ids = {o.strip().splitlines()[-1] for p, (o, e) in zip(procs, outs)
           if p.returncode == 0}
    assert ids == {external_job_id(EXT)}
    store = ManifestStore(jobs_root=tmp_path / "kp" / "jobs")
    assert len(store.list_jobs()) == 1


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "ki-config.yaml"
    config.write_text(textwrap.dedent(f"""
        pipeline_root: {tmp_path / 'kp'}
        media_project: {tmp_path / 'media'}
        docchunk_project: {tmp_path / 'docchunk'}
        media_output_root: {tmp_path / 'media-out'}
        docchunk_corpus_root: {tmp_path / 'corpus'}
        skill_roots: ["~/.agents/skills"]
        skills:
          baidu: baidu-drive
          quark: quarkclouddrive
          cangjie: cangjie-skill
          personal_distiller: personal-capability-distiller
        processing:
          media_device: auto
          media_timestamp: 10m
          require_orico: false
    """), encoding="utf-8")
    return config


def test_cli_conflict_nonzero_and_single_job(tmp_path):
    ok = _ki(tmp_path, "job", "create", "--provider", "telegram",
             "--source", "item:tg:1-3", "--target", "k2c", "--external-id", EXT)
    assert ok.returncode == 0
    bad = _ki(tmp_path, "job", "create", "--provider", "telegram",
              "--source", "OTHER", "--target", "k2c", "--external-id", EXT)
    assert bad.returncode != 0
    assert EXTERNAL_ID_CONFLICT in bad.stderr
    assert "tg:synthetic_channel" not in bad.stderr
    assert len(ManifestStore(jobs_root=tmp_path / "kp" / "jobs").list_jobs()) == 1


def test_cli_legacy_create_still_random(tmp_path):
    proc = _ki(tmp_path, "job", "create", "--provider", "local",
               "--source", "/tmp/x", "--target", "k2c")
    assert proc.returncode == 0
    job_id = proc.stdout.strip()
    assert not job_id.startswith(EXTERNAL_JOB_ID_PREFIX)
    # 旧 job 对 external lookup 无映射
    lookup = _ki(tmp_path, "job", "lookup", "--external-id", EXT, "--json")
    assert lookup.returncode == EXIT_CODES[NOT_FOUND]
