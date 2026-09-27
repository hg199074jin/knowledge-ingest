"""V3 M1: independent Insight Store (`insight/state.db`).

事实边界（设计 §4.2/§19）：`telegram/state.db` 只回答"Telegram 当时发生了
什么"；本库只回答"这些材料对用户意味着什么"。两库永不互写。

约定：
- WAL + foreign_keys + busy_timeout 5000ms（沿 TelegramEventStore 纪律）；
- user_version 高于本代码支持 → fail-fast（UnsupportedInsightSchemaError）；
- source 稳定身份 = (provider, source_item_id)；content_fingerprint 变化
  → source_revision += 1（同一逻辑身份），已 ADOPT 的旧 revision 由上层
  （M7 Human Gate）保证不被新 EDIT 静默覆盖；
- candidate/value-gate 决策按 (source, revision) 一行：相同决策幂等
  no-op，不同决策 latest-wins（Store 为 restart/idempotence authority，
  实施方案 Task 8 Step 1/2）；
- 不在 run/error 等通用字段保存整份个人长期知识正文。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from knowledge_ingest.config import AppConfig

from .models import CandidateDecision, DeepValueDecision, InsightSourceView

INSIGHT_SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000


class UnsupportedInsightSchemaError(RuntimeError):
    """insight/state.db user_version 高于本代码支持（fail-fast）。"""


def insight_root(config: AppConfig) -> Path:
    return Path(config.pipeline_root) / "insight"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _source_identity(provider: str, source_item_id: str) -> str:
    digest = hashlib.sha256(
        f"{provider}\x00{source_item_id}".encode()).hexdigest()
    return f"isv.{digest[:16]}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS insight_sources (
    insight_source_id   TEXT PRIMARY KEY,
    provider            TEXT NOT NULL,
    source_item_id      TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    source_revision     INTEGER NOT NULL DEFAULT 1,
    content_kind        TEXT NOT NULL,
    title               TEXT,
    visible_text        TEXT NOT NULL DEFAULT '',
    materialized_path   TEXT,
    full_text_available INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT NOT NULL DEFAULT 'source_only',
    provenance_json     TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (provider, source_item_id)
);

CREATE TABLE IF NOT EXISTS insight_candidates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_source_id TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER NOT NULL,
    candidate         INTEGER NOT NULL,
    decision_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (insight_source_id, source_revision)
);

-- Stage 2 Deep-Value Gate 决策（方案 11 表清单之外的对称补充：
-- M8 restart-safe 需要持久化 gate 决策，语义与 candidates 对称）
CREATE TABLE IF NOT EXISTS insight_value_gates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_source_id TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER NOT NULL,
    decision          TEXT NOT NULL,
    decision_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (insight_source_id, source_revision)
);

CREATE TABLE IF NOT EXISTS insight_runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_source_id TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER NOT NULL,
    stage             TEXT NOT NULL,
    status            TEXT NOT NULL,
    error_code        TEXT,
    output_ref        TEXT,
    created_at        TEXT NOT NULL,
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS insight_context_refs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_source_id TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER NOT NULL,
    record_id         TEXT NOT NULL,
    relation_reason   TEXT NOT NULL,
    state             TEXT NOT NULL,
    source_ref        TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insight_cards (
    card_id             TEXT PRIMARY KEY,
    insight_source_id   TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision     INTEGER NOT NULL,
    content_fingerprint TEXT NOT NULL,
    card_path           TEXT NOT NULL,
    quality_status      TEXT NOT NULL,
    cognition_delta     TEXT,
    human_gate          TEXT NOT NULL DEFAULT 'pending',
    meta_json           TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognition_proposals (
    proposal_id       TEXT PRIMARY KEY,
    card_id           TEXT NOT NULL,
    insight_source_id TEXT NOT NULL REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER NOT NULL,
    change_type       TEXT NOT NULL,
    proposal_json     TEXT NOT NULL,
    gate_status       TEXT NOT NULL DEFAULT 'pending',
    gate_decision     TEXT,
    gate_resolved_at  TEXT,
    gate_decision_source TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_signals (
    watch_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_source_id TEXT REFERENCES insight_sources(insight_source_id),
    source_revision   INTEGER,
    trend_key         TEXT NOT NULL,
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'WATCH',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trend_clusters (
    cluster_id   TEXT PRIMARY KEY,
    trend_key    TEXT NOT NULL UNIQUE,
    signal_count INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_cognition (
    record_id   TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    domain      TEXT,
    cognition   TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'CONFIRMED',
    source_ref  TEXT,
    approved_at TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT 'human_gate'
);

CREATE TABLE IF NOT EXISTS insight_ai_budget (
    stage                  TEXT PRIMARY KEY,
    calls                  INTEGER NOT NULL DEFAULT 0,
    attempts               INTEGER NOT NULL DEFAULT 0,
    consecutive_empty      INTEGER NOT NULL DEFAULT 0,
    consecutive_rate_limit INTEGER NOT NULL DEFAULT 0,
    consecutive_error      INTEGER NOT NULL DEFAULT 0
);

-- 预算 permit 幂等（M3）：request_id 重放不重复计数
CREATE TABLE IF NOT EXISTS insight_ai_permits (
    stage      TEXT NOT NULL,
    request_id TEXT NOT NULL,
    outcome    TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    PRIMARY KEY (stage, request_id)
);

CREATE TABLE IF NOT EXISTS insight_digest_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class InsightStore:
    """insight/state.db 的唯一写入口。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=BUSY_TIMEOUT_MS / 1000.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise

    # ---------- schema / pragmas ----------

    def user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _ensure_schema(self) -> None:
        version = self.user_version()
        if version > INSIGHT_SCHEMA_VERSION:
            raise UnsupportedInsightSchemaError(
                f"insight/state.db user_version={version} > supported "
                f"{INSIGHT_SCHEMA_VERSION}; refusing to open (fail-fast)")
        # 全部 DDL 幂等（IF NOT EXISTS）：新表对既有库自动补建
        self._conn.executescript(_SCHEMA)
        if self.user_version() < INSIGHT_SCHEMA_VERSION:
            self._conn.execute(
                f"PRAGMA user_version={INSIGHT_SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- sources / revisions ----------

    def register_source_view(self, view: InsightSourceView) -> str:
        """幂等注册：同 (provider, source_item_id) 同一逻辑身份；

        content_fingerprint 变化 → source_revision += 1 并刷新派生副本；
        指纹未变 → 严格 no-op（M8 增量扫描的幂等基础）。
        """
        sid = _source_identity(view.provider, view.source_item_id)
        now = _now_iso()
        provenance = {
            "captured_at": view.captured_at,
            "author": view.author,
            "source_deleted": view.source_deleted,
            "original_refs": list(view.original_refs),
        }
        with self._conn:
            row = self._conn.execute(
                "SELECT content_fingerprint FROM insight_sources "
                "WHERE provider = ? AND source_item_id = ?",
                (view.provider, view.source_item_id)).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO insight_sources
                       (insight_source_id, provider, source_item_id,
                        content_fingerprint, source_revision, content_kind,
                        title, visible_text, materialized_path,
                        full_text_available, verification_status,
                        provenance_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sid, view.provider, view.source_item_id,
                     view.content_fingerprint, view.content_kind,
                     view.title, view.visible_text, view.materialized_path,
                     1 if view.full_text_available else 0,
                     view.verification_status, json.dumps(provenance,
                                                          ensure_ascii=False),
                     now, now))
            elif row["content_fingerprint"] != view.content_fingerprint:
                self._conn.execute(
                    """UPDATE insight_sources
                       SET content_fingerprint = ?, source_revision =
                             source_revision + 1, content_kind = ?,
                           title = ?, visible_text = ?, materialized_path = ?,
                           full_text_available = ?, verification_status = ?,
                           provenance_json = ?, updated_at = ?
                       WHERE insight_source_id = ?""",
                    (view.content_fingerprint, view.content_kind,
                     view.title, view.visible_text, view.materialized_path,
                     1 if view.full_text_available else 0,
                     view.verification_status, json.dumps(provenance,
                                                          ensure_ascii=False),
                     now, sid))
        return sid

    def get_source(self, insight_source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM insight_sources WHERE insight_source_id = ?",
            (insight_source_id,)).fetchone()

    def count_sources(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM insight_sources").fetchone()[0]

    def _require_source_revision(self, insight_source_id: str) -> int:
        row = self.get_source(insight_source_id)
        if row is None:
            raise ValueError(f"insight source not found: {insight_source_id}")
        return int(row["source_revision"])

    # ---------- candidate / value-gate decisions ----------

    def record_candidate(self, insight_source_id: str,
                         decision: CandidateDecision) -> None:
        revision = self._require_source_revision(insight_source_id)
        payload = {
            "candidate": decision.candidate,
            "possible_value": list(decision.possible_value),
            "reasons": list(decision.reasons),
            "confidence": decision.confidence,
            "trend_key": decision.trend_key,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = _now_iso()
        with self._conn:
            row = self._conn.execute(
                "SELECT decision_json FROM insight_candidates "
                "WHERE insight_source_id = ? AND source_revision = ?",
                (insight_source_id, revision)).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO insight_candidates
                       (insight_source_id, source_revision, candidate,
                        decision_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (insight_source_id, revision,
                     1 if decision.candidate else 0, blob, now, now))
            elif row["decision_json"] == blob:
                return                     # 同决策幂等 no-op
            else:
                self._conn.execute(
                    """UPDATE insight_candidates
                       SET candidate = ?, decision_json = ?, updated_at = ?
                       WHERE insight_source_id = ? AND source_revision = ?""",
                    (1 if decision.candidate else 0, blob, now,
                     insight_source_id, revision))

    def latest_candidate(self, insight_source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT * FROM insight_candidates
               WHERE insight_source_id = ?
               ORDER BY source_revision DESC, id DESC LIMIT 1""",
            (insight_source_id,)).fetchone()

    def count_candidates(self, insight_source_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM insight_candidates "
            "WHERE insight_source_id = ?",
            (insight_source_id,)).fetchone()[0]

    def record_value_gate(self, insight_source_id: str,
                          decision: DeepValueDecision) -> None:
        revision = self._require_source_revision(insight_source_id)
        payload = {
            "decision": decision.decision,
            "reason": decision.reason,
            "novelty": decision.novelty,
            "cognitive_delta_potential": decision.cognitive_delta_potential,
            "business_potential": decision.business_potential,
            "project_relevance": decision.project_relevance,
            "transferability": decision.transferability,
            "evidence_quality": decision.evidence_quality,
            "contradiction_value": decision.contradiction_value,
            "thinking_space": decision.thinking_space,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = _now_iso()
        with self._conn:
            row = self._conn.execute(
                "SELECT decision_json FROM insight_value_gates "
                "WHERE insight_source_id = ? AND source_revision = ?",
                (insight_source_id, revision)).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO insight_value_gates
                       (insight_source_id, source_revision, decision,
                        decision_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (insight_source_id, revision, decision.decision, blob,
                     now, now))
            elif row["decision_json"] == blob:
                return
            else:
                self._conn.execute(
                    """UPDATE insight_value_gates
                       SET decision = ?, decision_json = ?, updated_at = ?
                       WHERE insight_source_id = ? AND source_revision = ?""",
                    (decision.decision, blob, now,
                     insight_source_id, revision))

    def latest_value_gate(self, insight_source_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT * FROM insight_value_gates
               WHERE insight_source_id = ?
               ORDER BY source_revision DESC, id DESC LIMIT 1""",
            (insight_source_id,)).fetchone()

    def count_value_gates(self, insight_source_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM insight_value_gates "
            "WHERE insight_source_id = ?",
            (insight_source_id,)).fetchone()[0]

    # ---------- runs ----------

    def create_run(self, insight_source_id: str, stage: str,
                   status: str, *, error_code: str | None = None) -> int:
        revision = self._require_source_revision(insight_source_id)
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO insight_runs
                   (insight_source_id, source_revision, stage, status,
                    error_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (insight_source_id, revision, stage, status, error_code,
                 _now_iso()))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, *,
                   output_ref: str | None = None,
                   error_code: str | None = None) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE insight_runs
                   SET status = ?, output_ref = ?, error_code = ?,
                       finished_at = ?
                   WHERE run_id = ?""",
                (status, output_ref, error_code, _now_iso(), run_id))
        if cursor.rowcount == 0:
            raise ValueError(f"run not found: {run_id}")

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM insight_runs WHERE run_id = ?",
            (run_id,)).fetchone()

    # ---------- insight AI budget persistence（M3） ----------

    def acquire_budget_permit(self, stage: str, request_id: str) -> bool:
        """创建 permit（幂等）；返回是否为新建。"""
        try:
            with self._conn:
                self._conn.execute(
                    """INSERT INTO insight_ai_permits
                       (stage, request_id, created_at) VALUES (?, ?, ?)""",
                    (stage, request_id, _now_iso()))
        except sqlite3.IntegrityError:
            return False
        with self._conn:
            self._conn.execute(
                """INSERT INTO insight_ai_budget (stage, attempts)
                   VALUES (?, 1)
                   ON CONFLICT(stage) DO UPDATE SET
                       attempts = attempts + 1""", (stage,))
        return True

    def get_budget_permit(self, stage: str, request_id: str):
        return self._conn.execute(
            "SELECT * FROM insight_ai_permits "
            "WHERE stage = ? AND request_id = ?",
            (stage, request_id)).fetchone()

    def settle_budget_permit(self, stage: str, request_id: str,
                             outcome: str) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE insight_ai_permits
                   SET outcome = ?, settled_at = ?
                   WHERE stage = ? AND request_id = ? AND outcome IS NULL""",
                (outcome, _now_iso(), stage, request_id))
        if cursor.rowcount == 0:
            raise ValueError(
                f"budget permit not open: {stage}:{request_id}")

    def bump_budget_outcome(self, stage: str, outcome: str) -> None:
        """更新 stage 计数：success 清零连续计数，其余按类型连续累计。"""
        if outcome not in ("success", "empty", "rate_limit", "error"):
            raise ValueError(f"unknown budget outcome: {outcome!r}")
        with self._conn:
            self._conn.execute(
                """INSERT INTO insight_ai_budget (stage) VALUES (?)
                   ON CONFLICT(stage) DO NOTHING""", (stage,))
            if outcome == "success":
                self._conn.execute(
                    """UPDATE insight_ai_budget
                       SET calls = calls + 1, consecutive_empty = 0,
                           consecutive_rate_limit = 0, consecutive_error = 0
                       WHERE stage = ?""", (stage,))
            else:
                column = {"empty": "consecutive_empty",
                          "rate_limit": "consecutive_rate_limit",
                          "error": "consecutive_error"}[outcome]
                others = {"empty": ("consecutive_rate_limit",
                                    "consecutive_error"),
                          "rate_limit": ("consecutive_empty",
                                         "consecutive_error"),
                          "error": ("consecutive_empty",
                                    "consecutive_rate_limit")}[outcome]
                self._conn.execute(
                    f"""UPDATE insight_ai_budget
                        SET {column} = {column} + 1,
                            {others[0]} = 0, {others[1]} = 0
                        WHERE stage = ?""", (stage,))

    def get_budget_row(self, stage: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM insight_ai_budget WHERE stage = ?",
            (stage,)).fetchone()

    def reset_budget(self, stage: str | None = None) -> None:
        with self._conn:
            if stage is None:
                self._conn.execute(
                    """UPDATE insight_ai_budget
                       SET calls = 0, attempts = 0, consecutive_empty = 0,
                           consecutive_rate_limit = 0, consecutive_error = 0""")
            else:
                self._conn.execute(
                    """UPDATE insight_ai_budget
                       SET calls = 0, attempts = 0, consecutive_empty = 0,
                           consecutive_rate_limit = 0, consecutive_error = 0
                       WHERE stage = ?""", (stage,))
