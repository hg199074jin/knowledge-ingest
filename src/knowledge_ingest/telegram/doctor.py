"""TG8: Telegram Doctor——长期无人值守的体检（§10.2，只读）。

每项检查返回 (name, status, detail)；status ∈ PASS/WARN/FAIL/SKIP。
整体退出码：存在 FAIL → 1，否则 0。绝不修改任何状态（--fix 属
独立命令，另行实现并强制 maintenance lock）。
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from knowledge_ingest.config import AppConfig

from . import watch_agent
from .event_store import SCHEMA_VERSION, TelegramEventStore

RECONCILE_STALE_SECONDS = 1800
NOTIFY_UNDELIVERED_WARN = 5
ORICO_ROOT = Path("/Volumes/ORICO")


def orico_online() -> bool:
    """§6.8 fail-closed 的在线判定；测试可 monkeypatch。"""
    return ORICO_ROOT.is_dir()


def _check(name: str, status: str, detail: str) -> tuple[str, str, str]:
    return (name, status, detail)


def _report_empty(db: str) -> int:
    checks = [_check("state.db 存在", "FAIL", f"{db} 不存在"),
              _check("LaunchAgent", "WARN",
                     str(watch_agent.plist_install_path()))]
    for name, status, detail in checks:
        print(f"[{status:4}] {name}: {detail}")
    print("doctor: 1 FAIL / 0 WARN / 2 checks")
    return 1


def session_dir(config: AppConfig) -> Path:
    return Path.home() / ".config" / "knowledge-ingest" / "telegram"


def run_checks(config: AppConfig, *,
               store: TelegramEventStore | None = None) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    db_path = config.pipeline_root / "telegram" / "state.db"
    session_dir_ = session_dir(config)
    session_files = list(session_dir_.glob("*.session")) \
        if session_dir_.is_dir() else []

    # 1/2 session 存在与权限
    if not session_files:
        checks.append(_check("session 存在", "FAIL",
                             f"{session_dir_} 下无 .session"))
    else:
        checks.append(_check("session 存在", "PASS",
                             str(session_files[0].name)))
        mode = stat.S_IMODE(session_files[0].stat().st_mode)
        perms_ok = mode & 0o077 == 0
        checks.append(_check("session 权限", "PASS" if perms_ok else "FAIL",
                             oct(mode)))

    # 3 API 登录有效性：会与常驻 watcher 抢 session，只做旁证
    checks.append(_check("API 登录有效", "SKIP",
                         "由运行中的 watcher 旁证（避免 session 争用）"))

    # 4 ORICO 在线（经 orico_online() 间接判断，测试可拦截）
    online = orico_online()
    checks.append(_check("ORICO 在线", "PASS" if online else "FAIL",
                         "/Volumes/ORICO"))

    # 5-16 state.db 各项
    checks.append(_check("state.db 存在", "PASS", str(db_path)))
    try:
        quick = store and store._conn.execute(
            "PRAGMA quick_check").fetchone()[0] if store else None
    except sqlite3.Error as exc:
        checks.append(_check("state.db integrity", "FAIL", str(exc)[:80]))
        return checks
    checks.append(_check("state.db integrity",
                         "PASS" if quick in (None, "ok") else "FAIL",
                         quick or "ok"))

    version = store.user_version()
    checks.append(_check("user_version",
                         "PASS" if version == SCHEMA_VERSION else "FAIL",
                         f"{version} (latest {SCHEMA_VERSION})"))

    # enabled source 均已绑定 chat_id（不做 Telegram 在线 resolve）
    bad_sources = [r["source_id"] for r in store.list_sources()
                   if r["enabled"] and r["chat_id"] is None]
    checks.append(_check("enabled source 可解析（chat_id 绑定）",
                         "PASS" if not bad_sources else "FAIL",
                         ", ".join(bad_sources) or "全部绑定"))

    # 长时间未 reconcile
    fresh = store._conn.execute(
        "SELECT MAX(last_reconciled_at) m FROM tg_sources "
        "WHERE enabled = 1").fetchone()[0]
    if fresh:
        age = (datetime.now(UTC)
               - datetime.fromisoformat(fresh)).total_seconds()
        checks.append(_check("reconcile 时效",
                             "PASS" if age < RECONCILE_STALE_SECONDS
                             else "WARN",
                             f"{int(age)}s 前"))
    else:
        checks.append(_check("reconcile 时效", "WARN", "启用源从未 reconcile"))

    # materialized 文件存在
    missing_files = 0
    materialized = 0
    for row in store._conn.execute(
            "SELECT item_id, materialized_path FROM source_items "
            "WHERE materialized_path IS NOT NULL"):
        materialized += 1
        if not Path(row["materialized_path"]).is_file():
            missing_files += 1
    checks.append(_check("materialized 文件存在",
                         "PASS" if missing_files == 0 else "FAIL",
                         f"{materialized - missing_files}/{materialized}"))

    # complete download 文件存在 + SHA 匹配
    from knowledge_ingest.fingerprint import fingerprint_file

    dl_bad = []
    dl_total = 0
    for row in store._conn.execute(
            "SELECT item_id, local_path, sha256 FROM downloads "
            "WHERE status = 'complete'"):
        dl_total += 1
        path = Path(row["local_path"]) if row["local_path"] else None
        if path is None or not path.is_file():
            dl_bad.append(f"{row['item_id']}:文件缺失")
            continue
        if row["sha256"]:
            actual = fingerprint_file(path).removeprefix("sha256:")
            if actual != row["sha256"]:
                dl_bad.append(f"{row['item_id']}:SHA 不匹配")
    checks.append(_check("下载文件存在且 SHA 匹配",
                         "PASS" if not dl_bad else "FAIL",
                         f"{dl_total - len(dl_bad)}/{dl_total}"
                         + (f"; { '; '.join(dl_bad[:3])}" if dl_bad else "")))

    # source_item → job_id 存在
    jobs_root = config.pipeline_root / "jobs"
    orphan = [r["item_id"] for r in store._conn.execute(
        "SELECT item_id, knowledge_ingest_job_id j FROM source_items "
        "WHERE handoff_completed = 1")
        if not (jobs_root / r["j"] / "job.yaml").is_file()]
    checks.append(_check("handoff → job 存在",
                         "PASS" if not orphan else "FAIL",
                         ", ".join(orphan) or "全部存在"))

    # REVIEW 积压
    open_reviews = store._conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE resolved_at IS NULL") \
        .fetchone()[0]
    checks.append(_check("REVIEW 积压",
                         "PASS" if open_reviews == 0 else "WARN",
                         f"{open_reviews} 条待人工"))

    # DOWNLOAD_ONCE 异常授权：已消费但无 complete 下载
    stuck_once = [r["item_id"] for r in store._conn.execute(
        """SELECT DISTINCT r.item_id FROM reviews r
           WHERE r.decision = 'DOWNLOAD_ONCE'
             AND r.decision_consumed_at IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM downloads d
                           WHERE d.item_id = r.item_id
                             AND d.status = 'complete')""")]
    checks.append(_check("DOWNLOAD_ONCE 消费一致性",
                         "PASS" if not stuck_once else "WARN",
                         ", ".join(stuck_once) or "正常"))

    # 通知投递失败
    undelivered = store._conn.execute(
        "SELECT COUNT(*) FROM notifications "
        "WHERE delivered_at IS NULL").fetchone()[0]
    checks.append(_check("通知投递",
                         "PASS" if undelivered < NOTIFY_UNDELIVERED_WARN
                         else "WARN",
                         f"{undelivered} 条未投递"))

    # LaunchAgent 状态
    wa = watch_agent.plist_install_path()
    checks.append(_check("LaunchAgent(watch)",
                         "PASS" if wa.is_file() and watch_agent.is_loaded()
                         else "WARN",
                         str(wa)))

    # digest cursor（TG7 日报游标）
    checks.append(_check("digest 游标",
                         "PASS" if store.get_digest_state(
                             "digest:last_sent_at") else "SKIP",
                         store.get_digest_state("digest:last_sent_at")
                         or "从未发送（digest --send 推进）"))
    return checks


def run(config: AppConfig, *, store: TelegramEventStore | None = None) -> int:
    db_path = config.pipeline_root / "telegram" / "state.db"
    if store is None:
        if not db_path.is_file():
            # §4.6：只读体检不得创建 state.db（评审 I4）
            return _report_empty(str(db_path))
    checks = run_checks(config, store=store)
    for name, status, detail in checks:
        print(f"[{status:4}] {name}: {detail}")
    fails = sum(1 for c in checks if c[1] == "FAIL")
    warns = sum(1 for c in checks if c[1] == "WARN")
    print(f"doctor: {fails} FAIL / {warns} WARN / {len(checks)} checks")
    return 1 if fails else 0


def stale_seconds(config: AppConfig) -> int:
    """供 --max-age 类阈值复用（保留扩展点）。"""
    return int(timedelta(seconds=RECONCILE_STALE_SECONDS).total_seconds())
