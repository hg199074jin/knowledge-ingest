"""TG6: Telegram Source Item → Source Handoff v2 → KI Job（方案 §8）。

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §13.4/§13.5/
§14/§15；幂等（§8.4/§19.3）；自动化止于 staged（§8.6）。
"""

import asyncio
import json
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest
from knowledge_ingest.telegram.event_store import TelegramEventStore
from knowledge_ingest.telegram.handoff import (
    TelegramHandoffRunner,
    important_capability_signal,
)


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
            "k2c": "k2c",
        },
        "processing": {"media_device": "auto", "media_timestamp": "10m",
                       "require_orico": False},
    })


def make_store(tmp_path: Path) -> TelegramEventStore:
    return TelegramEventStore(tmp_path / "telegram" / "state.db")


def seeded_item(tmp_path: Path, *, body="这是一段足够长的知识正文，" * 4,
                deleted=False, status="materialized", job_id=None,
                kind="text", local_name="message.md",
                interest=None):
    """造一个已物化的 item：message.md 真实存在（TG1 完成门要求）。"""
    store = TelegramEventStore(tmp_path / "telegram" / "state.db")
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     display_name="AI探索指南",
                     start_at="2026-09-18T09:00:00+08:00")
    materialized = None
    if kind == "text":
        materialized = tmp_path / "telegram" / "materialized" / "tg_a:1"
        materialized.mkdir(parents=True, exist_ok=True)
        (materialized / "message.md").write_text(body, encoding="utf-8")
    elif kind == "pdf":
        materialized = tmp_path / "telegram" / "attachments" / "tg_a:1"
        materialized.mkdir(parents=True, exist_ok=True)
        (materialized / "document.pdf").write_bytes(b"%PDF fake")
        interest = interest or "INCLUDE"
    store.create_source_item("tg_a:1", "tg_a", kind, [7],
                             processing_status=status)
    if kind == "pdf":
        store.upsert_download("tg_a:1", 7, status="complete", attempts=1,
                              expected_size_bytes=1024,
                              local_path=str(materialized / "document.pdf"))
    if deleted:
        store.upsert_message("tg_a", 7, message_date=(
            "2026-09-18T09:01:00+08:00"), text=body)
        store.mark_message_deleted("tg_a", 7)
    else:
        store.upsert_message("tg_a", 7, message_date=(
            "2026-09-18T09:01:00+08:00"), sender_id="99", text=body)
    if status in ("materialized", "interest_include"):
        target = (materialized / ("document.pdf" if kind == "pdf"
                                  else "message.md"))
        store.set_item_materialized("tg_a:1", str(target))
    if interest:
        store.set_item_interest("tg_a:1", interest)
    if job_id:
        store.set_item_handoff("tg_a:1", job_id)
    return store, (materialized / ("document.pdf" if kind == "pdf"
                                   else "message.md"))


def make_runner(tmp_path: Path, store: TelegramEventStore):
    config = make_config(tmp_path)
    runner = TelegramHandoffRunner(store, config)
    return config, runner


def registered_manifest(config, job_id):
    return ManifestStore(
        jobs_root=config.pipeline_root / "jobs").load(job_id)


# ---------- handoff v2 形状（冻结 §13.4） ----------

def test_build_handoff_v2_shape(tmp_path):
    store, path = seeded_item(tmp_path)
    _config, runner = make_runner(tmp_path, store)
    handoff = runner.build_handoff_v2("tg_a:1")
    assert handoff["schema_version"] == 2
    assert handoff["provider"] == "telegram"
    assert handoff["local_path"] == str(path)
    assert handoff["download_completed"] is True
    prov = handoff["provenance"]
    assert prov["platform"] == "telegram"
    assert prov["source_id"] == "tg_a"
    assert prov["chat_id"] == -1001234567890
    assert prov["message_ids"] == [7]
    assert prov["sender_id"] == "99"
    assert prov["source_deleted"] is False


def test_build_handoff_v2_records_source_deleted(tmp_path):
    store, _ = seeded_item(tmp_path, deleted=True, status="deleted_source")
    _config, runner = make_runner(tmp_path, store)
    handoff = runner.build_handoff_v2("tg_a:1")
    assert handoff["provenance"]["source_deleted"] is True


# ---------- prepare_handoff：创建 + 写 source.json + 注册 + 标记 ----------

def test_prepare_handoff_creates_and_registers(tmp_path):
    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    result = runner.prepare_handoff("tg_a:1")
    assert result["job_id"]
    manifest = registered_manifest(config, result["job_id"])
    assert manifest.request.provider == "telegram"
    assert manifest.status in ("DISCOVERING", "DOWNLOADED")
    assert manifest.source["schema_version"] == 2
    assert manifest.source["provenance"]["chat_id"] == -1001234567890
    handoff_file = (config.pipeline_root / "jobs" / result["job_id"]
                    / "handoff" / "source.json")
    assert json.loads(handoff_file.read_text(encoding="utf-8"))[
        "provider"] == "telegram"
    item = store.get_source_item("tg_a:1")
    assert item["knowledge_ingest_job_id"] == result["job_id"]
    assert item["handoff_completed"] == 1


