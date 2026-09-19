"""TG2: Telegram Source 事实层（SQLite，user_version=1）。

冻结原则 §8.1：SQLite 是 Telegram Source Runtime 的事实层；
Markdown/PDF handoff 是派生产物。并发契约 §8.4：WAL +
foreign_keys + busy_timeout + 短事务；多步写入必须持
maintenance lock（见 locks.py）。§8.3：本库只记录 job_id
与 handoff 是否完成，Job 真实状态以 job.yaml 为准。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

REVIEW_DECISIONS = ("KEEP", "SKIP", "DOWNLOAD_ONCE")

ITEM_KINDS = ("text", "pdf", "cloud_link")


class UnsupportedSchemaVersionError(RuntimeError):
    """数据库 user_version 高于本代码支持的版本（§4.5 fail-fast）。"""


class MessageBeforeStartError(ValueError):
    """message_date 早于 source.start_at（冻结 §6.3 硬边界）。"""


class ReviewConflictError(ValueError):
    """已解决 review 被提交了不同 decision（禁止静默覆盖）。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be tz-aware ISO-8601: {value!r}")
    return parsed


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_sources (
    source_id            TEXT PRIMARY KEY,
    chat_id              INTEGER UNIQUE,
    display_name         TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 0,
    start_at             TEXT,
    last_seen_message_id INTEGER NOT NULL DEFAULT 0,
    last_reconciled_at   TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_messages (
    source_id           TEXT NOT NULL REFERENCES tg_sources(source_id),
    message_id          INTEGER NOT NULL,
    sender_id           TEXT,
    message_date        TEXT NOT NULL,
    edited_at           TEXT,
    deleted_at          TEXT,
    text                TEXT,
    reply_to_message_id INTEGER,
    has_document        INTEGER NOT NULL DEFAULT 0,
    document_name       TEXT,
    document_mime       TEXT,
    document_size_bytes INTEGER,
    cloud_links_json    TEXT,
    raw_fingerprint     TEXT,
    status              TEXT NOT NULL DEFAULT 'raw',
    PRIMARY KEY (source_id, message_id)
);

CREATE TABLE IF NOT EXISTS source_items (
    item_id                 TEXT PRIMARY KEY,
    source_id               TEXT NOT NULL REFERENCES tg_sources(source_id),
    kind                    TEXT NOT NULL CHECK (kind IN ('text', 'pdf', 'cloud_link')),
    first_message_id        INTEGER,
    last_message_id         INTEGER,
    message_ids_json        TEXT NOT NULL DEFAULT '[]',
    created_at              TEXT NOT NULL,
    finalized_at            TEXT,
    noise_decision          TEXT,
    interest_decision       TEXT,
    processing_status       TEXT NOT NULL DEFAULT 'open',
    materialized_path       TEXT,
    knowledge_ingest_job_id TEXT,
    handoff_completed       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS downloads (
    item_id              TEXT NOT NULL REFERENCES source_items(item_id),
    document_message_id  INTEGER NOT NULL,
    telegram_document_id TEXT,
    expected_size_bytes  INTEGER,
    local_path           TEXT,
    sha256               TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    PRIMARY KEY (item_id, document_message_id)
);

CREATE TABLE IF NOT EXISTS source_ai_budget (
    source_id              TEXT NOT NULL,
    classifier_kind        TEXT NOT NULL,
    calls                  INTEGER NOT NULL DEFAULT 0,
    attempts               INTEGER NOT NULL DEFAULT 0,
    consecutive_empty      INTEGER NOT NULL DEFAULT 0,
    consecutive_rate_limit INTEGER NOT NULL DEFAULT 0,
    permits_json           TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, classifier_kind)
);

CREATE TABLE IF NOT EXISTS classifier_audit (
    audit_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            TEXT NOT NULL,
    kind               TEXT NOT NULL,
    decision           TEXT NOT NULL,
    reason_code        TEXT,
    confidence         REAL,
    policy_version     TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id              TEXT NOT NULL REFERENCES source_items(item_id),
    reason               TEXT NOT NULL,
    kind                 TEXT,
    size_bytes           INTEGER,
    created_at           TEXT NOT NULL,
    resolved_at          TEXT,
    decision             TEXT CHECK (decision IN ('KEEP', 'SKIP', 'DOWNLOAD_ONCE')),
    decision_consumed_at TEXT
);
"""


class TelegramEventStore:
    """state.db 的唯一写入口；业务代码不得绕过本类拿裸 connection。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._ensure_schema()

    # ---------- schema / pragmas ----------

    def _ensure_schema(self) -> None:
        version = self.user_version()
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"state.db user_version={version} > supported "
                f"{SCHEMA_VERSION}; refusing to open (fail-fast)")
        # 全部 DDL 幂等（IF NOT EXISTS）：v1 期内追加的表对既有库
        # 自动补建，不引入迁移框架（§4.5；TG7 才做 1→2）
        self._conn.executescript(_SCHEMA)
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def busy_timeout_ms(self) -> int:
        return int(self._conn.execute("PRAGMA busy_timeout").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    # ---------- sources（§6 Source Registry） ----------

    def add_source(self, source_id: str, *, chat_id: int | None = None,
                   display_name: str = "", start_at: str | None = None,
                   last_seen_message_id: int = 0,
                   enabled: bool = True) -> None:
        """幂等：完全重复的 add 是 no-op；真实冲突显式报错。

        - chat_id 已被其他 source 占用 → ValueError（不静默吞掉）；
        - 同 source_id 试图改绑不同 chat_id → ValueError；
        - 既有行 chat_id 为空时允许补填。
        start_at 一经写入不倒退（§6.3）。
        """
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO tg_sources
                   (source_id, chat_id, display_name, enabled, start_at,
                    last_seen_message_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_id, chat_id, display_name, int(enabled), start_at,
                 last_seen_message_id, now, now))
        existing = self.get_source(source_id)
        if existing is None:
            owner = self._conn.execute(
                "SELECT source_id FROM tg_sources WHERE chat_id = ?",
                (chat_id,)).fetchone()
            owner_id = owner["source_id"] if owner else "unknown"
            raise ValueError(
                f"chat_id {chat_id} already registered to source "
                f"{owner_id}; refusing to register {source_id}")
        existing_chat = existing["chat_id"]
        if (chat_id is not None and existing_chat is not None
                and existing_chat != chat_id):
            raise ValueError(
                f"source {source_id} already bound to chat_id "
                f"{existing_chat}; refusing to rebind to {chat_id}")
        if chat_id is not None and existing_chat is None:
            with self._conn:
                self._conn.execute(
                    "UPDATE tg_sources SET chat_id = ?, updated_at = ? "
                    "WHERE source_id = ?",
                    (chat_id, _now_iso(), source_id))

    def get_source(self, source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tg_sources WHERE source_id = ?",
            (source_id,)).fetchone()

    def get_source_by_chat_id(self, chat_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tg_sources WHERE chat_id = ?",
            (chat_id,)).fetchone()

    def list_sources(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tg_sources ORDER BY source_id").fetchall()

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tg_sources SET enabled = ?, updated_at = ? "
                "WHERE source_id = ?",
                (int(enabled), _now_iso(), source_id))
        if cursor.rowcount == 0:
            raise ValueError(f"source not found: {source_id}")

    def advance_seen(self, source_id: str, message_id: int) -> None:
        """live 路径推进 last_seen 游标（只前进，不倒退）。"""
        with self._conn:
            self._conn.execute(
                """UPDATE tg_sources SET
                     last_seen_message_id = MAX(last_seen_message_id, ?),
                     updated_at = ?
                   WHERE source_id = ?""",
                (message_id, _now_iso(), source_id))

    def touch_source_cursor(self, source_id: str, *,
                            last_seen_message_id: int | None = None,
                            last_reconciled_at: str | None = None) -> None:
        """§4.7：reconcile 后推进 last_seen / last_reconciled 游标。"""
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE tg_sources SET
                     last_seen_message_id = COALESCE(?, last_seen_message_id),
                     last_reconciled_at = COALESCE(?, last_reconciled_at),
                     updated_at = ?
                   WHERE source_id = ?""",
                (last_seen_message_id, last_reconciled_at, _now_iso(),
                 source_id))
        if cursor.rowcount == 0:
            raise ValueError(f"source not found: {source_id}")

    # ---------- messages（§8 Raw Event） ----------

    def upsert_message(self, source_id: str, message_id: int, *,
                       message_date: str, sender_id: str | None = None,
                       text: str | None = None,
                       reply_to_message_id: int | None = None,
                       has_document: bool = False,
                       document_name: str | None = None,
                       document_mime: str | None = None,
                       document_size_bytes: int | None = None,
                       cloud_links: list | None = None,
                       raw_fingerprint: str | None = None,
                       edited_at: str | None = None) -> None:
        source = self.get_source(source_id)
        if source is None:
            raise ValueError(f"unknown source: {source_id}")
        start_at = source["start_at"]
        if start_at and _parse_ts(message_date) < _parse_ts(start_at):
            raise MessageBeforeStartError(
                f"message {source_id}#{message_id} date {message_date} "
                f"is before start_at {start_at}; historical backfill "
                f"forbidden (冻结 §6.3)")
        links_json = (json.dumps(cloud_links, ensure_ascii=False)
                      if cloud_links is not None else None)
        with self._conn:
            self._conn.execute(
                """INSERT INTO tg_messages
                   (source_id, message_id, sender_id, message_date,
                    edited_at, text, reply_to_message_id, has_document,
                    document_name, document_mime, document_size_bytes,
                    cloud_links_json, raw_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id, message_id) DO UPDATE SET
                     sender_id = excluded.sender_id,
                     message_date = excluded.message_date,
                     edited_at = excluded.edited_at,
                     text = excluded.text,
                     reply_to_message_id = excluded.reply_to_message_id,
                     has_document = excluded.has_document,
                     document_name = excluded.document_name,
                     document_mime = excluded.document_mime,
                     document_size_bytes = excluded.document_size_bytes,
                     cloud_links_json = excluded.cloud_links_json,
                     raw_fingerprint = excluded.raw_fingerprint""",
                (source_id, message_id, sender_id, message_date,
                 edited_at, text, reply_to_message_id, int(has_document),
                 document_name, document_mime, document_size_bytes,
                 links_json, raw_fingerprint))

    def get_message(self, source_id: str,
                    message_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM tg_messages WHERE source_id = ? "
            "AND message_id = ?", (source_id, message_id)).fetchone()

    def count_messages(self, source_id: str | None = None) -> int:
        if source_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_messages").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tg_messages WHERE source_id = ?",
                (source_id,)).fetchone()
        return int(row[0])

    def mark_message_deleted(self, source_id: str, message_id: int, *,
                             deleted_at: str | None = None) -> None:
        """冻结 §20.2：只记删除事实，不物理删除行；重复删除幂等，
        保留最早 deleted_at（首次删除时间是审计事实）。"""
        stamp = deleted_at or _now_iso()
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE tg_messages SET deleted_at = ? "
                "WHERE source_id = ? AND message_id = ? "
                "AND deleted_at IS NULL",
                (stamp, source_id, message_id))
        if cursor.rowcount == 0 and \
                self.get_message(source_id, message_id) is None:
            raise ValueError(
                f"message not found: {source_id}#{message_id}")
        # 已删除（或刚删除成功）：保留最早时间戳，幂等返回

    # ---------- source items / downloads ----------

    def create_source_item(self, item_id: str, source_id: str, kind: str,
                           message_ids: list[int], *,
                           processing_status: str = "open") -> None:
        """确定性 item_id 重放时幂等（§19.3；评审 TG4 备注）。"""
        if kind not in ITEM_KINDS:
            raise ValueError(f"invalid item kind: {kind}")
        if not message_ids:
            raise ValueError("source item requires non-empty message_ids")
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO source_items
                   (item_id, source_id, kind, first_message_id,
                    last_message_id, message_ids_json, created_at,
                    processing_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, source_id, kind, min(message_ids),
                 max(message_ids), json.dumps(message_ids), now,
                 processing_status))

    def get_source_item(self, item_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_items WHERE item_id = ?",
            (item_id,)).fetchone()

    def set_item_processing_status(self, item_id: str, status: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET processing_status = ? "
                "WHERE item_id = ?", (status, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def set_item_handoff(self, item_id: str, job_id: str) -> None:
        """§8.3：SQLite 只记 job_id 与 handoff 是否完成。"""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET knowledge_ingest_job_id = ?, "
                "handoff_completed = 1 WHERE item_id = ?",
                (job_id, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    # ---------- TG4：窗口 / 物化 / 下载闸门 ----------

    def find_open_text_item(self, source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT * FROM source_items
               WHERE source_id = ? AND kind = 'text'
                 AND processing_status = 'open'
               ORDER BY created_at DESC LIMIT 1""",
            (source_id,)).fetchone()

    def append_item_message(self, item_id: str, message_id: int) -> None:
        row = self.get_source_item(item_id)
        if row is None:
            raise ValueError(f"item not found: {item_id}")
        ids = json.loads(row["message_ids_json"])
        if message_id in ids:
            return  # 幂等
        ids.append(message_id)
        with self._conn:
            self._conn.execute(
                "UPDATE source_items SET message_ids_json = ?, "
                "last_message_id = ? WHERE item_id = ?",
                (json.dumps(ids), max(ids), item_id))

    def finalize_item(self, item_id: str, *, status: str = "finalized") -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET finalized_at = COALESCE("
                "finalized_at, ?), processing_status = ? "
                "WHERE item_id = ?", (_now_iso(), status, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def set_item_materialized(self, item_id: str, path: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET materialized_path = ?, "
                "processing_status = 'materialized' WHERE item_id = ?",
                (path, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def get_item_messages(self, item_id: str) -> list[sqlite3.Row]:
        item = self.get_source_item(item_id)
        if item is None:
            return []
        rows = [self.get_message(item["source_id"], mid)
                for mid in json.loads(item["message_ids_json"])]
        return [row for row in rows if row is not None]

    def find_item_by_message(self, source_id: str,
                             message_id: int) -> sqlite3.Row | None:
        for row in self._conn.execute(
                "SELECT * FROM source_items WHERE source_id = ? "
                "ORDER BY created_at DESC", (source_id,)).fetchall():
            if message_id in json.loads(row["message_ids_json"]):
                return row
        return None

    def download_gate(self, item_id: str) -> str:
        """§6.7 DOWNLOAD_ONCE 闸门（短事务守卫，幂等）。

        返回：'authorized'（本次 claim 成功）/ 'retry'（同授权内的
        重试，已有失败/挂起下载行）/ 'completed'（已完成，禁止再下）
        / 'denied'（无授权）。
        """
        review = self._conn.execute(
            """SELECT * FROM reviews WHERE item_id = ?
               AND decision = 'DOWNLOAD_ONCE'
               ORDER BY review_id DESC LIMIT 1""",
            (item_id,)).fetchone()
        if review is None:
            return "denied"
        row = self._conn.execute(
            "SELECT status, attempts FROM downloads WHERE item_id = ?",
            (item_id,)).fetchone()
        if row is not None and row["status"] == "complete":
            return "completed"
        if review["decision_consumed_at"] is None:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE reviews SET decision_consumed_at = ? "
                    "WHERE review_id = ? AND decision_consumed_at IS NULL",
                    (_now_iso(), review["review_id"]))
            if cursor.rowcount == 1:
                return "authorized"
        if row is not None and row["attempts"] >= 1:
            return "retry"
        return "denied"

    def upsert_download(self, item_id: str, document_message_id: int, *,
                        telegram_document_id: str | None = None,
                        expected_size_bytes: int | None = None,
                        local_path: str | None = None,
                        sha256: str | None = None, status: str = "pending",
                        attempts: int = 0,
                        last_error: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO downloads
                   (item_id, document_message_id, telegram_document_id,
                    expected_size_bytes, local_path, sha256, status,
                    attempts, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id, document_message_id) DO UPDATE SET
                     telegram_document_id = excluded.telegram_document_id,
                     expected_size_bytes = excluded.expected_size_bytes,
                     local_path = excluded.local_path,
                     sha256 = excluded.sha256,
                     status = excluded.status,
                     attempts = excluded.attempts,
                     last_error = excluded.last_error""",
                (item_id, document_message_id, telegram_document_id,
                 expected_size_bytes, local_path, sha256, status,
                 attempts, last_error))

    def list_downloads(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM downloads ORDER BY item_id, "
            "document_message_id").fetchall()

    def get_download(self, item_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM downloads WHERE item_id = ?",
            (item_id,)).fetchone()

    def reset_item_policy(self, item_id: str) -> None:
        """§6.4 B：重建时清理待重算的 policy 决定。"""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET noise_decision = NULL, "
                "interest_decision = NULL WHERE item_id = ?", (item_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def list_source_items(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self._conn.execute(
                "SELECT * FROM source_items ORDER BY item_id").fetchall()
        return self._conn.execute(
            "SELECT * FROM source_items WHERE kind = ? ORDER BY item_id",
            (kind,)).fetchall()

    def find_stranded_items(self) -> list[sqlite3.Row]:
        """C1 恢复扫描：finalized 但从未物化成功的 text item。"""
        return self._conn.execute(
            "SELECT * FROM source_items WHERE kind = 'text' "
            "AND finalized_at IS NOT NULL AND materialized_path IS NULL "
            "AND processing_status != 'deleted_source'").fetchall()

    # ---------- TG5：decision 落库与审计 ----------

    def set_item_noise(self, item_id: str, decision: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET noise_decision = ? "
                "WHERE item_id = ?", (decision, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def set_item_interest(self, item_id: str, decision: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE source_items SET interest_decision = ? "
                "WHERE item_id = ?", (decision, item_id))
        if cursor.rowcount == 0:
            raise ValueError(f"item not found: {item_id}")

    def get_ai_budget(self, source_id: str, classifier_kind: str):
        return self._conn.execute(
            "SELECT * FROM source_ai_budget WHERE source_id = ? "
            "AND classifier_kind = ?", (source_id,
                                        classifier_kind)).fetchone()

    def create_classifier_audit(self, item_id: str, kind: str,
                                decision: str, *, reason_code=None,
                                confidence=None, policy_version: str,
                                classifier_version: str) -> int:
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO classifier_audit
                   (item_id, kind, decision, reason_code, confidence,
                    policy_version, classifier_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, kind, decision, reason_code, confidence,
                 policy_version, classifier_version, _now_iso()))
        return int(cursor.lastrowid)

    def list_classifier_audit(self, item_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM classifier_audit WHERE item_id = ? "
            "ORDER BY audit_id", (item_id,)).fetchall()

    # ---------- reviews（§16 REVIEW 机制） ----------

    def create_review(self, item_id: str, reason: str, *, kind=None,
                      size_bytes=None) -> int:
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO reviews
                   (item_id, reason, kind, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, reason, kind, size_bytes, _now_iso()))
        return int(cursor.lastrowid)

    def get_review(self, review_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM reviews WHERE review_id = ?",
            (review_id,)).fetchone()

    def list_open_reviews(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM reviews WHERE resolved_at IS NULL "
            "ORDER BY review_id").fetchall()

    def resolve_review(self, review_id: int, decision: str) -> str:
        """§16.3：同 decision 幂等 no-op；不同 decision 拒绝覆盖。

        DOWNLOAD_ONCE 在此只落 item 级授权事实
        （decision + decision_consumed_at IS NULL）；
        消费由 TG4 下载器负责。
        """
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"invalid review decision: {decision}")
        row = self.get_review(review_id)
        if row is None:
            raise ValueError(f"review not found: {review_id}")
        if row["resolved_at"] is not None:
            if row["decision"] == decision:
                return "no-op"
            raise ReviewConflictError(
                f"review {review_id} already resolved as "
                f"{row['decision']}; refusing to overwrite with "
                f"{decision}")
        # UPDATE 带 resolved_at IS NULL 守卫：读取与写入之间被并发
        # 解决时 rowcount=0，以库内事实裁决，绝不静默覆盖
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE reviews SET resolved_at = ?, decision = ? "
                "WHERE review_id = ? AND resolved_at IS NULL",
                (_now_iso(), decision, review_id))
        if cursor.rowcount == 0:
            current = self._conn.execute(
                "SELECT decision FROM reviews WHERE review_id = ?",
                (review_id,)).fetchone()
            if current is not None and current["decision"] == decision:
                return "no-op"
            raise ReviewConflictError(
                f"review {review_id} was concurrently resolved; "
                f"refusing to overwrite with {decision}")
        return "resolved"

    # ---------- status（只读，§23 telegram status） ----------

    def status_summary(self) -> dict[str, Any]:
        scalars = {
            "sources_enabled":
                "SELECT COUNT(*) FROM tg_sources WHERE enabled = 1",
            "sources_disabled":
                "SELECT COUNT(*) FROM tg_sources WHERE enabled = 0",
            "messages_total": "SELECT COUNT(*) FROM tg_messages",
            "open_reviews":
                "SELECT COUNT(*) FROM reviews WHERE resolved_at IS NULL",
            "pending_resources":
                "SELECT COUNT(*) FROM source_items WHERE kind = "
                "'cloud_link' AND processing_status = 'PENDING_RESOURCE'",
        }
        summary: dict[str, Any] = {"schema_version": self.user_version()}
        for key, sql in scalars.items():
            summary[key] = int(self._conn.execute(sql).fetchone()[0])
        summary["sources_last_seen"] = [
            {"source_id": row["source_id"],
             "enabled": bool(row["enabled"]),
             "last_seen_message_id": row["last_seen_message_id"],
             "last_reconciled_at": row["last_reconciled_at"]}
            for row in self.list_sources()]
        summary["downloads_by_status"] = {
            row["status"]: row["n"] for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM downloads "
                "GROUP BY status")}
        summary["recent_handoffs"] = [
            {"item_id": row["item_id"],
             "job_id": row["knowledge_ingest_job_id"]}
            for row in self._conn.execute(
                "SELECT item_id, knowledge_ingest_job_id FROM source_items "
                "WHERE handoff_completed = 1 ORDER BY created_at DESC "
                "LIMIT 5")]
        summary["download_once_pending"] = [
            row["item_id"] for row in self._conn.execute(
                "SELECT item_id FROM reviews WHERE decision = "
                "'DOWNLOAD_ONCE' AND decision_consumed_at IS NULL")]
        return summary


# ---------- TG5：Source AI Budget + 分类审计（§21.1 / §7.5） ----------


class AIBudgetGuard:
    """Source 级模型预算（独立于 Target Budget；SQLite 分账）。

    复用 budget.py 的 acquire/outcome/quota/breaker 思想：
    - acquire(request_id) 幂等（重放不重复计数）；
    - outcome 幂等（同 permit 同结果 no-op，冲突拒绝）；
    - success 双清零；empty/rate_limit 递增对应熔断计数；
    - 拒绝理由：budget_exhausted / breaker_open。
    状态持久在 state.db（source × classifier_kind 维度）。
    """

    def __init__(self, store: TelegramEventStore, *,
                 max_calls: int = 100,
                 breaker_empty: int = 3,
                 breaker_rate_limit: int = 2):
        self.store = store
        self.max_calls = max_calls
        self.breaker_empty = breaker_empty
        self.breaker_rate_limit = breaker_rate_limit

    def _row(self, source_id: str, kind: str) -> dict:
        row = self.store.get_ai_budget(source_id, kind)
        if row is None:
            with self.store._conn:
                self.store._conn.execute(
                    "INSERT OR IGNORE INTO source_ai_budget "
                    "(source_id, classifier_kind) VALUES (?, ?)",
                    (source_id, kind))
            row = self.store.get_ai_budget(source_id, kind)
        return dict(row)

    def acquire(self, source_id: str, kind: str, request_id: str):
        row = self._row(source_id, kind)
        permits = json.loads(row["permits_json"] or "{}")
        if request_id in permits:
            return ("permit", request_id)      # 重放幂等
        if row["calls"] >= self.max_calls:
            return ("denied", "budget_exhausted")
        if (row["consecutive_empty"] >= self.breaker_empty
                or row["consecutive_rate_limit"] >= self.breaker_rate_limit):
            return ("denied", "breaker_open")
        permits[request_id] = "pending"
        with self.store._conn:
            self.store._conn.execute(
                """UPDATE source_ai_budget SET calls = calls + 1,
                     attempts = attempts + 1, permits_json = ?
                   WHERE source_id = ? AND classifier_kind = ?""",
                (json.dumps(permits), source_id, kind))
        return ("permit", request_id)

    def outcome(self, source_id: str, kind: str, request_id: str,
                result: str) -> None:
        if result not in ("success", "empty", "rate_limit"):
            raise ValueError(f"invalid budget outcome: {result}")
        row = self._row(source_id, kind)
        permits = json.loads(row["permits_json"] or "{}")
        if request_id not in permits:
            raise ValueError(f"unknown budget request: {request_id}")
        if permits[request_id] not in ("pending", result):
            raise ValueError(
                f"budget outcome conflict for {request_id}: "
                f"{permits[request_id]} != {result}")
        if permits[request_id] == result:
            return                              # 幂等 no-op
        permits[request_id] = result
        empty = row["consecutive_empty"]
        rate = row["consecutive_rate_limit"]
        if result == "success":
            empty = 0
            rate = 0
        elif result == "empty":
            empty += 1
            rate = 0
        else:
            rate += 1
            empty = 0
        with self.store._conn:
            self.store._conn.execute(
                """UPDATE source_ai_budget SET
                     consecutive_empty = ?, consecutive_rate_limit = ?,
                     permits_json = ?
                   WHERE source_id = ? AND classifier_kind = ?""",
                (empty, rate, json.dumps(permits), source_id, kind))
