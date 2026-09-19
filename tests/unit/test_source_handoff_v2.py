"""TG1: Source Handoff v2 — provider=telegram, schema v1/v2 并存。

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §13.4/§13.5。
完成门语义：version in {1,2}；provider 一致；download_completed；
local_path 存在；telegram 的 provenance 最小契约（未知键容忍）。
"""

import json
from argparse import Namespace
from pathlib import Path

from knowledge_ingest.cli import _cmd_source_register
from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "pipeline_root": str(tmp_path / "kp"),
        "media_project": str(tmp_path / "media"),
        "docchunk_project": str(tmp_path / "docchunk"),
        "media_output_root": str(tmp_path / "media-out"),
        "docchunk_corpus_root": str(tmp_path / "corpus"),
        "skill_roots": ["~/.agents/skills"],
        "skills": {
            "baidu": "baidu-drive", "quark": "quarkclouddrive",
            "cangjie": "cangjie-skill",
            "personal_distiller": "personal-capability-distiller",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


def make_job(tmp_path: Path, provider: str) -> tuple[AppConfig, ManifestStore, str]:
    config = make_config(tmp_path)
    store = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store.create(JobRequest(
        raw_prompt="x", provider=provider, source="/x", targets=["cangjie"]))
    return config, store, manifest.job_id


def write_v1_handoff(tmp_path: Path, provider: str, remote_path: str | None,
                     local_name: str = "x.pdf") -> Path:
    """v1 handoff（inline 构造，与既有 fixtures 同构）。"""
    local_file = tmp_path / local_name
    local_file.write_bytes(b"pdf")
    handoff = {
        "schema_version": 1, "provider": provider,
        "remote": {"id": None, "path": remote_path, "name": "x.pdf",
                   "size_bytes": 10, "mtime": None},
        "local_path": str(local_file),
        "download_completed": True,
        "source_notes": [],
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    return path


def telegram_provenance(**extra) -> dict:
    provenance = {
        "platform": "telegram",
        "source_id": "tg_ai_explore",
        "chat_id": -1001234567890,
        "message_ids": [12345, 12346, 12347],
        "sender_id": "999",
        "message_url": None,
        "first_message_at": "2026-09-18T12:25:00+08:00",
        "last_message_at": "2026-09-18T12:29:00+08:00",
    }
    provenance.update(extra)
    return provenance


def write_telegram_handoff(
    tmp_path: Path, *, provider: str = "telegram", version: int = 2,
    provenance: dict | None = None, omit_provenance: bool = False,
    local_name: str = "message.md", local_path: str | None = None,
    download_completed: bool = True,
) -> Path:
    local_file = tmp_path / local_name
    local_file.write_text("知识正文", encoding="utf-8")
    handoff: dict = {
        "schema_version": version,
        "provider": provider,
        "remote": {"id": "tg_ai_explore:12345-12347", "path": None,
                   "name": "AI探索指南", "size_bytes": None, "mtime": None},
        "local_path": local_path or str(local_file),
        "download_completed": download_completed,
        "source_notes": [],
    }
    if not omit_provenance:
        handoff["provenance"] = (telegram_provenance()
                                 if provenance is None else provenance)
    path = tmp_path / "source.json"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    return path


def register(config, job_id: str, handoff: Path):
    return _cmd_source_register(
        config, Namespace(job_id=job_id, handoff=str(handoff)))


def last_error_reason(store, job_id: str) -> str:
    return store.load(job_id).errors[-1]["reason"]


# ---------- v1 零回归 ----------

def test_v1_local_handoff_still_passes(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "local")
    rc = register(config, job_id, write_v1_handoff(tmp_path, "local", None))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


def test_v1_baidu_handoff_still_passes(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "baidu")
    rc = register(config, job_id,
                  write_v1_handoff(tmp_path, "baidu", "/apps/bdpan/课程.pdf"))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


def test_v1_quark_handoff_still_passes(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "quark")
    rc = register(config, job_id,
                  write_v1_handoff(tmp_path, "quark", "s/52f8xx extractionCode"))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


# ---------- v2 telegram happy path ----------

def test_v2_telegram_handoff_passes(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id, write_telegram_handoff(tmp_path))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


def test_v2_telegram_provenance_readable_after_register(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    register(config, job_id, write_telegram_handoff(tmp_path))
    source = store.load(job_id).source
    assert source["provenance"]["source_id"] == "tg_ai_explore"
    assert source["provenance"]["chat_id"] == -1001234567890
    assert source["provenance"]["message_ids"] == [12345, 12346, 12347]


def test_unknown_provenance_keys_tolerated(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    provenance = telegram_provenance(
        future_field={"nested": True}, another_unknown=[1, 2])
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, provenance=provenance))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


def test_telegram_v1_with_provenance_passes(tmp_path: Path):
    """provenance 契约按 provider 判定而非 version：telegram 必须带身份。"""
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id, write_telegram_handoff(tmp_path, version=1))
    assert rc == 0
    assert store.load(job_id).status != "BLOCKED"


# ---------- v2 telegram 负例 ----------

def test_v2_telegram_provider_mismatch_blocked(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, provider="local"))
    assert rc == 1
    assert last_error_reason(store, job_id) == "provider_mismatch"


