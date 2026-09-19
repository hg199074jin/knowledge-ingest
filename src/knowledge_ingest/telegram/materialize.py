"""TG4: 物化与 PDF 下载（冻结设计 §12/§13；方案 §6.3/§6.5/§6.7/§6.8）。

ORICO fail-closed：下载/物化前必须实测数据根可用，绝不 fallback
内置盘，绝不读取 require_orico 死配置。
"""

import asyncio
import os
from pathlib import Path

from knowledge_ingest.fingerprint import fingerprint_file

ORICO_ROOT = Path("/Volumes/ORICO")
TEXT_HEADER = "# Telegram Knowledge Item"
MAX_AUTO_DOWNLOAD_BYTES = 50 * 1024 * 1024  # §12：≤50 MiB 自动，>50 需授权
MAX_DOWNLOAD_ATTEMPTS = 5  # I2：毒丸熔断阈值（超限转人工 REVIEW）


class OricoUnavailableError(RuntimeError):
    """ORICO 数据根不可用（§6.8 fail-closed）。"""


def orico_ready() -> bool:
    return ORICO_ROOT.is_dir()


def materialize_text_item(store, item_id: str, base_dir: str | Path, *,
                          orico_check=orico_ready) -> Path:
    """§13.1：固定标题 + 原文按序拼接；provenance 不进正文；
    已删除消息的正文不进 Corpus（§20.2），删除事实留在 tg_messages。
    写入原子化（tmp + os.replace），崩溃不会留下截断的 message.md。"""
    item = store.get_source_item(item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    if not orico_check():
        raise OricoUnavailableError(
            "ORICO data root unavailable; refusing to materialize "
            "(fail-closed, §6.8)")
    texts = [row["text"] for row in store.get_item_messages(item_id)
             if row["text"] and not row["deleted_at"]]
    body = "\n\n".join(texts)
    directory = Path(base_dir) / "materialized" / item_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "message.md"
    tmp = directory / ".message.md.tmp"
    tmp.write_text(f"{TEXT_HEADER}\n\n{body}\n", encoding="utf-8")
    os.replace(tmp, path)
    store.set_item_materialized(item_id, str(path))
    return path


async def download_pdf_async(store, item_id: str, client,
                            data_root: str | Path, *, decision: str,
                            orico_check=orico_ready) -> Path | None:
    """§6.5/§6.7 PDF 下载核心（异步原生，供 watcher 事件循环内调用）。

    顺序（评审 C3/I1/I2）：完成幂等检查（所有尺寸）→ attempts 熔断 →
    >50 MiB（或大小未知）DOWNLOAD_ONCE 闸门 → ORICO fail-closed →
    认领尝试。ORICO 不可用时在任何授权消费之前抛出——fail-closed
    暂停，绝不没收人工授权。
    """
    if decision != "INCLUDE":
        return None
    item = store.get_source_item(item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    source = store.get_source(item["source_id"])
    message_id = item["last_message_id"]
    message = store.get_message(item["source_id"], message_id)
    expected = message["document_size_bytes"] if message else None

    existing = store.get_download(item_id)
    if existing is not None and existing["status"] == "complete":
        return None                       # I1：完成态全尺寸幂等
    if (existing is not None
            and existing["attempts"] >= MAX_DOWNLOAD_ATTEMPTS):
        # I2：毒丸熔断——停在可人工处置状态，不无限重试
        already = any(r["item_id"] == item_id
                      and r["reason"] == "download_attempts_exceeded"
                      for r in store.list_open_reviews())
        if not already:
            store.create_review(item_id, "download_attempts_exceeded",
                                kind=item["kind"])
        return None
    if not orico_check():
        # C3：ORICO 检查必须在 DOWNLOAD_ONCE 认领之前——fail-closed
        # 暂停，绝不因瞬时掉线没收人工授权
        raise OricoUnavailableError(
            "ORICO data root unavailable; refusing to download "
            "(fail-closed, §6.8)")
    if expected is None or expected > MAX_AUTO_DOWNLOAD_BYTES:
        gate = store.download_gate(item_id)
        if gate in ("denied", "completed"):
            return None
    attempts = (existing["attempts"] if existing is not None else 0) + 1
    store.upsert_download(item_id, message_id, status="downloading",
                          attempts=attempts, expected_size_bytes=expected)
    dest_dir = Path(data_root) / "telegram" / "attachments" / item_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = Path(await client.download_document(
            source["chat_id"], message_id, dest_dir))
    except Exception as exc:
        store.upsert_download(item_id, message_id, status="failed",
                              attempts=attempts,
                              expected_size_bytes=expected,
                              last_error=str(exc))
        raise
    store.upsert_download(
        item_id, message_id, status="complete", attempts=attempts,
        expected_size_bytes=expected, local_path=str(path),
        sha256=fingerprint_file(path).removeprefix("sha256:"))
    return path


def download_pdf(store, item_id: str, client, data_root: str | Path, *,
                 decision: str, orico_check=orico_ready) -> Path | None:
    """同步包装（CLI/测试）；语义见 download_pdf_async。"""
    return asyncio.run(download_pdf_async(
        store, item_id, client, data_root, decision=decision,
        orico_check=orico_check))