def test_prepare_handoff_idempotent_no_second_job(tmp_path):
    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    first = runner.prepare_handoff("tg_a:1")
    second = runner.prepare_handoff("tg_a:1")
    assert first == second  # 同 job，不再创建
    jobs = list((config.pipeline_root / "jobs").iterdir())
    assert len([p for p in jobs if p.is_dir()]) == 1


def test_prepare_handoff_resumes_after_crash_between_create_and_register(
        tmp_path):
    """崩溃恢复：job 已建但 item 未标记（register 前崩溃）→ 续注册，
    不新建第二个 job。"""
    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    store_km = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    manifest = store_km.create(JobRequest(
        raw_prompt="telegram source item tg_a:1", provider="telegram",
        source="tg_a:1", targets=["k2c"]))
    store.set_item_handoff("tg_a:1", manifest.job_id)  # 标记了 job，
    # 但 handoff 文件/注册都还没发生（模拟崩溃点）

    result = runner.prepare_handoff("tg_a:1")
    assert result["job_id"] == manifest.job_id           # 复用，不新建
    assert registered_manifest(config, manifest.job_id).status in (
        "DISCOVERING", "DOWNLOADED")


# ---------- 门控（§8.3/§6.4/§6.6） ----------

def test_short_text_not_handed_off(tmp_path):
    store, _ = seeded_item(tmp_path, body="园区")  # 2 字符垃圾
    _config, runner = make_runner(tmp_path, store)
    assert runner.prepare_handoff("tg_a:1") is None
    assert store.get_source_item("tg_a:1")["processing_status"] == \
        "skipped_too_short"


def test_deleted_source_not_handed_off(tmp_path):
    store, _ = seeded_item(tmp_path, deleted=True,
                           status="deleted_source")
    _config, runner = make_runner(tmp_path, store)
    assert runner.prepare_handoff("tg_a:1") is None
    assert store.get_source_item("tg_a:1")["knowledge_ingest_job_id"] \
        is None


def test_pending_resource_not_handed_off(tmp_path):
    store = make_store(tmp_path)
    store.add_source(source_id="tg_a", chat_id=-1001234567890,
                     start_at="2026-09-18T09:00:00+08:00")
    store.create_source_item("tg_a:5", "tg_a", "cloud_link", [5],
                             processing_status="PENDING_RESOURCE")
    _config, runner = make_runner(tmp_path, store)
    assert runner.prepare_handoff("tg_a:5") is None


def test_pdf_include_completed_hands_off_document(tmp_path):
    store, local = seeded_item(tmp_path, kind="pdf",
                               status="interest_include",
                               interest="INCLUDE")
    config, runner = make_runner(tmp_path, store)
    result = runner.prepare_handoff("tg_a:1")
    assert result["job_id"]
    manifest = registered_manifest(config, result["job_id"])
    assert manifest.source["local_path"] == str(local)
    assert manifest.source["local_path"].endswith("document.pdf")


def test_scan_hands_off_all_eligible_and_skips_rest(tmp_path):
    store, _ = seeded_item(tmp_path)                      # 合格项
    store.add_source(source_id="tg_b", chat_id=-2002,
                     start_at="2026-09-18T09:00:00+08:00")
    store.create_source_item("tg_b:9", "tg_b", "cloud_link", [9],
                             processing_status="PENDING_RESOURCE")
    store.create_source_item("tg_b:10", "tg_b", "text", [10],
                             processing_status="deleted_source")
    _config, runner = make_runner(tmp_path, store)
    results = runner.scan()
    assert sorted(r["item_id"] for r in results) == ["tg_a:1"]


# ---------- IMPORTANT_CAPABILITY 生产者核对（§8.7） ----------

def test_important_capability_no_explicit_signal():
    """K2C 现有 manifest 无显式 high-value/important 字段 → 无生产者。"""
    assert important_capability_signal(
        {"status": "completed", "capabilities": []}) is None
    assert important_capability_signal({"staged_assets": ["x"]}) is None


def test_important_capability_explicit_signal_detected():
    manifest = {"status": "completed", "importance": "high"}
    assert important_capability_signal(manifest) == "high"


# ---------- watcher 集成（run 循环里调 scan） ----------

def test_watcher_run_invokes_handoff_scan(tmp_path):
    store, _ = seeded_item(tmp_path)
    _config, runner = make_runner(tmp_path, store)
    scans = []

    async def drive():
        from knowledge_ingest.telegram.watcher import TelegramWatcher
        watcher = TelegramWatcher(store)

        async def scan():
            scans.append(runner.scan())

        await watcher.run(max_ticks=1, idle_seconds=0.01,
                          handoff_scan=scan)

    asyncio.run(drive())
    assert len(scans) == 1
    assert [r["item_id"] for r in scans[0]] == ["tg_a:1"]
    assert scans[0][0]["job_id"]


# ---------- 评审 I2：推进中/BLOCKED/毒丸/尺寸边界回归 ----------

