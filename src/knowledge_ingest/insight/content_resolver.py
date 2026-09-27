"""V3 M2: source-neutral content resolver — parts 契约（A2 修订冻结）。

职责：把 InsightSourceView 解析为可读全文的多 part 表示。
- TEXT：读 view.materialized_path（权威路径，存在才读；单 part）；
- corpus_verified：读 corpus 的 index.jsonl + batches/B*.md（§17 冻结入口；
  一个 batch 一个 part；无 combined.md fallback）；
- 任何不可读（缺文件/坏编码/空 corpus）→ None：调用方进入可恢复的
  WAITING_CONTENT，绝不 REJECT、绝不伪造内容、绝不静默截断。
本模块不感知 Telegram；路径一律来自 view 携带的显式字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import InsightSourceView


@dataclass(frozen=True)
class ContentPart:
    """单个可读片段：TEXT 为整篇单 part；corpus 为逐 batch part。"""

    part_id: str
    text: str
    source_ref: str


@dataclass(frozen=True)
class ResolvedContent:
    parts: tuple[ContentPart, ...]
    verification_status: str


@runtime_checkable
class ContentResolverPort(Protocol):
    def resolve(self, view: InsightSourceView) -> ResolvedContent | None: ...


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None                     # 可恢复等待，不崩、不伪造


class PartsContentResolver:
    """ContentResolverPort 的确定性实现（V3.0 首个正式 adapter）。"""

    def resolve(self, view: InsightSourceView) -> ResolvedContent | None:
        path = view.materialized_path
        if not path:
            return None
        root = Path(path)
        if view.verification_status == "corpus_verified":
            return self._resolve_corpus(root)
        if view.content_kind == "text" or root.is_file():
            text = _read_text_file(root)
            if text is None:
                return None
            return ResolvedContent(
                parts=(ContentPart(part_id="main", text=text,
                                   source_ref=str(root)),),
                verification_status=view.verification_status)
        return None

    @staticmethod
    def _resolve_corpus(root: Path) -> ResolvedContent | None:
        # §17 冻结入口：index.jsonl + batches/B*.md；combined.md 不是入口
        index = root / "index.jsonl"
        batches_dir = root / "batches"
        if not index.is_file() or not batches_dir.is_dir():
            return None
        batches = sorted(batches_dir.glob("B*.md"))
        if not batches:
            return None
        parts = []
        for batch in batches:
            text = _read_text_file(batch)
            if text is None:
                return None             # 任一 batch 不可读 → 整体 WAITING
            parts.append(ContentPart(part_id=batch.stem, text=text,
                                     source_ref=str(batch)))
        return ResolvedContent(parts=tuple(parts),
                               verification_status="corpus_verified")
