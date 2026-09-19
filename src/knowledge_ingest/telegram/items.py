"""TG4: Source Item Builder（冻结设计 §9/§20/§22；方案 §6.2/§6.4/§6.6）。

窗口要点（§9.1）：first-message anchored 固定 300 秒，不是滚动窗口；
同 source + 同 sender 才合并；PDF / 网盘链接消息打断合并。
编辑四态（§6.4）以 handoff_completed 为不可逆边界；Item 内容延迟
派生——materialize 时按 message_ids 回读 tg_messages 重建，因此
open 窗口内的 EDIT 天然被吸收。
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from knowledge_ingest.manifest_store import ManifestStore, atomic_write_text

from .client_port import TelegramEventKind
from .event_store import TelegramEventStore

WINDOW_SECONDS = 300
_QUARK_HINT = "quark.cn"
_BAIDU_HINT = "pan.baidu.com"
_COLLECTION_RE = re.compile(
    r"\d+\s*(?:份|个文件|个文档)|(?<![\w.])\d+(?:\.\d+)?\s*[GT]B?\b",
    re.IGNORECASE)

# 评审 R1：已作出判定/停车待人审的状态——删除只落 tg_messages 事实，
# 绝不改写这些状态（原缺陷：部分删除会经 _materialize 复活成可交付）。
TERMINAL_ITEM_STATUSES = (
    "skipped_noise", "skipped_interest", "skipped_too_short", "empty_item",
    "noise_review", "interest_review", "deleted_source")
# 评审 R1：正文派生仍在进行中/已完成 → 删除后需要按幸存正文重建
_TEXT_REBUILDABLE_STATUSES = ("finalized", "materialized")
# 评审 R2：人工决定的审计来源标记（与模型分类器区分）
HUMAN_REVIEW_CLASSIFIER = "human-review"
# 尺寸参数缺省哨兵：None 是合法值（= 大小未知），不能当"未传"用
_UNSET = object()


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
        status = item["processing_status"]
        if item["handoff_completed"]:
            self._mark_downstream_deleted(item_id)
            return "deleted_provenance"      # 仅追加事实，不跨层回滚
        if status in TERMINAL_ITEM_STATUSES:
            # 评审 R1：SKIP / 待人审 / 已移除的决定不因单条删除被复活
            #（原缺陷：幸存正文会把 skipped_noise/noise_review 翻成
            # materialized，从而通过 handoff 门并生成真实 k2c Job）
            return "decision_kept"
        if item["kind"] == "text":
            survivors = self._live_texts(item_id)
            if survivors:
                if status in _TEXT_REBUILDABLE_STATUSES:
                    self._materialize(item_id)  # 重建剔除已删正文
                return "survived"            # 幸存正文不因单条被删而丢失
        self.store.set_item_processing_status(item_id, "deleted_source")
        return "blocked"                     # 空窗口/单消息类：阻断 handoff

    # ---- R2：人工 REVIEW 决定的消费（§16.3） ----

    def consume_review_decisions(self) -> list[dict]:
        """把已解决的人工决定落到 Item 上（幂等；失败不消费，下轮重试）。

        §16.3 的 KEEP / SKIP 必须真的改变 Item 的命运；DOWNLOAD_ONCE
        例外——它由 download_gate 在真正下载时消费（唯一消费方）。
        """
        applied = []
        for review in self.store.list_reviews_pending_apply():
            try:
                outcome = self._apply_review_decision(review)
            except Exception as exc:        # noqa: BLE001 —— 单项隔离
                print(f"review {review['review_id']} apply failed: {exc}",
                      file=sys.stderr, flush=True)
                continue
            if outcome == "deferred":
                continue
            self.store.mark_review_consumed(review["review_id"])
            applied.append({"review_id": review["review_id"],
                            "item_id": review["item_id"],
                            "reason": review["reason"],
                            "decision": review["decision"],
                            "outcome": outcome})
        return applied

    def _apply_review_decision(self, review) -> str:
        item = self.store.get_source_item(review["item_id"])
        if item is None:
            return "item_missing"
        item_id = item["item_id"]
        reason, decision = review["reason"], review["decision"]
        if item["handoff_completed"]:
            return "post_handoff_informational"   # §20.1：不跨层回滚
        if decision == "DOWNLOAD_ONCE":
            if reason != "download_attempts_exceeded":
                return "deferred"                 # 交给 download_gate 认领
            self.store.reset_download_attempts(item_id)
            return "download_retry_armed"
        if reason == "noise_uncertain":
            if decision == "KEEP":
                self.store.set_item_noise(item_id, "KEEP")
                if not "\n\n".join(self._live_texts(item_id)).strip():
                    self.store.set_item_processing_status(item_id,
                                                          "empty_item")
                    self._audit_human(item_id, "noise", "KEEP", "human_keep")
                    return "kept_empty"
                self._materialize(item_id)
                self._audit_human(item_id, "noise", "KEEP", "human_keep")
                return "materialized"
            if decision == "SKIP":
                self.store.set_item_noise(item_id, "SKIP")
                self.store.set_item_processing_status(item_id,
                                                      "skipped_noise")
                self._audit_human(item_id, "noise", "SKIP", "human_skip")
                return "skipped_noise"
            return "unsupported_decision"
        if reason in ("interest_uncertain", "pdf size unknown",
                      "size over 50 MiB", "download_attempts_exceeded"):
            if decision == "KEEP":
                self.store.set_item_interest(item_id, "INCLUDE")
                self.store.set_item_processing_status(item_id,
                                                      "interest_include")
                self._audit_human(item_id, "interest", "INCLUDE",
                                  "human_keep")
                # 人工 INCLUDE 同样要过 §12 尺寸规则（>50 MiB/未知 →
                # 补一张 DOWNLOAD_ONCE 授权单，否则 Item 会卡在无人知晓处）
                self._flag_size_review_if_needed(item)
                return "interest_include"
            if decision == "SKIP":
                self.store.set_item_interest(item_id, "EXCLUDE")
                self.store.set_item_processing_status(item_id,
                                                      "skipped_interest")
                self._audit_human(item_id, "interest", "EXCLUDE",
                                  "human_skip")
                return "skipped_interest"
            return "unsupported_decision"
        return "informational"      # SOURCE_EDITED_AFTER_HANDOFF 等：仅记录

    def _audit_human(self, item_id: str, kind: str, decision: str,
                     reason_code: str) -> None:
        """§21.1：人工决定同样进审计（来源标记为 human-review）。"""
        from .classify import CLASSIFIER_VERSION, POLICY_VERSION

        self.store.create_classifier_audit(
            item_id, kind, decision, reason_code=reason_code,
            confidence=1.0, policy_version=POLICY_VERSION,
            classifier_version=f"{HUMAN_REVIEW_CLASSIFIER}:"
                               f"{CLASSIFIER_VERSION}")

    # ---- R3：PDF 下载接入运行时（§6.5/§6.7/§6.8） ----

    async def download_pending_pdfs(self, client, *,
                                    item_id: str | None = None) -> int:
        """把 interest_include 的 PDF Item 推进到已下载。

        ≤50 MiB 自动下载；>50 MiB 或大小未知须人工 DOWNLOAD_ONCE；
        ORICO 不可用时不消费授权、Item 保持可恢复（§6.8）。
        单项失败不影响其他 Item（评审 R3：此前下载器没有任何调用方，
        整条 PDF 腿到不了 handoff）。
        """
        from .materialize import download_pdf_async

        if client is None:
            return 0
        downloaded = 0
        for item in self.store.list_source_items(kind="pdf"):
            if item_id is not None and item["item_id"] != item_id:
                continue
            if item["processing_status"] != "interest_include":
                continue
            try:
                path = await download_pdf_async(
                    self.store, item["item_id"], client, self.data_root,
                    decision=item["interest_decision"] or "",
                    orico_check=self._orico())
            except Exception as exc:        # noqa: BLE001 —— 单项隔离
                print(f"pdf download {item['item_id']}: {exc}",
                      file=sys.stderr, flush=True)
                continue
            if path is not None:
                downloaded += 1
        return downloaded

    def _mark_downstream_deleted(self, item_id: str) -> None:
        """评审 R6/§20.2：已 handoff 的 Item 收到删除后，下游 provenance
        必须能知道（只改 provenance 事实，不改 Corpus / Job 内容，§20.1）。
        """
        item = self.store.get_source_item(item_id)
        job_id = item["knowledge_ingest_job_id"] if item else None
        if not job_id:
            return
        jobs_root = Path(self.data_root) / "jobs"
        try:
            handoff_file = jobs_root / job_id / "handoff" / "source.json"
            if handoff_file.is_file():
                handoff = json.loads(handoff_file.read_text(encoding="utf-8"))
                provenance = handoff.get("provenance")
                if isinstance(provenance, dict):
                    provenance["source_deleted"] = True
                    atomic_write_text(handoff_file, json.dumps(
                        handoff, ensure_ascii=False, indent=2))
            manifest_store = ManifestStore(jobs_root=jobs_root)
            if not manifest_store.manifest_path(job_id).is_file():
                return
            registered = manifest_store.load(job_id)
            source = registered.source if isinstance(registered.source,
                                                     dict) else None
            provenance = source.get("provenance") if source else None
            if isinstance(provenance, dict) \
                    and provenance.get("source_deleted") is not True:
                with manifest_store.edit(job_id) as manifest:
                    manifest.source["provenance"]["source_deleted"] = True
        except Exception as exc:            # noqa: BLE001 —— 审计补记不致命
            print(f"downstream provenance update failed ({job_id}): {exc}",
                  file=sys.stderr, flush=True)

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
        texts = self._live_texts(item_id)
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
            self._flag_size_review_if_needed(item, size=size)

    def _flag_size_review_if_needed(self, item, *, size=_UNSET):
        """§12：>50 MiB（或大小未知）的 INCLUDE PDF 必须人工授权才下载。"""
        from .materialize import MAX_AUTO_DOWNLOAD_BYTES

        item_id = item["item_id"]
        if size is _UNSET:
            message = self.store.get_message(item["source_id"],
                                             item["last_message_id"])
            size = message["document_size_bytes"] if message else None
        if size is not None and size <= MAX_AUTO_DOWNLOAD_BYTES:
            return
        reason = "pdf size unknown" if size is None else "size over 50 MiB"
        if not self.store.has_open_review(item_id, reason):
            self.store.create_review(item_id, reason, kind="pdf",
                                     size_bytes=size)

    def _live_texts(self, item_id: str) -> list[str]:
        """Item 的幸存正文（已删消息不进 Corpus，§20.2）。"""
        return [row["text"] for row in self.store.get_item_messages(item_id)
                if row["text"] and not row["deleted_at"]]

    def _orico(self):
        from .materialize import orico_ready

        return self._orico_check or orico_ready

    def _materialize(self, item_id: str):
        from .materialize import materialize_text_item

        materialize_text_item(self.store, item_id, self.data_root,
                              orico_check=self._orico())
