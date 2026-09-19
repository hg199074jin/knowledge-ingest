"""TG6: Telegram Source Item → knowledge-ingest → target=k2c 编排。

冻结依据：docs/k2c-telegram-knowledge-source-v2-design.md §13.4/§15；
方案 §8（幂等 / 门控 / 自动化止于 staged / IMPORTANT_CAPABILITY
生产者核对）。本模块只做"接上线"：下游 route/preprocess/docchunk/
K2C 全部复用 knowledge-ingest 既有机器（§8.5 不得旁路）。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest

from .event_store import TelegramEventStore
from .materialize import MAX_AUTO_DOWNLOAD_BYTES

MIN_HANDOFF_TEXT_CHARS = 20          # §6 观察：短垃圾（"园区"）不进 Corpus
DOWNLOAD_COMPLETE = "complete"


def important_capability_signal(k2c_manifest: dict) -> str | None:
    """§8.7 生产者核对：仅识别 K2C manifest 里**显式**的
    high-value / important 信号；没有显式信号 → None（= 当前无
    生产者），绝不发明启发式，也绝不把所有 staged 默认视为重要。
    """
    if not isinstance(k2c_manifest, dict):
        return None
    for key in ("importance", "high_value", "priority"):
        value = k2c_manifest.get(key)
        if isinstance(value, str) and value.lower() in ("high", "important"):
            return value
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


class TelegramHandoffRunner:
    """把合格（materialized / interest_include+download complete）的
    Source Item 交付给 knowledge-ingest（handoff v2 → job → register）。

    幂等（§8.4）：确定性以 item 的 knowledge_ingest_job_id 为准——
    未标记才建 job；标记了但没注册完（崩溃点）→ 复用同 job 续注册。
    """

    def __init__(self, store: TelegramEventStore, config):
        self.store = store
        self.config = config

    # ---- 资格判定（§8.2/§8.3/§6.4/§6.6 门控） ----

    def gate(self, item) -> str | None:
        """返回 None = 允许 handoff；字符串 = 拒绝原因。"""
        status = item["processing_status"]
        if status in ("deleted_source", "skipped_noise", "skipped_interest",
                      "skipped_too_short", "empty_item", "noise_review",
                      "interest_review"):
            return f"blocked:{status}"
        if status == "PENDING_RESOURCE":
            return "blocked:pending_resource"   # §6.6：Stub 不进 KI
        kind = item["kind"]
        if kind == "text":
            body_file = item["materialized_path"]
            if not body_file or not Path(body_file).is_file():
                return "blocked:not_materialized"
            body = Path(body_file).read_text(encoding="utf-8")
            body_len = len(body.replace(
                "# Telegram Knowledge Item", "").strip())
            if body_len < MIN_HANDOFF_TEXT_CHARS:
                self.store.set_item_processing_status(
                    item["item_id"], "skipped_too_short")
                return "blocked:too_short"
            return None
        if kind in ("pdf", "video"):
            download = self.store.get_download(item["item_id"])
            if (download is None
                    or download["status"] != DOWNLOAD_COMPLETE
                    or not download["local_path"]
                    or not Path(download["local_path"]).is_file()):
                return "blocked:download_incomplete"
            if item["interest_decision"] != "INCLUDE":
                return "blocked:not_included"   # §8.3：不得偷跑
            expected = download["expected_size_bytes"]
            if expected is None or expected > MAX_AUTO_DOWNLOAD_BYTES:
                # §8.3：>50MiB（含 DOWNLOAD_ONCE 授权下载完成的）不自动
                # 建 job——人工需要时走 CLI 显式创建
                return "blocked:oversize"
            return None
        return f"blocked:unknown_kind:{kind}"

    # ---- handoff v2（冻结 §13.4 示例结构） ----

    def build_handoff_v2(self, item_id: str) -> dict:
        item = self.store.get_source_item(item_id)
        source = self.store.get_source(item["source_id"])
        messages = self.store.get_item_messages(item_id)
        senders = [row["sender_id"] for row in messages
                   if row["sender_id"]]
        local_path = (item["materialized_path"]
                      or (self.store.get_download(item_id)["local_path"]
                          if self.store.get_download(item_id) else None))
        local = Path(local_path) if local_path else None
        return {
            "schema_version": 2,
            "provider": "telegram",
            "remote": {
                "id": (f"{item['source_id']}:"
                       f"{item['first_message_id']}-{item['last_message_id']}"),
                "path": None,
                "name": source["display_name"] if source else item_id,
                "size_bytes": local.stat().st_size if local
                and local.is_file() else None,
                "mtime": (messages[-1]["message_date"]
                          if messages else None),
            },
            "local_path": str(local) if local else "",
            "download_completed": True,
            "source_notes": [],
            "provenance": {
                "platform": "telegram",
                "source_id": item["source_id"],
                "chat_id": source["chat_id"] if source else None,
                "message_ids": json.loads(item["message_ids_json"]),
                "sender_id": senders[0] if senders else None,
                "message_url": None,
                "first_message_at": (messages[0]["message_date"]
                                     if messages else None),
                "last_message_at": (messages[-1]["message_date"]
                                    if messages else None),
                # §20.2：下游必须能知道原消息后来被删除
                "source_deleted": any(row["deleted_at"] for row in messages),
            },
        }

    # ---- 交付 ----

    def prepare_handoff(self, item_id: str) -> dict | None:
        from knowledge_ingest.cli import _cmd_source_register

        item = self.store.get_source_item(item_id)
        if item is None:
            return None
        jobs_root = self.config.pipeline_root / "jobs"
        marked_job = item["knowledge_ingest_job_id"]
        manifest_file = jobs_root / marked_job / "job.yaml" \
            if marked_job else None
        if manifest_file is not None and manifest_file.is_file():
            # 评审 C1/C2：凡 job.yaml 存在且状态 ≠ CREATED，说明注册
            # 已发生过（甚至已被下游推进/人工阻断）——一律幂等早退。
            # 重注册会对推进中的 manifest 抛 InvalidTransition（杀死
            # watcher），并会把 BLOCKED job 每 5 分钟静默复活。
            manifest = ManifestStore(jobs_root=jobs_root).load(marked_job)
            if manifest.status != "CREATED":
                return {"item_id": item_id, "job_id": marked_job,
                        "status": item["processing_status"]}
            # 完整交付过的：幂等返回；仅当 handoff 文件缺失或尚未
            # 注册成功（崩溃点）时才继续走补注册
        reason = self.gate(item)
        if reason is not None:
            return None

        if marked_job:
            job_id = marked_job                 # 崩溃恢复：复用既有 job
        else:
            store_km = ManifestStore(jobs_root=jobs_root)
            manifest = store_km.create(JobRequest(
                raw_prompt=f"telegram source item {item_id}",
                provider="telegram", source=item_id, targets=["k2c"]))
            job_id = manifest.job_id
            # 先标记再注册：注册幂等可重放，job 不可重复创建（§8.4）
            self.store.set_item_handoff(item_id, job_id)

        handoff_path = jobs_root / job_id / "handoff" / "source.json"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_path.write_text(
            json.dumps(self.build_handoff_v2(item_id), ensure_ascii=False,
                       indent=2), encoding="utf-8")
        import argparse

        rc = _cmd_source_register(
            self.config,
            argparse.Namespace(job_id=job_id,
                               handoff=str(handoff_path)))
        if rc != 0:
            return None
        fresh = self.store.get_source_item(item_id)
        return {"item_id": item_id, "job_id": job_id,
                "status": fresh["processing_status"]}

    def scan(self) -> list[dict]:
        results = []
        for item in self.store.list_source_items():
            if item["kind"] not in ("text", "pdf"):
                continue
            try:
                result = self.prepare_handoff(item["item_id"])
            except Exception as exc:            # noqa: BLE001 —— 单项隔离
                print(f"handoff error {item['item_id']}: {exc}",
                      file=sys.stderr, flush=True)
                continue
            if result is not None:
                results.append(result)
        return results