def test_prepare_on_advanced_manifest_is_noop(tmp_path):
    """C1 回归：job 被下游推进后，prepare 绝不重注册（曾抛
    InvalidTransition 杀死 watcher）。"""

    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    first = runner.prepare_handoff("tg_a:1")            # 注册至 DOWNLOADED
    store_km = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    for status in ("ROUTING", "TARGET_RUNNING", "COMPLETED"):
        with store_km.edit(first["job_id"]) as manifest:
            manifest.status = status                    # 模拟下游推进
        result = runner.prepare_handoff("tg_a:1")
        assert result is not None
        assert result["job_id"] == first["job_id"]
        assert store_km.load(first["job_id"]).status == status  # 未回拨


def test_prepare_on_blocked_manifest_never_resurrects(tmp_path):
    """C2 回归：BLOCKED job 不得被 scan 静默复活。"""
    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    first = runner.prepare_handoff("tg_a:1")
    store_km = ManifestStore(jobs_root=config.pipeline_root / "jobs")
    with store_km.edit(first["job_id"]) as manifest:
        manifest.status = "BLOCKED"
    result = runner.prepare_handoff("tg_a:1")
    assert result is not None                           # 幂等返回
    assert store_km.load(first["job_id"]).status == "BLOCKED"  # 仍阻断


def test_scan_poison_item_does_not_starve_rest(tmp_path, monkeypatch):
    """C3 回归：单个毒丸 item 不得饿死排序在后的合格 item。"""
    store = ready_store_twice(tmp_path)
    _config, runner = make_runner(tmp_path, store)

    def poison(item):
        if item["item_id"] == "tg_a:1":
            raise RuntimeError("poison")

    monkeypatch.setattr(runner, "gate", poison)
    results = runner.scan()
    assert [r["item_id"] for r in results] == ["tg_b:2"]


def ready_store_twice(tmp_path):
    store = make_store(tmp_path)
    for source_id in ("tg_a", "tg_b"):
        store.add_source(source_id=source_id,
                         chat_id=-1001234567890 if source_id == "tg_a"
                         else -2002,
                         display_name=source_id,
                         start_at="2026-09-18T09:00:00+08:00")
    for item_id, source in (("tg_a:1", "tg_a"), ("tg_b:2", "tg_b")):
        materialized = tmp_path / "telegram" / "materialized" / item_id
        materialized.mkdir(parents=True, exist_ok=True)
        (materialized / "message.md").write_text(
            "这是一段足够长的知识正文，" * 4, encoding="utf-8")
        store.create_source_item(item_id, source, "text", [7],
                                 processing_status="open")
        store.upsert_message(source, 7, message_date=(
            "2026-09-18T09:01:00+08:00"), sender_id="99", text="正文")
        store.finalize_item(item_id)
        store.set_item_materialized(item_id, str(materialized
                                                 / "message.md"))
    return store


def test_oversize_pdf_blocked_from_handoff(tmp_path):
    """§8.3：>50MiB 即使 DOWNLOAD_ONCE 授权下载完成，也不自动建 job。"""
    store, _ = seeded_item(tmp_path, kind="pdf",
                           status="interest_include", interest="INCLUDE")
    store.upsert_download("tg_a:1", 7, status="complete", attempts=1,
                          expected_size_bytes=80 * 1024 * 1024,
                          local_path=str(
                              tmp_path / "telegram" / "attachments"
                              / "tg_a:1" / "document.pdf"))
    _config, runner = make_runner(tmp_path, store)
    assert runner.gate(store.get_source_item("tg_a:1")) == \
        "blocked:oversize"
    assert runner.prepare_handoff("tg_a:1") is None
    assert store.get_source_item("tg_a:1")["knowledge_ingest_job_id"] is None


# ---------- 评审 R6：handoff 后的删除必须让下游 provenance 看得见 ----------


def test_r6_post_handoff_delete_refreshes_downstream_provenance(tmp_path):
    """§20.2：已 handoff 的 Item 收到删除 → job 的 provenance 记 source_deleted。"""
    from knowledge_ingest.telegram.client_port import (
        TelegramEvent,
        TelegramEventKind,
    )
    from knowledge_ingest.telegram.items import SourceItemPipeline
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    store, _ = seeded_item(tmp_path)
    config, runner = make_runner(tmp_path, store)
    result = runner.prepare_handoff("tg_a:1")
    job_id = result["job_id"]
    handoff_file = (config.pipeline_root / "jobs" / job_id / "handoff" /
                    "source.json")
    assert json.loads(handoff_file.read_text(
        encoding="utf-8"))["provenance"]["source_deleted"] is False

    pipeline = SourceItemPipeline(store, data_root=config.pipeline_root,
                                  orico_check=lambda: True)
    watcher = TelegramWatcher(store, client=None, pipeline=pipeline)
    watcher.handle_event(TelegramEvent(
        kind=TelegramEventKind.DELETE, chat_id=-1001234567890,
        message_id=7))

    assert json.loads(handoff_file.read_text(
        encoding="utf-8"))["provenance"]["source_deleted"] is True
    manifest = registered_manifest(config, job_id)
    assert manifest.source["provenance"]["source_deleted"] is True
    # 跨层不回滚：Corpus 侧文件与 job 状态不动
    assert manifest.status == "DOWNLOADED"
