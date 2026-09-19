"""TG4: Source Item Builder（冻结设计 §9/§20/§22；方案 §6.2/§6.4/§6.6）。

窗口要点（§9.1）：first-message anchored 固定 300 秒，不是滚动窗口；
同 source + 同 sender 才合并；PDF / 网盘链接消息打断合并。
编辑四态（§6.4）以 handoff_completed 为不可逆边界；Item 内容延迟
派生——materialize 时按 message_ids 回读 tg_messages 重建，因此
open 窗口内的 EDIT 天然被吸收。
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from .client_port import TelegramEventKind
from .event_store import TelegramEventStore

WINDOW_SECONDS = 300
_QUARK_HINT = "quark.cn"
_BAIDU_HINT = "pan.baidu.com"
_COLLECTION_RE = re.compile(
    r"\d+\s*(?:份|个文件|个文档)|(?<![\w.])\d+(?:\.\d+)?\s*[GT]B?\b",
    re.IGNORECASE)


def classify_kind(event) -> str:
    if event.document is not None:
        return "pdf"
    if event.cloud_links:
        return "cloud_link"
    return "text"


def provider_guess(links) -> str:
    """§22：与 baidu/quark provider 枚举对齐；识别不出 = unknown。"""
    joined = " ".join(links)
    if _QUARK_HINT in joined:
        return "quark"
    if _BAIDU_HINT in joined:
        return "baidu"
    return "unknown"


def is_resource_collection_candidate(text: str | None) -> bool:
    """§22 大合集描述启发式（"2755 份 / 176G" 类）；只做标记，
    V2 绝不自动下载/遍历。"""
    return bool(text) and bool(_COLLECTION_RE.search(text))


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


def _sender_str(sender_id) -> str | None:
    return str(sender_id) if sender_id is not None else None


class SourceItemPipeline:
    """Raw 事实之上的 Item 层；由 watcher 在落库后回调（§19.2）。"""

    def __init__(self, store: TelegramEventStore, *,
                 data_root: str | Path,
                 window_seconds: int = WINDOW_SECONDS,
                 orico_check=None,
                 noise_llm=None, interest_llm=None, budget=None):
        self.store = store
        self.data_root = Path(data_root)
        self.window_seconds = window_seconds
        self._orico_check = orico_check
        # TG5：可注入分类通道与预算（不写死任何 provider/SDK，§7.6）
        self.noise_llm = noise_llm
        self.interest_llm = interest_llm
        self.budget = budget

    # ---- watcher 回调入口 ----

    def on_raw(self, event, status: str, source_id: str | None):
        if source_id is None:
            return None
        if event.kind is TelegramEventKind.NEW and status == "stored":
            return self.ingest_new(event, source_id)
        if event.kind is TelegramEventKind.EDIT and status == "updated":
            return self.ingest_edit(event, source_id)
        if event.kind is TelegramEventKind.DELETE and status == "deleted":
            return self.ingest_delete(event, source_id)
        return None

    # ---- NEW：分类 + 窗口合并 ----

    def ingest_new(self, event, source_id: str):
        kind = classify_kind(event)
        if kind == "text":
            return self._merge_or_open_text(event, source_id)
        item_id = f"{source_id}:{event.message_id}"
        status = "PENDING_RESOURCE" if kind == "cloud_link" else "open"
        self.store.create_source_item(item_id, source_id, kind,
                                      [event.message_id],
                                      processing_status=status)
        self.store.finalize_item(item_id, status=status)
        if kind == "pdf":
            self._classify_pdf(item_id)      # §11.4：下载前判定
        return item_id

    def _merge_or_open_text(self, event, source_id: str) -> str:
        # C2：重放/乱序事件不得重复入项（live 重投、崩溃后 reconcile
        # 重放是正常工况）；已是某 Item 成员则直接归属，绝不二次并入。
        owner = self.store.find_item_by_message(source_id,
                                                event.message_id)
        if owner is not None:
            return owner["item_id"]
        self._close_expired_windows(event, source_id)
        existing = self.store.find_open_text_item(source_id)
        if existing is not None:
            last = self.store.get_message(source_id,
                                          existing["last_message_id"])
            same_sender = (
                last is not None
                and last["sender_id"] == _sender_str(event.sender_id))
            if same_sender and self._within_window(
                    source_id, existing, event.message_date):
                self.store.append_item_message(existing["item_id"],
                                               event.message_id)
                return existing["item_id"]
            self._finalize_text(existing["item_id"])
        item_id = f"{source_id}:{event.message_id}"
        self.store.create_source_item(item_id, source_id, "text",
                                      [event.message_id])
        return item_id

    def _close_expired_windows(self, event, source_id: str) -> None:
        if event.message_date is None:
            return
        open_item = self.store.find_open_text_item(source_id)
        if open_item is None:
            return
        if not self._within_window(source_id, open_item,
                                   event.message_date):
            self._finalize_text(open_item["item_id"])

    def _within_window(self, source_id: str, item, event_date) -> bool:
        if event_date is None:
            return True
        first = self.store.get_message(source_id, item["first_message_id"])
        if first is None:
            return False
        anchor = _aware(first["message_date"])
        # C2：负 delta（早于锚点的乱序消息）绝不并入更新的窗口
        return 0 <= (event_date - anchor).total_seconds() <= \
            self.window_seconds

    # ---- EDIT：§6.4 四态 ----

    def ingest_edit(self, event, source_id: str):
        item = self.store.find_item_by_message(source_id, event.message_id)
        if item is None:
            return self.ingest_new(event, source_id)  # 未见过的编辑=事实补录
        item_id = item["item_id"]
        if item["handoff_completed"]:
            self.store.create_review(item_id,
                                     "SOURCE_EDITED_AFTER_HANDOFF",
                                     kind=item["kind"])
            return "handoff_review"          # §6.4 C：Corpus 不动
        if item["processing_status"] == "open":
            return "absorbed"                # §6.4 A：open 窗口派生吸收
        # §6.4 B：同 item_id 重建——清待重算的 policy；
        # 只有 text 重写物化（pdf/cloud_link 的物化由各自链路负责）；
        # 兼覆盖 C1 的 stranded（finalized 未物化）场景。
        self.store.reset_item_policy(item_id)
        if item["kind"] == "text":
            self._finalize_text(item_id)   # 重建即重分类（policy 已清）
        elif item["kind"] == "pdf":
            self._classify_pdf(item_id)    # I3：caption 变了 → 重判兴趣
        return "rebuilt"

    # ---- DELETE：§6.4 D ----

    def ingest_delete(self, event, source_id: str):
        item = self.store.find_item_by_message(source_id, event.message_id)
        if item is None:
            return None
        item_id = item["item_id"]
        if item["handoff_completed"]:
            return "deleted_provenance"      # 仅追加事实，不跨层回滚
        if item["kind"] == "text":
            survivors = [row for row in self.store.get_item_messages(item_id)
                         if not row["deleted_at"] and row["text"]]
            if survivors:
                if item["processing_status"] != "open":
                    self._materialize(item_id)  # 重建剔除已删正文
                return "survived"            # 幸存正文不因单条被删而丢失
        self.store.set_item_processing_status(item_id, "deleted_source")
        return "blocked"                     # 空窗口/单消息类：阻断 handoff

    # ---- 内部 ----

    def recover_stranded(self) -> int:
        """C1：finalized 但物化失败（ORICO 掉线/崩溃窗口）的 item，
        在 ORICO 恢复后补物化；仍失败的留待下轮 reconcile。"""
        recovered = 0
        for item in self.store.find_stranded_items():
            try:
                self._finalize_text(item["item_id"])
                recovered += 1
            except Exception as exc:        # noqa: BLE001 —— 恢复不中断
                print(f"recover stranded {item['item_id']}: {exc}",
                      file=sys.stderr, flush=True)
                continue
        return recovered

    def pending_record(self, item_id: str) -> dict | None:
        """§13.3 Pending Record：CLOUD_LINK 的 Stub 视图（从既有事实
        确定性派生，不引入第二事实源；未来 Cloud Resource Resolver
        从此续接）。"""
        import json as _json

        item = self.store.get_source_item(item_id)
        if item is None or item["kind"] != "cloud_link":
            return None
        message = self.store.get_message(item["source_id"],
                                         item["last_message_id"])
        links = (_json.loads(message["cloud_links_json"])
                 if message and message["cloud_links_json"] else [])
        caption = message["text"] if message else None
        url = None
        if links:
            matched = re.search(r"https?://\S+", links[0])
            url = matched.group(0) if matched else links[0]
        return {
            "item_id": item_id,
            "source_id": item["source_id"],
            "message_id": item["last_message_id"],
            "caption": caption,
            "url": url,
            "provider_guess": provider_guess(links),
            "resource_collection_candidate":
                is_resource_collection_candidate(caption),
            "status": "PENDING_RESOURCE",
        }

    def _finalize_text(self, item_id: str) -> None:
        """§24.1 顺序：finalize → Noise → KEEP/REVIEW 才 materialize；
        SKIP 只留审计（30 天保留，§18.2），不产生下游产物。"""
        from .classify import CLASSIFIER_VERSION, POLICY_VERSION, classify_noise

        self.store.finalize_item(item_id)
        item = self.store.get_source_item(item_id)
        texts = [row["text"] for row in self.store.get_item_messages(
            item_id) if row["text"] and not row["deleted_at"]]
        decision = classify_noise("\n\n".join(texts),
                                  llm=self.noise_llm, budget=self.budget,
                                  source_id=item["source_id"])
        self.store.set_item_noise(item_id, decision.decision)
        self.store.create_classifier_audit(
            item_id, "noise", decision.decision,
            reason_code=decision.reason_code,
            confidence=decision.confidence,
            policy_version=POLICY_VERSION,
            classifier_version=CLASSIFIER_VERSION)
        if decision.decision == "SKIP":
            self.store.set_item_processing_status(item_id, "skipped_noise")
            return
        body = "\n\n".join(texts)
        if not body.strip():
            # I4：空正文（全删/无文本）→ 终态，不物化、不进人工队列
            self.store.set_item_processing_status(item_id, "empty_item")
            return
        if decision.decision == "REVIEW":
            # I4：§24.1 只在 KEEP 物化；REVIEW 停车等待人工，
            # resolve→KEEP 后由 TG6 handoff runner 补物化
            self.store.set_item_processing_status(item_id, "noise_review")
            if not self.store.has_open_review(item_id, "noise_uncertain"):
                self.store.create_review(item_id, "noise_uncertain",
                                         kind="text")
            return
        self._materialize(item_id)

    def _classify_pdf(self, item_id: str) -> None:
        """§11.4/§12：Interest（下载前）→ INCLUDE 且 ≤50MiB 才待下载；
        越界或 REVIEW 转人工。"""
        from .classify import CLASSIFIER_VERSION, POLICY_VERSION, classify_interest
        from .materialize import MAX_AUTO_DOWNLOAD_BYTES

        item = self.store.get_source_item(item_id)
        source = self.store.get_source(item["source_id"])
        message = self.store.get_message(item["source_id"],
                                         item["last_message_id"])
        meta = {
            "caption": message["text"] if message else None,
            "filename": message["document_name"] if message else None,
            "size_bytes": (message["document_size_bytes"]
                           if message else None),
            "source_display_name": source["display_name"] if source
            else None,
        }
        decision = classify_interest(meta, llm=self.interest_llm,
                                     budget=self.budget,
                                     source_id=item["source_id"])
        self.store.set_item_interest(item_id, decision.decision)
        self.store.create_classifier_audit(
            item_id, "interest", decision.decision,
            reason_code=decision.reason,
            confidence=decision.confidence,
            policy_version=POLICY_VERSION,
            classifier_version=CLASSIFIER_VERSION)
        size = meta["size_bytes"]
        if decision.decision == "EXCLUDE":
            self.store.set_item_processing_status(item_id,
                                                  "skipped_interest")
        elif decision.decision == "REVIEW":
            self.store.set_item_processing_status(item_id,
                                                  "interest_review")
            if not self.store.has_open_review(item_id,
                                              "interest_uncertain"):
                self.store.create_review(item_id, "interest_uncertain",
                                         kind="pdf", size_bytes=size)
        else:                                   # INCLUDE
            self.store.set_item_processing_status(item_id,
                                                  "interest_include")
            reason = ("pdf size unknown" if size is None
                      else "size over 50 MiB")
            if (size is None or size > MAX_AUTO_DOWNLOAD_BYTES) \
                    and not self.store.has_open_review(item_id, reason):
                self.store.create_review(item_id, reason,
                                         kind="pdf", size_bytes=size)

    def _materialize(self, item_id: str):
        from .materialize import materialize_text_item, orico_ready

        check = self._orico_check
        if check is None:
            check = orico_ready
        materialize_text_item(self.store, item_id, self.data_root,
                              orico_check=check)
