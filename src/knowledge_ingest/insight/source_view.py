"""V3 M2: source-neutral view helpers — fingerprints and view assembly.

视图构造只依赖调用方显式传入的路径/字段；禁止从 item_id 推导任何
文件系统路径（本机审阅意见 A2：TEXT 权威路径=source_items.materialized_path）。
"""

from __future__ import annotations

import hashlib


def compute_fingerprint(data: bytes) -> str:
    return "fp." + hashlib.sha256(data).hexdigest()[:16]


def compute_text_fingerprint(text: str) -> str:
    return compute_fingerprint(text.encode("utf-8"))


def compute_corpus_fingerprint(batch_stats: list[tuple[str, int]]) -> str:
    """corpus 指纹 = 有序 batch 文件名 + 字节数的稳定哈希。

    batch 集变化（重建/扩充）即内容变化 → 触发新 revision。
    """
    canonical = "\n".join(f"{name}:{size}" for name, size in batch_stats)
    return compute_text_fingerprint(canonical)
