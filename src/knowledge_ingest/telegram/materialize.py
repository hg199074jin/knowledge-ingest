"""TG4: 物化与 PDF 下载（冻结设计 §12/§13；方案 §6.3/§6.5/§6.7/§6.8）。

ORICO fail-closed：下载/物化前必须实测数据根可用，绝不 fallback
内置盘，绝不读取 require_orico 死配置。
"""

import asyncio
from pathlib import Path

from knowledge_ingest.fingerprint import fingerprint_file

ORICO_ROOT = Path("/Volumes/ORICO")
TEXT_HEADER = "# Telegram Knowledge Item"
MAX_AUTO_DOWNLOAD_BYTES = 50 * 1024 * 1024  # §12：≤50 MiB 自动，>50 需授权


class OricoUnavailableError(RuntimeError):
    """ORICO 数据根不可用（§6.8 fail-closed）。"""


def orico_ready() -> bool:
    return ORICO_ROOT.is_dir()


def materialize_text_item(store, item_id: str, base_dir: str | Path, *,
                          orico_check=orico_ready) -> Path:
    """§13.1：固定标题 + 原文按序拼接；provenance 不进正文；
    已删除消息的正文不进 Corpus（§20.2），删除事实留在 tg_messages。"""
    if not orico_check():
        raise OricoUnavailableError(
            "ORICO data root unavailable; refusing to materialize "
            "(fail-closed, §6.8)")
    item = store.get_source_item(item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    texts = [row["text"] for row in store.get_item_messages(item_id)
             if row["text"] and not row["deleted_at"]]
    body = "\n\n".join(texts)
    directory = Path(base_dir) / "materialized" / item_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "message.md"
    path.write_text(f"{TEXT_HEADER}\n\n{body}\n", encoding="utf-8")
    store.set_item_materialized(item_id, str(path))
    return path


def download_pdf(store, item_id: str, client, data_root: str | Path, *,
                 decision: str, orico_check=orico_ready) -> Path | None:
    """§6.5/§6.7：decision=INCLUDE 才下载；>50 MiB 必须已持有
    DOWNLOAD_ONCE 授权（download_gate 裁决 claim/retry/completed）；
    attempts 与状态走短事务；ORICO fail-closed。

    返回 None = 本轮不下载（denied / completed / 非 INCLUDE）。
    瞬时失败：downloads 行记 failed + last_error 后原样抛出，
    由 watcher.run 的韧性循环决定退避重试（同授权内重试不重复
    消费授权）。
    """
    if decision != "INCLUDE":
        return None
    item = store.get_source_item(item_id)
    source = store.get_source(item["source_id"])
    message_id = item["last_message_id"]
    message = store.get_message(item["source_id"], message_id)
    expected = message["document_size_bytes"] if message else None

    # §12：50 MiB 是自动处理阈值；越界（或未知）必须持 DOWNLOAD_ONCE
    if expected is None or expected > MAX_AUTO_DOWNLOAD_BYTES:
        gate = store.download_gate(item_id)
        if gate in ("denied", "completed"):
            return None
    if not orico_check():
        raise OricoUnavailableError(
            "ORICO data root unavailable; refusing to download "
            "(fail-closed, §6.8)")
    previous = store._conn.execute(
        "SELECT attempts FROM downloads WHERE item_id = ?",
        (item_id,)).fetchone()
    attempts = (previous["attempts"] if previous is not None else 0) + 1
    store.upsert_download(item_id, message_id, status="downloading",
                          attempts=attempts, expected_size_bytes=expected)
    dest_dir = Path(data_root) / "telegram" / "attachments" / item_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    async def run():
        return await client.download_document(source["chat_id"],
                                              message_id, dest_dir)

    try:
        path = Path(asyncio.run(run()))
    except Exception as exc:
        store.upsert_download(item_id, message_id, status="failed",
                              attempts=attempts,
                              expected_size_bytes=expected,
                              last_error=str(exc))
        raise
    size = path.stat().st_size
    store.upsert_download(
        item_id, message_id, status="complete", attempts=attempts,
        expected_size_bytes=size, local_path=str(path),
        sha256=fingerprint_file(path).removeprefix("sha256:"))
    return path