def test_v2_telegram_local_path_missing_blocked(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    missing = str(tmp_path / "nope" / "message.md")
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, local_path=missing))
    assert rc == 1
    assert last_error_reason(store, job_id) == "source_missing"


def test_v2_telegram_missing_provenance_blocked(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, omit_provenance=True))
    assert rc == 1
    assert last_error_reason(store, job_id) == "invalid_telegram_provenance"


def test_v2_telegram_wrong_platform_blocked(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    provenance = telegram_provenance(platform="wechat")
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, provenance=provenance))
    assert rc == 1
    assert last_error_reason(store, job_id) == "invalid_telegram_provenance"


def test_v2_telegram_empty_message_ids_blocked(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    provenance = telegram_provenance(message_ids=[])
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, provenance=provenance))
    assert rc == 1
    assert last_error_reason(store, job_id) == "invalid_telegram_provenance"


def test_schema_version_3_fail_fast(tmp_path: Path):
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id, write_telegram_handoff(tmp_path, version=3))
    assert rc == 1
    assert last_error_reason(store, job_id) == "invalid_handoff_schema"


def test_baidu_scope_gate_not_applied_to_telegram(tmp_path: Path):
    """baidu /apps/bdpan 范围门只对 baidu 生效。"""
    config, store, job_id = make_job(tmp_path, "telegram")
    rc = register(config, job_id, write_telegram_handoff(tmp_path))
    assert rc == 0
    loaded = store.load(job_id)
    assert loaded.status != "BLOCKED"
    reasons = [e.get("reason") for e in loaded.errors]
    assert "baidu_scope_limited" not in reasons


def test_v2_telegram_non_int_message_ids_blocked(tmp_path: Path):
    """message_ids 必须是非空 int 列表（身份字段，不允许字符串混入）。"""
    config, store, job_id = make_job(tmp_path, "telegram")
    provenance = telegram_provenance(message_ids=["a", "b"])
    rc = register(config, job_id,
                  write_telegram_handoff(tmp_path, provenance=provenance))
    assert rc == 1
    assert last_error_reason(store, job_id) == "invalid_telegram_provenance"


def test_cli_parser_accepts_telegram_provider():
    """argparse 层锁定 job create --provider telegram 可用。"""
    from knowledge_ingest.cli import _build_parser
    args = _build_parser().parse_args(
        ["job", "create", "--provider", "telegram", "--source", "tg://x",
         "--target", "k2c"])
    assert args.provider == "telegram"


def test_handoff_v2_example_carries_source_deleted():
    """评审 R7：v2 规范样例必须包含运行时恒定输出的 source_deleted。"""
    root = Path(__file__).resolve().parents[2]
    example = json.loads(
        (root / "schemas" / "source-handoff.example.json")
        .read_text(encoding="utf-8"))
    assert example["schema_version"] == 2
    assert example["provenance"]["source_deleted"] is False
