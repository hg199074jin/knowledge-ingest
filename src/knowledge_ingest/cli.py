"""knowledge-ingest command line interface."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import functools
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from datetime import UTC
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.doctor import has_fail, run_doctor
from knowledge_ingest.encoding import TEXT_SUFFIXES, preflight_utf8
from knowledge_ingest.manifest_store import LockHeld, ManifestStore, flock_ctx
from knowledge_ingest.models import JobRequest
from knowledge_ingest.router import effective_paths, route_source
from knowledge_ingest.state_machine import InvalidTransition, transition_to
from knowledge_ingest.targets import (
    get as get_target,
)
from knowledge_ingest.targets import (
    target_choices,
)
from knowledge_ingest.telegram.event_store import (
    AIBudgetGuard,
    TelegramEventStore,
)
from knowledge_ingest.telegram.registry import register_source

BAIDU_APP_PREFIXES = ("apps/bdpan", "/apps/bdpan")


def _log(config: AppConfig, manifest, event: str, **fields) -> None:
    from knowledge_ingest.report import EventLog

    job_dir = _store(config).job_dir(manifest.job_id)
    EventLog(job_dir / "logs" / "events.jsonl").log(
        event, job_id=manifest.job_id, **fields)


def _source_fingerprint(path: Path) -> str | None:
    from knowledge_ingest.fingerprint import (
        fingerprint_collection,
        fingerprint_file,
    )

    path = Path(path)
    if not path.exists():
        return None
    if path.is_dir():
        return fingerprint_collection(path).sha256
    return fingerprint_file(path)


LOCK_NAME = ".preprocess.lock"


def _flock_ctx(lock_path: Path):
    """preprocess 业务互斥：EXCLUSIVE + NON-BLOCKING（拿不到立即放弃，不等待）。"""
    return flock_ctx(lock_path, blocking=False)


def _lock_holder(lock: Path) -> str:
    """锁文件里的 PID+时间戳诊断（仅展示用，不作为判活依据）。"""
    try:
        content = Path(lock).read_text(encoding="utf-8").strip()
    except OSError:
        return "<no diagnostic>"
    return content or "<no diagnostic>"


def _lock_alive(lock: Path) -> bool:
    """flock 探测判活：实际尝试对该锁文件非阻塞 flock——能拿到=无活进程。

    锁文件内容（PID/时间戳）仅作诊断展示；文件存在但无人持有 = 陈旧锁，不算活。
    """
    lock = Path(lock)
    if not lock.is_file():
        return False
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # 有活进程持有
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _locked(fn):
    """preprocess 独占锁（业务互斥）：运行期间禁止第二个 preprocess。

    EXCLUSIVE + NON-BLOCKING：拿不到锁 → 打印诊断并返回退出码 3（不重试不等待）。
    """
    @functools.wraps(fn)
    def wrapper(config: AppConfig, args) -> int:
        lock = _store(config).job_dir(args.job_id) / LOCK_NAME
        try:
            with _flock_ctx(lock):
                return fn(config, args)
        except LockHeld:
            print(f"another preprocess holds this job: {_lock_holder(lock)}",
                  file=sys.stderr)
            return 3
    return wrapper


def _jobs_root(config: AppConfig) -> Path:
    return config.pipeline_root / "jobs"


def _store(config: AppConfig) -> ManifestStore:
    return ManifestStore(jobs_root=_jobs_root(config))


def _load_job(store: ManifestStore, job_id: str):
    try:
        return store.load(job_id)
    except FileNotFoundError:
        print(f"error: job not found: {job_id}", file=sys.stderr)
        raise SystemExit(2) from None


def _advance(manifest, statuses: list[str]) -> None:
    for status in statuses:
        transition_to(manifest, status)


def _block(manifest, reason: str, **fields) -> None:
    entry = {"reason": reason}
    entry.update(fields)
    manifest.errors.append(entry)
    transition_to(manifest, "BLOCKED")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-ingest",
        description="Multi-source knowledge ingestion orchestrator",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="path to config YAML")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=[common],
                            help="verify machine baseline")
    doctor.add_argument("--json", dest="json_output", action="store_true",
                        help="emit machine-readable JSON")

    create = sub.add_parser("job", parents=[common], help="job operations")
    job_sub = create.add_subparsers(dest="job_command", required=True)
    create_cmd = job_sub.add_parser("create", parents=[common],
                                    help="create a new ingest job")
    create_cmd.add_argument("--provider", required=True,
                            choices=["local", "baidu", "quark", "telegram"])
    create_cmd.add_argument("--source", required=True,
                            help="cloud path, share link, or local path")
    create_cmd.add_argument("--target", dest="targets", action="append",
                            required=True, choices=target_choices())
    create_cmd.add_argument("--prompt", default="",
                            help="raw user request text for provenance")
    create_cmd.add_argument(
        "--with-router", dest="with_router", nargs="?",
        const="family_router", default=None, metavar="TARGET",
        help="also request a router target (default: family_router); "
             "auto-ensures its direct depends_on entry (no recursion, "
             "no auto-start); fail-fast if a dependency is unregistered")
    amend_cmd = job_sub.add_parser(
        "amend", parents=[common],
        help="modify a job (refused while preprocess holds the lock)")
    amend_cmd.add_argument("job_id")
    amend_cmd.add_argument("--add-target", required=True,
                           choices=target_choices())

    register = sub.add_parser("source", parents=[common], help="source operations")
    source_sub = register.add_subparsers(dest="source_command", required=True)
    register_cmd = source_sub.add_parser("register", parents=[common],
                                         help="register a completed Source Handoff")
    register_cmd.add_argument("job_id")
    register_cmd.add_argument("--handoff", required=True,
                              help="path to source.json handoff file")
    init_cmd = source_sub.add_parser(
        "init", parents=[common],
        help="generate handoff/source.json (from local path or template)")
    init_cmd.add_argument("job_id")
    init_cmd.add_argument("--remote-path", default="",
                          help="cloud path (baidu: full apps/bdpan/... path)")
    init_cmd.add_argument("--name", default=None, help="source display name")
    init_cmd.add_argument("--local-path", default=None,
                          help="downloaded local path; verified if given")
    init_cmd.add_argument("--note", dest="note", action="append", default=[],
                          help="source note (repeatable)")

    route = sub.add_parser("route", parents=[common],
                       help="classify source files (documents/media)")
    route.add_argument("job_id")
    route.add_argument("--exclude", dest="excludes", action="append", default=[],
                       metavar="PATH",
                       help="user-approved exclusion (repeatable)")

    preprocess = sub.add_parser(
        "preprocess", parents=[common],
        help="route -> media(when present) -> document set -> docchunk -> verify",
    )
    preprocess.add_argument("job_id")

    nxt = sub.add_parser("next", parents=[common], help="next action for this job")
    nxt.add_argument("job_id")
    nxt.add_argument("--json", dest="json_output", action="store_true",
                     default=True, help="emit JSON (default)")

    gate = sub.add_parser("gate", parents=[common], help="human confirmation gates")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_enter_cmd = gate_sub.add_parser("enter", parents=[common],
                                         help="record that a gate is now open")
    gate_enter_cmd.add_argument("job_id")
    gate_enter_cmd.add_argument("--target", required=True,
                                choices=target_choices())
    gate_enter_cmd.add_argument("--name", required=True)
    gate_resolve_cmd = gate_sub.add_parser(
        "resolve", parents=[common],
        help="record a REAL user decision (never fabricate)")
    gate_resolve_cmd.add_argument("job_id")
    gate_resolve_cmd.add_argument("--target", required=True,
                                  choices=target_choices())
    gate_resolve_cmd.add_argument("--name", required=True)
    gate_resolve_cmd.add_argument("--decision", required=True,
                                  choices=["confirmed", "rejected"])
    gate_resolve_cmd.add_argument(
        "--preauthorization", default=None, metavar="GRANT_ID",
        help="reference a preauthorization grant (pa_*) instead of a live "
             "answer; the granted value must not be repeated here")
    gate_preauthorize_cmd = gate_sub.add_parser(
        "preauthorize", parents=[common],
        help="grant a preauthorization for a preauthorizable gate (spec 10)")
    gate_preauthorize_cmd.add_argument("job_id")
    gate_preauthorize_cmd.add_argument("--target", required=True,
                                       choices=target_choices())
    gate_preauthorize_cmd.add_argument("--name", required=True)
    gate_preauthorize_cmd.add_argument("--value", required=True)

    budget = sub.add_parser("budget", parents=[common],
                            help="external-call budget guard (spec 9)")
    budget_sub = budget.add_subparsers(dest="budget_command", required=True)
    budget_acquire_cmd = budget_sub.add_parser(
        "acquire", parents=[common],
        help="phase 1: request an external-call permit (idempotent by "
             "request_id)")
    budget_acquire_cmd.add_argument("job_id")
    budget_acquire_cmd.add_argument("--target", required=True,
                                    choices=target_choices())
    budget_acquire_cmd.add_argument("--host", required=True)
    budget_acquire_cmd.add_argument("--case-id", dest="case_id",
                                    required=True)
    budget_acquire_cmd.add_argument("--request-id", dest="request_id",
                                    required=True)
    budget_outcome_cmd = budget_sub.add_parser(
        "outcome", parents=[common],
        help="phase 2: report a permit outcome (idempotent; conflicts exit 2)")
    budget_outcome_cmd.add_argument("job_id")
    budget_outcome_cmd.add_argument("--permit", required=True)
    budget_outcome_cmd.add_argument(
        "--result", required=True, choices=["success", "empty", "rate_limit"])
    budget_amend_cmd = budget_sub.add_parser(
        "amend", parents=[common],
        help="change budget limits only; never auto-restores BLOCKED state")
    budget_amend_cmd.add_argument("job_id")
    budget_amend_cmd.add_argument("--target", required=True,
                                  choices=target_choices())
    budget_amend_cmd.add_argument("--max-external-calls",
                                  dest="max_external_calls", type=int,
                                  default=None)
    budget_amend_cmd.add_argument("--max-retries-per-case",
                                  dest="max_retries_per_case", type=int,
                                  default=None)

    target = sub.add_parser("target", parents=[common], help="target skill operations")
    target_sub = target.add_subparsers(dest="target_command", required=True)
    target_start_cmd = target_sub.add_parser(
        "start", parents=[common],
        help="begin distillation for a target (requires dependencies COMPLETED)")
    target_start_cmd.add_argument("job_id")
    target_start_cmd.add_argument("--target", required=True,
                                  choices=target_choices())
    target_done = target_sub.add_parser(
        "complete", parents=[common],
        help="register a distillation target's final output")
    target_done.add_argument("job_id")
    target_done.add_argument("--target", required=True,
                             choices=target_choices())
    target_done.add_argument("--output-path", required=True)
    target_done.add_argument(
        "--pipeline-state", default=None,
        help="cangjie: path to books/<slug>/PIPELINE_STATE.md for resume")
    target_checkpoint_cmd = target_sub.add_parser(
        "checkpoint", parents=[common],
        help="record a transactional checkpoint (spec 15; no heartbeat)")
    target_checkpoint_cmd.add_argument("job_id")
    target_checkpoint_cmd.add_argument("--target", required=True,
                                       choices=target_choices())
    target_checkpoint_cmd.add_argument("--phase", required=True)
    target_checkpoint_cmd.add_argument("--checkpoint", default=None,
                                       help="path to the checkpoint artifact")
    target_checkpoint_cmd.add_argument("--evidence", dest="evidence",
                                       default=None,
                                       help="path to the evidence directory")
    target_resume_cmd = target_sub.add_parser(
        "resume", parents=[common],
        help="explicitly restore a BLOCKED target (spec 9 blocker 1)")
    target_resume_cmd.add_argument("job_id")
    target_resume_cmd.add_argument("--target", required=True,
                                   choices=target_choices())

    status = sub.add_parser("status", parents=[common],
                        help="human-readable job status")
    status.add_argument("job_id")
    status.add_argument("--json", dest="json_output", action="store_true",
                        help="emit machine-readable JSON")

    report = sub.add_parser("report", parents=[common],
                        help="render reports/final.md")
    report.add_argument("job_id")

    resume = sub.add_parser(
        "resume", parents=[common],
        help="list/continue resumable jobs (reboot-survival entry)")
    resume.add_argument("--job", default=None, help="limit to one job")
    resume.add_argument("--exec", dest="exec_run", action="store_true",
                        help="run preprocess on the first resumable job")

    wd = sub.add_parser(
        "watchdog", parents=[common],
        help="reboot watchdog (LaunchAgent) install/status/uninstall")
    wd_sub = wd.add_subparsers(dest="watchdog_command", required=True)
    for _action, _help in (
            ("install", "generate + load the reboot watchdog (idempotent)"),
            ("status", "report watchdog installation state"),
            ("uninstall", "unload and remove the watchdog (logs kept)")):
        wd_sub.add_parser(_action, parents=[common], help=_help)

    distill = sub.add_parser("distill", parents=[common],
                             help="distillation workspace operations")
    distill_sub = distill.add_subparsers(dest="distill_command", required=True)
    distill_prep = distill_sub.add_parser(
        "prepare", parents=[common],
        help="scaffold distill cwd + target handoff for a target")
    distill_prep.add_argument("job_id")
    distill_prep.add_argument("--target", required=True,
                              choices=target_choices())

    tg = sub.add_parser("telegram", parents=[common],
                        help="Telegram source operations (TG2: local state)")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_sources = tg_sub.add_parser("sources", parents=[common],
                                   help="whitelist source registry")
    tg_sources_sub = tg_sources.add_subparsers(
        dest="telegram_sources_command", required=True)
    tg_sources_sub.add_parser("list", parents=[common],
                              help="list configured sources")
    tg_show = tg_sources_sub.add_parser("show", parents=[common],
                                        help="show one source")
    tg_show.add_argument("source_id")
    tg_enable = tg_sources_sub.add_parser("enable", parents=[common],
                                          help="enable a source")
    tg_enable.add_argument("source_id")
    tg_disable = tg_sources_sub.add_parser("disable", parents=[common],
                                           help="disable a source")
    tg_disable.add_argument("source_id")
    tg_sub.add_parser("status", parents=[common],
                      help="read-only telegram state summary")
    tg_review = tg_sub.add_parser("review", parents=[common],
                                  help="human review queue")
    tg_review_sub = tg_review.add_subparsers(dest="telegram_review_command",
                                             required=True)
    tg_review_sub.add_parser("list", parents=[common],
                             help="list unresolved reviews")
    tg_resolve = tg_review_sub.add_parser("resolve", parents=[common],
                                          help="resolve a review")
    tg_resolve.add_argument("review_id", type=int)
    tg_resolve.add_argument("--decision", required=True,
                            choices=["KEEP", "SKIP", "DOWNLOAD_ONCE"])
    tg_sub.add_parser("auth", parents=[common],
                      help="interactive Telegram login (H1: user present)")
    tg_sub.add_parser("watch", parents=[common],
                      help="run the live watcher (long-running)")
    tg_download = tg_sub.add_parser(
        "download", parents=[common],
        help="download one PDF source item (explicit human trigger)")
    tg_download.add_argument("item_id")
    tg_handoff = tg_sub.add_parser(
        "handoff", parents=[common],
        help="hand one source item to knowledge-ingest (explicit, gated)")
    tg_handoff.add_argument("item_id")
    tg_dryrun = tg_sub.add_parser(
        "classify-dryrun", parents=[common],
        help="run the classifier channel over parked samples (read-only)")
    tg_dryrun.add_argument("--limit", type=int, default=None)
    tg_budget = tg_sub.add_parser(
        "budget", parents=[common],
        help="source AI budget ledger (show / reset)")
    tg_budget_sub = tg_budget.add_subparsers(dest="telegram_budget_command",
                                             required=True)
    tg_budget_sub.add_parser("show", parents=[common],
                             help="ledger counters + breaker state")
    tg_budget_reset = tg_budget_sub.add_parser(
        "reset", parents=[common],
        help="clear counters/breaker (recover a stuck classifier channel)")
    tg_budget_reset.add_argument("--source", default=None)
    tg_budget_reset.add_argument(
        "--kind", default=None,
        choices=["noise_classifier", "pdf_interest_classifier"])
    tg_sources_sub.add_parser("discover", parents=[common],
                              help="list visible dialogs")
    tg_add = tg_sources_sub.add_parser("add", parents=[common],
                                       help="add a whitelisted source")
    tg_add.add_argument("ref", help="@username or chat ref")
    tg_add.add_argument("--source-id", required=True,
                        help="stable local source id, e.g. tg_ai_explore")
    tg_add.add_argument("--display-name", default=None)

    return parser


def _cmd_doctor(config: AppConfig, args) -> int:
    checks = run_doctor(config)
    if args.json_output:
        summary = {"pass": 0, "warn": 0, "fail": 0}
        for check in checks:
            summary[check.status.lower()] += 1
        print(json.dumps(
            {"checks": [asdict(c) for c in checks], "summary": summary},
            ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check.status}] {check.name}: {check.detail}")
        print(f"total: {len(checks)} checks")
    return 1 if has_fail(checks) else 0


def _cmd_job_create(config: AppConfig, args) -> int:
    from knowledge_ingest.report import redact_text

    targets = list(dict.fromkeys(args.targets))
    with_router = getattr(args, "with_router", None)
    if with_router:
        # 规格 4：--with-router [family_router]——自动确保 router entry +
        # 其直接 depends_on entry（model validator 补建）；不自动启动、
        # 不递归补；依赖未注册 → fail-fast。
        try:
            runtime = get_target(with_router)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        unregistered = [dep for dep in runtime.depends_on
                        if dep not in target_choices()]
        if unregistered:
            print(f"error: cannot ensure dependencies for "
                  f"{runtime.name}: {unregistered}", file=sys.stderr)
            return 2
        if runtime.name not in targets:
            targets.append(runtime.name)
    request = JobRequest(
        # prompt 是自由文本，可能携带敏感串——落盘前脱敏（硬约束：Token 不入 Manifest）
        raw_prompt=redact_text(args.prompt or args.source),
        provider=args.provider,
        source=args.source,
        targets=targets,
    )
    manifest = _store(config).create(request)
    print(manifest.job_id)
    return 0


BAIDU_APP_PATH = re.compile(r"^/?apps/bdpan(/|$)")


def _telegram_provenance_error(handoff: dict) -> str | None:
    """TG1：telegram 的最小 provenance 契约（冻结设计 §13.4/§13.5）。

    身份三要素必须可读（platform / source_id / message_ids）；
    未知键一律容忍；message_url 等可选字段缺席不拒绝。
    """
    provenance = handoff.get("provenance")
    if not isinstance(provenance, dict):
        return "invalid_telegram_provenance"
    if provenance.get("platform") != "telegram":
        return "invalid_telegram_provenance"
    if not provenance.get("source_id"):
        return "invalid_telegram_provenance"
    message_ids = provenance.get("message_ids")
    if (not isinstance(message_ids, list) or not message_ids
            or not all(isinstance(i, int) and not isinstance(i, bool)
                       for i in message_ids)):
        return "invalid_telegram_provenance"
    return None


def _handoff_validation_error(manifest, handoff: dict) -> str | None:
    """Source Handoff 完成门：不完整/不存在/provider 不符一律拒绝。

    TG1：schema v1/v2 并存（v1=既有 local/baidu/quark，
    v2=telegram+provenance）；v3+ 仍 fail-fast。
    """
    if handoff.get("schema_version") not in (1, 2):
        return "invalid_handoff_schema"
    if handoff.get("download_completed") is not True:
        return "source_incomplete"
    if handoff.get("provider") != manifest.request.provider:
        return "provider_mismatch"
    local = handoff.get("local_path")
    if not local or not Path(str(local)).expanduser().exists():
        return "source_missing"
    if manifest.request.provider == "telegram":
        return _telegram_provenance_error(handoff)
    return None


def _cmd_source_register(config: AppConfig, args) -> int:
    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    handoff_path = Path(args.handoff).expanduser()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    # 事务式 mutate：read-modify-write 整体在 manifest 锁临界区内
    with store.edit(args.job_id) as manifest:
        manifest.source = handoff

        _advance(manifest, ["DISCOVERING"])
        validation_error = _handoff_validation_error(manifest, handoff)
        if validation_error:
            _block(manifest, validation_error)
            _log(config, manifest, "blocked", reason=validation_error)
            print(f"BLOCKED: {validation_error}")
            return 1

        fingerprint = _source_fingerprint(
            Path(handoff.get("local_path") or manifest.request.source))
        if fingerprint:
            manifest.source["source_fingerprint"] = fingerprint
        _log(config, manifest, "source_registered",
             provider=manifest.request.provider,
             fingerprint=fingerprint)

        remote = handoff.get("remote") or {}
        if manifest.request.provider == "baidu":
            remote_path = str(remote.get("path") or "")
            # 规范：remote.path 必须是应用目录全路径（/apps/bdpan/... 或 apps/bdpan/...）
            if not BAIDU_APP_PATH.match(remote_path):
                manifest.source["scope_violation"] = True
                _block(manifest, "baidu_scope_limited")
                _log(config, manifest, "blocked", reason="baidu_scope_limited")
                print(f"BLOCKED: baidu_scope_limited ({remote_path or '<empty>'})")
                return 1

        _advance(manifest, ["DOWNLOADING", "DOWNLOADED"])
        print(f"source registered: {handoff.get('local_path')}")
        print(f"status: {manifest.status}")
        return 0


def _cmd_route(config: AppConfig, args) -> int:
    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    # 事务式 mutate：read-modify-write 整体在 manifest 锁临界区内
    with store.edit(args.job_id) as manifest:
        local_path = Path(manifest.source.get("local_path") or
                          manifest.request.source)
        if not local_path.exists():
            print(f"error: source path missing: {local_path}", file=sys.stderr)
            return 2
        _advance(manifest, ["ROUTING"])
        result = route_source(local_path, excludes=args.excludes)
        manifest.routing = {
            "collection": result.is_collection,
            "discovered": {
                "documents": [str(p) for p in result.discovered_documents],
                "media": [str(p) for p in result.discovered_media],
                "unsupported": [str(p) for p in result.discovered_unsupported],
            },
            "excluded": {
                "documents": [str(p) for p in result.excluded_documents],
                "media": [str(p) for p in result.excluded_media],
                "unsupported": [str(p) for p in result.excluded_unsupported],
            },
            "effective": {
                "documents": [str(p) for p in result.documents],
                "media": [str(p) for p in result.media],
                "unsupported": [str(p) for p in result.unsupported],
            },
            "excluded_raw": list(args.excludes),
        }
        # v0.3 冻结规格 1：unsupported 检查先于编码预检（Part B 在此之后插入）
        if result.unsupported:
            manifest.routing["blocked_unsupported"] = [
                str(p) for p in result.unsupported]
            _block(manifest, "unsupported_source")
            _log(config, manifest, "routed",
                 effective_documents=len(result.documents),
                 effective_media=len(result.media),
                 effective_unsupported=len(result.unsupported),
                 excluded_documents=len(result.excluded_documents),
                 excluded_media=len(result.excluded_media),
                 excluded_unsupported=len(result.excluded_unsupported),
                 blocked_reason="unsupported_source")
            for p in result.unsupported:
                print(f"unsupported (exclude explicitly to continue): {p}")
            print("BLOCKED: unsupported_source")
            return 1
        # v0.3 冻结规格 14：流式全文件严格 UTF-8 预检（.txt/.md/.markdown）
        for doc in result.documents:
            if doc.suffix.lower() not in TEXT_SUFFIXES:
                continue
            detected = preflight_utf8(doc)
            if detected is not None:
                _block(manifest, "text_encoding_unsupported",
                       path=str(doc), detected_encoding=detected,
                       remediation="convert the file to UTF-8, "
                                   "then re-run route")
                store.save(manifest)
                _log(config, manifest, "blocked",
                     reason="text_encoding_unsupported", path=str(doc),
                     detected_encoding=detected)
                print(f"BLOCKED: text_encoding_unsupported ({doc}, "
                      f"detected_encoding={detected})")
                print("remediation: convert the file to UTF-8, "
                      "then re-run route")
                return 1
        _log(config, manifest, "routed",
             effective_documents=len(result.documents),
             effective_media=len(result.media),
             effective_unsupported=len(result.unsupported),
             excluded_documents=len(result.excluded_documents),
             excluded_media=len(result.excluded_media),
             excluded_unsupported=len(result.excluded_unsupported))
        print(
            f"routed: discovered documents={len(result.discovered_documents)} "
            f"media={len(result.discovered_media)} "
            f"unsupported={len(result.discovered_unsupported)}; "
            f"excluded documents={len(result.excluded_documents)} "
            f"media={len(result.excluded_media)} "
            f"unsupported={len(result.excluded_unsupported)}; "
            f"effective documents={len(result.documents)} "
            f"media={len(result.media)} collection={result.is_collection}"
        )
        print(f"status: {manifest.status}")
        return 0


@_locked
def _cmd_preprocess(config: AppConfig, args) -> int:
    from datetime import datetime

    from knowledge_ingest.adapters.docchunk import DocchunkAdapter
    from knowledge_ingest.adapters.media import MediaAdapter
    from knowledge_ingest.cache import (
        TranscriptCache,
        TranscriptCacheEntry,
        build_media_cache_key,
    )
    from knowledge_ingest.collection import (
        CollectionIncomplete,
        build_document_set,
        clear_handoff,
    )
    from knowledge_ingest.fingerprint import fingerprint_file
    from knowledge_ingest.models import MediaOutput
    from knowledge_ingest.state_machine import transition_to as tsm

    store = _store(config)
    manifest = _load_job(store, args.job_id)
    if manifest.status == "CORPUS_READY":
        print(f"already CORPUS_READY: {manifest.docchunk.corpus_path}")
        return 0
    if manifest.status not in {"ROUTING", "TRANSCRIBING", "DOCCHUNKING",
                               "VERIFYING"}:
        print(f"error: preprocess expects ROUTING (or interrupted "
              f"TRANSCRIBING/DOCCHUNKING/VERIFYING), got {manifest.status}",
              file=sys.stderr)
        return 2

    # v0.3 A2：run_id 贯穿本次 preprocess 的事件闭环
    # （started/finished 同 run_id；media_* 事件同 run_id；失败点先写 state 再 return）
    run_id = uuid.uuid4().hex
    state = {"outcome": "completed", "reason": ""}
    _log(config, manifest, "preprocess_started", run_id=run_id)
    try:
        routing = manifest.routing
        document_paths = effective_paths(routing, "documents")
        media_paths = effective_paths(routing, "media")
        if not document_paths and not media_paths:
            state["outcome"] = "failed"
            state["reason"] = "nothing_to_preprocess"
            print("error: nothing to preprocess (all inputs excluded?)",
                  file=sys.stderr)
            return 2

        job_dir = store.job_dir(manifest.job_id)
        handoff_dir = job_dir / "handoff" / "document-set"
        source_root = Path(manifest.source.get("local_path") or
                           manifest.request.source)

        transcripts: dict[str, str] = {}
        if media_paths:
            seen_stems: dict[str, Path] = {}
            for media in media_paths:
                stem = media.stem.lower()
                if stem in seen_stems:
                    # media-transcriber 产物按 stem 落盘，同名 stem 会互相覆盖/混淆
                    state["outcome"] = "failed"
                    state["reason"] = "media_stem_conflict"
                    _block(manifest, "media_stem_conflict")
                    store.save_section(manifest,
                                       ["media", "errors", "status"])
                    _log(config, manifest, "blocked",
                         reason="media_stem_conflict",
                         first=str(seen_stems[stem]), second=str(media))
                    print(f"BLOCKED: media_stem_conflict "
                          f"({seen_stems[stem]} vs {media})")
                    return 1
                seen_stems[stem] = media
            if manifest.status == "ROUTING":
                tsm(manifest, "TRANSCRIBING")
            manifest.media.status = "running"
            manifest.media.started_at = manifest.media.started_at or \
                datetime.now(UTC)
            manifest.media.outputs = []   # 重入时重建（缓存让重建零成本）
            store.save_section(manifest, ["status", "media"])

            media_adapter = MediaAdapter(
                project=config.media_project,
                output_root=config.media_output_root)
            cache = TranscriptCache(
                config.pipeline_root / "cache" / "transcript-index.json")
            mt_head = media_adapter.head()

            for media in media_paths:
                media_resolved = media.resolve()
                source_sha = fingerprint_file(media_resolved)
                try:
                    rel_name = media_resolved.relative_to(
                        source_root).as_posix()
                except (ValueError, OSError):
                    rel_name = media.name
                key = build_media_cache_key(
                    source_sha256=source_sha, mt_head=mt_head, config_sha=None,
                    device=config.processing.media_device,
                    timestamp=config.processing.media_timestamp,
                    glossary=None, hotwords=None)
                entry = cache.lookup(key)
                cache_hit = entry is not None
                duration_ms = 0
                if entry is None:
                    perf_t0 = time.perf_counter()
                    try:
                        result = media_adapter.transcribe(
                            media_resolved,
                            device=config.processing.media_device,
                            timestamp=config.processing.media_timestamp)
                    except (RuntimeError, OSError) as exc:
                        state["outcome"] = "failed"
                        state["reason"] = "media_failed"
                        manifest.media.status = "failed"
                        manifest.media.error = f"{media}: {exc}"
                        _log(config, manifest, "media_failed",
                             run_id=run_id, source=rel_name,
                             error=str(exc), attempt=1)
                        _block(manifest, "media_failed")
                        store.save_section(manifest,
                                           ["media", "errors", "status"])
                        print(f"BLOCKED: media_failed ({media})")
                        return 1
                    duration_ms = int((time.perf_counter() - perf_t0) * 1000)
                    entry = TranscriptCacheEntry(
                        cache_key=result.cache_key, source_path=media_resolved,
                        markdown_path=result.markdown_path,
                        metadata_path=result.metadata_path,
                        transcript_sha256=result.transcript_sha256)
                    cache.put(entry)
                transcripts[media_resolved.as_posix()] = \
                    str(entry.markdown_path)
                manifest.media.outputs.append(MediaOutput(
                    source_relative_path=rel_name,
                    source_sha256=source_sha,
                    transcript=entry.markdown_path,
                    transcript_sha256=entry.transcript_sha256,
                    metadata=entry.metadata_path,
                    cache_key=entry.cache_key,
                    outcome="cache_reused" if cache_hit else "transcribed",
                ))
                # 每文件即落盘：长 ASR 期间 status 可见真实进度，中断零丢失
                store.save_section(manifest, ["media"])
                _log(config, manifest, "media_output_ready",
                     run_id=run_id,
                     outcome="cache_reused" if cache_hit else "transcribed",
                     source=rel_name, source_sha256=source_sha,
                     cache_key=entry.cache_key, duration_ms=duration_ms,
                     attempt=1)
            manifest.media.status = "success"
            manifest.media.completed_at = datetime.now(UTC)

        if manifest.status in {"ROUTING", "TRANSCRIBING"}:
            tsm(manifest, "DOCCHUNKING")
        manifest.docchunk.status = "running"
        store.save_section(manifest, ["media", "status", "docchunk"])

        clear_handoff(handoff_dir)
        try:
            built = build_document_set(
                handoff_dir=handoff_dir, source_root=source_root,
                document_paths=document_paths, media_paths=media_paths,
                transcripts=transcripts)
        except CollectionIncomplete as exc:
            state["outcome"] = "failed"
            state["reason"] = "collection_incomplete"
            _block(manifest, "collection_incomplete")
            store.save_section(manifest, ["errors", "status"])
            _log(config, manifest, "blocked", reason="collection_incomplete",
                 detail=str(exc)[-300:])
            print(f"BLOCKED: collection_incomplete ({exc})")
            return 1
        except OSError as exc:
            state["outcome"] = "failed"
            state["reason"] = "collection_build_failed"
            _block(manifest, "collection_build_failed")
            store.save_section(manifest, ["errors", "status"])
            _log(config, manifest, "blocked", reason="collection_build_failed",
                 detail=str(exc)[-300:])
            print(f"BLOCKED: collection_build_failed ({exc})")
            return 1
        manifest.routing["document_set_map"] = str(built.map_path)
        store.save_section(manifest, ["routing"])

        docchunk_adapter = DocchunkAdapter(project=config.docchunk_project)
        from knowledge_ingest.adapters.docchunk import resolve_corpus_path
        from knowledge_ingest.cache import CorpusCache, build_corpus_cache_key
        from knowledge_ingest.fingerprint import fingerprint_collection
        from knowledge_ingest.runner import poll as poll_task

        try:
            handoff_fp = fingerprint_collection(handoff_dir)
            # 说明：此分量是本次调用的配置文件指纹（默认 no-config）；
            # docchunk 自身配置由 docchunk_revision（可编辑安装的 git HEAD）覆盖
            config_fp = (fingerprint_file(Path(args.config).expanduser())
                         if getattr(args, "config", None) else "no-config")
            corpus_cache = CorpusCache(
                config.pipeline_root / "cache" / "corpus-index.json")
            corpus_key = build_corpus_cache_key(
                handoff_fingerprint=handoff_fp.sha256,
                docchunk_revision=docchunk_adapter.head(),
                config_fingerprint=config_fp)
            manifest.docchunk.cache_key = corpus_key

            reused_corpus = corpus_cache.lookup(corpus_key,
                                                docchunk_adapter.verify)
            if reused_corpus is not None:
                corpus = reused_corpus
                manifest.docchunk.reused = True
                _log(config, manifest, "corpus_reused", corpus=str(corpus))
                print(f"corpus reused (verified): {corpus}")
            else:
                manifest.docchunk.reused = False
                _log(config, manifest, "docchunk_split_started")
                task = docchunk_adapter.split_task(
                    handoff_dir, log_path=job_dir / "logs"
                    / "docchunk-split.log")
                started = time.monotonic()
                poll_round = 0
                while True:
                    finished = poll_task(task)
                    if finished is not None:
                        break
                    poll_round += 1
                    if poll_round % 6 == 0:  # 每 30 秒记录一次进度事件
                        _log(config, manifest, "docchunk_progress",
                             elapsed_seconds=round(
                                 time.monotonic() - started))
                    time.sleep(5)
                if finished.returncode != 0:
                    raise RuntimeError(
                        f"docchunk split exited {finished.returncode}: "
                        f"{finished.stdout.strip()[-300:]}")
                corpus = resolve_corpus_path(finished)
                corpus_cache.put(corpus_key, corpus)
        except (RuntimeError, OSError) as exc:
            state["outcome"] = "failed"
            state["reason"] = "docchunk_split_failed"
            _block(manifest, "docchunk_split_failed")
            store.save_section(manifest, ["errors", "status"])
            _log(config, manifest, "blocked", reason="docchunk_split_failed",
                 detail=str(exc)[-300:])
            print(f"BLOCKED: docchunk_split_failed ({exc})")
            return 1

        if manifest.status != "VERIFYING":
            tsm(manifest, "VERIFYING")
        store.save_section(manifest, ["docchunk", "status"])
        verified = docchunk_adapter.verify(corpus)

        manifest.docchunk.corpus_path = corpus
        manifest.docchunk.verify = "PASS" if verified else "FAIL"
        manifest.docchunk.status = "verified" if verified else "failed"
        if verified:
            tsm(manifest, "CORPUS_READY")
            store.save_section(manifest, ["docchunk", "status"])
            _log(config, manifest, "corpus_verified", corpus=str(corpus),
                 reused=bool(manifest.docchunk.reused))
            print(f"corpus verified: {corpus}")
            print(f"status: {manifest.status}")
            return 0
        state["outcome"] = "failed"
        state["reason"] = "corpus_verify_failed"
        _log(config, manifest, "blocked", reason="corpus_verify_failed")
        _block(manifest, "corpus_verify_failed")
        store.save_section(manifest, ["docchunk", "errors", "status"])
        print("BLOCKED: corpus_verify_failed")
        return 1
    except BaseException as exc:
        # 未捕获异常 → interrupted + 异常摘要（SIGKILL 无法覆盖，不做承诺）
        state["outcome"] = "interrupted"
        state["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        _log(config, manifest, "preprocess_finished", run_id=run_id,
             outcome=state["outcome"], reason=state["reason"])


def _cmd_next(config: AppConfig, args) -> int:
    from knowledge_ingest.next_action import next_action

    store = _store(config)
    manifest = _load_job(store, args.job_id)
    print(json.dumps(next_action(manifest), ensure_ascii=False, indent=2))
    return 0


def _cmd_gate(config: AppConfig, args) -> int:
    from knowledge_ingest.next_action import (
        gate_enter,
        gate_preauthorize,
        gate_resolve,
    )

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    with store.edit(args.job_id) as manifest:
        if args.gate_command == "enter":
            gate_enter(manifest, args.target, args.name)
            print(f"WAITING_USER: {args.target}:{args.name}")
            return 0
        if args.gate_command == "preauthorize":
            grant = gate_preauthorize(manifest, args.target, args.name,
                                      args.value)
            print(f"preauthorized: {grant['grant_id']} "
                  f"({args.target}:{args.name})")
            return 0
        gate_resolve(manifest, args.target, args.name, args.decision,
                     preauthorization=args.preauthorization)
        print(f"gate resolved ({args.decision}): status={manifest.status}")
        return 0


def _cmd_target_complete(config: AppConfig, args) -> int:
    import os as _os

    from knowledge_ingest.next_action import target_complete

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    output_path = Path(args.output_path).expanduser().resolve()
    if not output_path.exists():
        print(f"error: output path does not exist: {output_path}",
              file=sys.stderr)
        return 2
    pipeline_state = (Path(args.pipeline_state).expanduser().resolve()
                      if args.pipeline_state else None)
    runtime = get_target(args.target)
    # 锁外：扫描产物 + 生成决定性 manifest 内容 → tmp
    # （锁内校验通过后才 rename，事务失败则 TargetState 不变）
    # Task 21/22：按 target dispatch —— cangjie 用既有 scanner，
    # k2c 用专属 scanner + 状态映射门；禁止互相套用。
    tmp_path: Path | None = None
    final_path: Path | None = None
    if runtime.output_manifest:
        handoff_dir = store.job_dir(args.job_id) / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        final_path = handoff_dir / runtime.output_manifest
        tmp_path = handoff_dir / f".{runtime.output_manifest}.tmp"
        if args.target == "k2c":
            from knowledge_ingest.k2c_output_manifest import (
                build_k2c_output_manifest,
                k2c_complete_blocked_reason,
                render_k2c_output_manifest,
            )

            k2c_manifest = build_k2c_output_manifest(output_path)
            blocked = k2c_complete_blocked_reason(k2c_manifest)
            if blocked is not None:
                print(f"error: {blocked}", file=sys.stderr)
                return 2
            tmp_path.write_text(
                render_k2c_output_manifest(k2c_manifest), encoding="utf-8")
        else:
            from knowledge_ingest.output_manifest import (
                build_output_manifest,
                render_output_manifest,
            )

            tmp_path.write_text(
                render_output_manifest(build_output_manifest(output_path)),
                encoding="utf-8")
    try:
        with store.edit(args.job_id) as manifest:
            # 先校验 target 仍允许 complete（锁内），再 rename，最后推进状态：
            # 任一步失败 → 事务放弃，TargetState 不变
            state = manifest.targets.get(args.target)
            if state is None or state.status != "RUNNING":
                raise ValueError(
                    f"target {args.target} not completable "
                    f"(status={getattr(state, 'status', None)})")
            if tmp_path is not None and final_path is not None:
                _os.replace(tmp_path, final_path)
            target_complete(manifest, args.target, output_path,
                            pipeline_state=pipeline_state,
                            output_manifest=final_path)
            print(f"{args.target} complete: {output_path}")
            print(f"status: {manifest.status}")
            return 0
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _cmd_target_checkpoint(config: AppConfig, args) -> int:
    from knowledge_ingest.next_action import target_checkpoint

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    checkpoint_path = (Path(args.checkpoint).expanduser()
                       if args.checkpoint else None)
    evidence_dir = (Path(args.evidence).expanduser()
                    if args.evidence else None)
    with store.edit(args.job_id) as manifest:
        record = target_checkpoint(
            manifest, args.target, args.phase,
            checkpoint_path=checkpoint_path, evidence_dir=evidence_dir)
        _log(config, manifest, "target_checkpointed", **record)
        print(f"checkpointed: {args.target} phase={args.phase} "
              f"attempt={record['attempt']}")
        return 0


def _cmd_target_resume(config: AppConfig, args) -> int:
    from knowledge_ingest.next_action import target_resume

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    with store.edit(args.job_id) as manifest:
        target_status = target_resume(manifest, args.target)
        print(f"{args.target} resumed: {target_status}")
        print(f"status: {manifest.status}")
        return 0


def _cmd_budget(config: AppConfig, args) -> int:
    import json as _json

    from knowledge_ingest import budget

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    job_dir = store.job_dir(args.job_id)
    if args.budget_command == "amend":
        # 规格 9 Blocker 1：修改预算 ≠ 自动恢复 BLOCKED
        with store.edit(args.job_id) as manifest:
            if args.target not in manifest.targets:
                raise ValueError(
                    f"target not present in this job: {args.target}")
            amended = budget.amend(
                job_dir, args.target,
                max_external_calls=args.max_external_calls,
                max_retries_per_case=args.max_retries_per_case)
        print(f"budget amended for {args.target}: "
              f"{_json.dumps(amended, ensure_ascii=False, sort_keys=True)}")
        print("note: budget amend never restores BLOCKED state — "
              "use 'target resume' (see references/recovery.md)")
        return 0
    with store.edit(args.job_id) as manifest:
        if args.budget_command == "acquire":
            if args.target not in manifest.targets:
                raise ValueError(
                    f"target not present in this job: {args.target}")
            result = budget.acquire(
                manifest, job_dir, target=args.target, host=args.host,
                case_id=args.case_id, request_id=args.request_id)
            if result["allowed"]:
                if not result.get("replay"):
                    _log(config, manifest, "external_call_acquired",
                         permit_id=result["permit_id"], target=args.target,
                         host=args.host, case_id=args.case_id)
                print(_json.dumps(
                    {"allowed": True, "permit_id": result["permit_id"],
                     "call_no": result["call_no"]}, ensure_ascii=False))
                return 0
            # 规格 9 Blocker 1 修订：budget acquire denied 时
            # target=BLOCKED、overall=BLOCKED、active_target=null
            # 整体必须迁移到 BLOCKED 状态，无论当前 overall 是什么
            # （CORPUS_READY 下的预检 BLOCKED 同样合理：某个 target
            # 预启动时被预算拒绝，Job 整体进入 BLOCKED 等用户处置）
            state = manifest.targets[args.target]
            state.status = "BLOCKED"
            state.reason = result["reason"]
            if manifest.active_target == args.target:
                manifest.active_target = None
            manifest.errors.append({
                "reason": result["reason"], "target": args.target,
                "scope": "budget",
            })
            if manifest.status not in {"COMPLETED", "PARTIAL", "FAILED",
                                        "BLOCKED"}:
                transition_to(manifest, "BLOCKED")
            _log(config, manifest, "blocked", reason=result["reason"],
                 target=args.target, scope="budget")
            print(_json.dumps({"allowed": False,
                               "reason": result["reason"]},
                              ensure_ascii=False))
            return 1
        # outcome
        result = budget.outcome(manifest, permit_id=args.permit,
                                result=args.result)
        if not result["noop"]:
            _log(config, manifest, "external_call_outcome",
                 permit_id=args.permit, result=args.result)
        print(_json.dumps(result, ensure_ascii=False))
        return 0


def _cmd_target_start(config: AppConfig, args) -> int:
    from knowledge_ingest.next_action import target_start

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    with store.edit(args.job_id) as manifest:
        target_start(manifest, args.target)
        print(f"{args.target} started: status={manifest.status}")
        return 0


RESUMABLE_STATUSES = {"ROUTING", "TRANSCRIBING", "DOCCHUNKING", "VERIFYING"}


def _cmd_resume(config: AppConfig, args) -> int:
    store = _store(config)
    job_ids = [args.job] if args.job else store.list_jobs()
    resumable: list[tuple[str, str]] = []
    for jid in job_ids:
        try:
            manifest = store.load(jid)
        except FileNotFoundError:
            if args.job:
                print(f"error: job not found: {jid}", file=sys.stderr)
                return 2
            continue
        if _lock_alive(store.job_dir(jid) / LOCK_NAME):
            print(f"{jid}: running (preprocess holds lock) — skip")
            continue
        status = manifest.status
        if status in RESUMABLE_STATUSES:
            resumable.append((jid, status))
        elif status in {"CREATED", "DISCOVERING", "DOWNLOADING"}:
            print(f"{jid}: {status} — needs cloud skill / source register "
                  f"(not auto-resumable)")
        elif status == "DOWNLOADED":
            print(f"{jid}: DOWNLOADED — run 'knowledge-ingest route {jid}'")
        elif status == "CORPUS_READY":
            print(f"{jid}: CORPUS_READY — run 'next --json' to invoke target")
        elif status == "WAITING_USER":
            waiting = [(name, state.waiting_for)
                       for name, state in manifest.targets.items()
                       if state.status == "WAITING_USER" and state.waiting_for]
            gate = (f"{waiting[0][0]}:{waiting[0][1]}"
                    if waiting else "?")
            print(f"{jid}: WAITING_USER ({gate}) — ask the user, "
                  f"then 'gate resolve'")
        elif status == "BLOCKED":
            reason = (manifest.errors[-1].get("reason")
                      if manifest.errors else "unknown")
            print(f"{jid}: BLOCKED ({reason}) — see references/recovery.md")
    for jid, status in resumable:
        print(f"resumable: {jid} ({status})")
    if not args.exec_run:
        return 0
    if not resumable:
        print("nothing to resume")
        return 0
    jid, status = resumable[0]
    print(f"resuming {jid} (was {status})")
    return _cmd_preprocess(config, argparse.Namespace(job_id=jid))


def _cmd_job_amend(config: AppConfig, args) -> int:
    from datetime import datetime

    from knowledge_ingest.next_action import propagate_dependencies

    store = _store(config)
    if not store.manifest_path(args.job_id).is_file():
        print(f"error: job not found: {args.job_id}", file=sys.stderr)
        return 2
    if _lock_alive(store.job_dir(args.job_id) / LOCK_NAME):
        print("error: preprocess is running and holds the manifest "
              "(memory copy would overwrite your change); "
              "retry after it exits", file=sys.stderr)
        return 2
    with store.edit(args.job_id) as manifest:
        if args.add_target not in manifest.request.targets:
            manifest.request.targets.append(args.add_target)
        # 新 target entry 及其直接依赖 entry（model validator 在 load 时运行，
        # append 后需显式补建）；再按依赖算 PENDING/READY + 依赖传播
        manifest.ensure_target_entries()
        propagate_dependencies(manifest)
        # 验收 E9：COMPLETED Job 上 amend，若存在新可执行 target →
        # 显式重入可调度态 CORPUS_READY（active_target=null；
        # 旧 COMPLETED target 保持 COMPLETED）
        ready = [name for name, state in manifest.targets.items()
                 if state.status == "READY"]
        if manifest.status == "COMPLETED" and ready:
            manifest.status = "CORPUS_READY"
            manifest.active_target = None
            manifest.gate_history.append({
                "ts": datetime.now(UTC).isoformat(),
                "action": "amend_reenter", "target": args.add_target,
            })
        print(f"targets: {manifest.request.targets}")
    return 0


def _cmd_source_init(config: AppConfig, args) -> int:
    store = _store(config)
    manifest = _load_job(store, args.job_id)
    remote_path = args.remote_path or ""
    name = args.name or (Path(remote_path).name if remote_path
                         else manifest.job_id)
    handoff = {
        "schema_version": 1,
        "provider": manifest.request.provider,
        "remote": {"id": None, "path": remote_path, "name": name,
                   "size_bytes": None, "mtime": None},
        "local_path": None,
        "download_completed": False,
        "source_notes": list(args.note or []),
    }
    if args.local_path:
        local = Path(args.local_path).expanduser().resolve()
        if not local.exists():
            print(f"error: local path does not exist: {local}",
                  file=sys.stderr)
            return 2
        handoff["local_path"] = str(local)
        handoff["download_completed"] = True
        fingerprint = _source_fingerprint(local)
        if fingerprint:
            handoff["source_fingerprint"] = fingerprint
    dest = store.job_dir(manifest.job_id) / "handoff" / "source.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(handoff, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"handoff written: {dest}")
    print(f"next: knowledge-ingest source register {manifest.job_id} "
          f"--handoff {dest}")
    return 0


def _cmd_distill_prepare(config: AppConfig, args) -> int:
    store = _store(config)
    manifest = _load_job(store, args.job_id)
    if manifest.status not in {"CORPUS_READY", "TARGET_RUNNING",
                               "WAITING_USER"}:
        print(f"error: distill prepare expects CORPUS_READY or later, "
              f"got {manifest.status}", file=sys.stderr)
        return 2
    runtime = get_target(args.target)
    target = runtime.name
    root = config.pipeline_root / "distill" / manifest.job_id / target
    root.mkdir(parents=True, exist_ok=True)
    for subdir in runtime.workspace_subdirs:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    state = root / "PIPELINE_STATE.md"
    if not state.exists():
        state.write_text(
            f"# PIPELINE_STATE — {manifest.job_id} / {target}\n\n"
            f"- job: {manifest.job_id}\n"
            f"- corpus: {manifest.docchunk.corpus_path}\n"
            f"- created: distill prepare (knowledge-ingest v0.3.0)\n",
            encoding="utf-8")
    handoff_path = (store.job_dir(manifest.job_id) / "handoff"
                    / f"target-{target}.yaml")
    if not handoff_path.exists():
        title = ((manifest.source.get("remote") or {}).get("name")
                 or Path(manifest.request.source).name)
        lines = [
            f"job_id: {manifest.job_id}",
            f"target: {target}",
            f"corpus_path: {manifest.docchunk.corpus_path}",
            "source:",
            f"  provider: {manifest.request.provider}",
            f"  title: {title}",
            "  author_or_speaker: null",
            "  publication_date: null",
            f"purpose: {manifest.request.raw_prompt}",
            (f"provenance_manifest: "
             f"{store.job_dir(manifest.job_id) / 'job.yaml'}"),
        ]
        if runtime.handoff_extra is not None:
            lines.extend(runtime.handoff_extra(
                manifest=manifest,
                job_dir=store.job_dir(manifest.job_id),
                corpus_path=manifest.docchunk.corpus_path))
        handoff_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"distill workspace: {root}")
    print(f"target handoff: {handoff_path}")
    return 0


def _cmd_status(config: AppConfig, args) -> int:
    from knowledge_ingest.report import build_status

    manifest = _load_job(_store(config), args.job_id)
    status = build_status(manifest)
    if args.json_output:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"Job            {status['job_id']}")
        print(f"Source         {status['source']}")
        print(f"Transcribe     {status['transcribe']}")
        print(f"DocChunk       {status['docchunk']}")
        for name, line in status["targets"].items():
            runtime = get_target(name)
            label = runtime.display_name if runtime else name
            print(f"{label:<15}{line}")
        print(f"Overall        {status['overall']}")
    return 0


def _cmd_report(config: AppConfig, args) -> int:
    from knowledge_ingest.report import write_report

    store = _store(config)
    manifest = _load_job(store, args.job_id)
    path = write_report(manifest, store.job_dir(manifest.job_id))
    print(path)
    return 0


# ---- TG2：telegram 本地状态入口（冻结设计 §6/§8/§16/§23） ----


def _telegram_db_path(config: AppConfig) -> Path:
    return config.pipeline_root / "telegram" / "state.db"


def _open_telegram_store(config: AppConfig, *, create: bool):
    """create=False 时读命令不得创建 state.db（方案 §4.6 只读约束）。"""
    db = _telegram_db_path(config)
    if not create and not db.exists():
        return None
    return TelegramEventStore(db)


def _cmd_telegram(config: AppConfig, args) -> int:
    if args.telegram_command == "auth":
        return _cmd_telegram_auth(config, args)
    if args.telegram_command == "watch":
        return _cmd_telegram_watch(config, args)
    if args.telegram_command == "status":
        return _cmd_telegram_status(config, args)
    if args.telegram_command == "download":
        return _cmd_telegram_download(config, args)
    if args.telegram_command == "handoff":
        return _cmd_telegram_handoff(config, args)
    if args.telegram_command == "classify-dryrun":
        return _cmd_telegram_classify_dryrun(config, args)
    if args.telegram_command == "budget":
        return _cmd_telegram_budget(config, args)
    if args.telegram_command == "sources":
        sub = args.telegram_sources_command
        if sub == "list":
            return _cmd_telegram_sources_list(config, args)
        if sub == "discover":
            return _cmd_telegram_sources_discover(config, args)
        if sub == "add":
            return _cmd_telegram_sources_add(config, args)
        store = _open_telegram_store(config, create=False)
        if store is None:
            print("error: telegram state not initialized",
                  file=sys.stderr)
            return 2
        if sub == "show":
            row = store.get_source(args.source_id)
            if row is None:
                print(f"error: source not found: {args.source_id}",
                      file=sys.stderr)
                return 2
            for key in ("source_id", "chat_id", "display_name", "enabled",
                        "start_at", "last_seen_message_id",
                        "last_reconciled_at"):
                print(f"{key}: {row[key]}")
            return 0
        if sub in ("enable", "disable"):
            try:
                store.set_source_enabled(args.source_id, sub == "enable")
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            state = "enabled" if sub == "enable" else "disabled"
            print(f"{args.source_id}: {state}")
            return 0
        print(f"unknown sources command: {sub}", file=sys.stderr)
        return 2
    if args.telegram_command == "review":
        sub = args.telegram_review_command
        if sub == "list":
            return _cmd_telegram_review_list(config, args)
        if sub == "resolve":
            return _cmd_telegram_review_resolve(config, args)
        print(f"unknown review command: {sub}", file=sys.stderr)
        return 2
    print(f"unknown telegram command: {args.telegram_command}",
          file=sys.stderr)
    return 2


def _cmd_telegram_sources_list(config: AppConfig, args) -> int:
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("telegram state not initialized (no sources added)")
        return 0
    rows = store.list_sources()
    if not rows:
        print("no telegram sources configured")
        return 0
    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        print(f"{row['source_id']}  {state}  "
              f"start_at={row['start_at']}  "
              f"last_seen={row['last_seen_message_id']}  "
              f"{row['display_name']}")
    return 0


def _render_telegram_status(store) -> int:
    summary = store.status_summary()
    print(f"schema: user_version={summary['schema_version']}")
    print(f"sources: enabled={summary['sources_enabled']} "
          f"disabled={summary['sources_disabled']}")
    for item in summary["sources_last_seen"]:
        reconciled = item["last_reconciled_at"] or "-"
        state = "on" if item["enabled"] else "off"
        print(f"last_seen: {item['source_id']} ({state}) "
              f"id={item['last_seen_message_id']} "
              f"reconciled={reconciled}")
    print(f"messages: {summary['messages_total']}")
    print(f"open_reviews: {summary['open_reviews']}")
    for reason, count in sorted(summary["reviews_by_reason"].items()):
        print(f"  review[{reason}]: {count}")
    if summary["oldest_open_review_at"]:
        print(f"oldest_open_review: {summary['oldest_open_review_at']}")
    print(f"pending_resources: {summary['pending_resources']}")
    downloads = summary["downloads_by_status"]
    print("downloads: " + (", ".join(
        f"{key}={value}" for key, value in sorted(downloads.items()))
        or "none"))
    handoffs = summary["recent_handoffs"]
    print("handoffs: " + (", ".join(
        f"{item['item_id']}->{item['job_id']}" for item in handoffs)
        or "none"))
    pending_once = summary["download_once_pending"]
    print("DOWNLOAD_ONCE pending: " + (", ".join(pending_once) or "none"))
    max_calls, breaker_empty, breaker_rate_limit = _tg_budget_limits()
    budgets = store.list_ai_budgets()
    rendered = []
    for row in budgets:
        calls = f"{row['calls']}/{max_calls}"
        if (row["calls"] >= max_calls
                or row["consecutive_empty"] >= breaker_empty
                or row["consecutive_rate_limit"] >= breaker_rate_limit):
            calls += "(breaker)"
        rendered.append(f"{row['source_id']}:{row['classifier_kind']}={calls}")
    print("classifier channel budget: " + (", ".join(rendered) or "unused"))
    return 0


def _cmd_telegram_status(config: AppConfig, args) -> int:
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("telegram state not initialized (no state.db)")
        return 0
    return _render_telegram_status(store)


def _cmd_telegram_review_list(config: AppConfig, args) -> int:
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("telegram state not initialized (no reviews)")
        return 0
    rows = store.list_open_reviews()
    if not rows:
        print("no open reviews")
        return 0
    for row in rows:
        size = row["size_bytes"]
        print(f"review={row['review_id']}  item={row['item_id']}  "
              f"reason={row['reason']}  kind={row['kind']}  "
              f"size={size if size is not None else '-'}  "
              f"created={row['created_at']}")
    return 0


def _cmd_telegram_review_resolve(config: AppConfig, args) -> int:
    from knowledge_ingest.telegram.items import SourceItemPipeline

    store = _open_telegram_store(config, create=False)
    if store is None:
        print("error: telegram state not initialized", file=sys.stderr)
        return 2
    try:
        outcome = store.resolve_review(args.review_id, args.decision)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"review {args.review_id}: {outcome} (decision={args.decision})")
    # R2：决定必须真的作用于 Item（KEEP→物化、SKIP→终态），否则人工
    # 队列就成了单向黑洞；失败（ORICO 掉线）时留下未消费，下轮重试
    pipeline = SourceItemPipeline(store, data_root=config.pipeline_root)
    for entry in pipeline.consume_review_decisions():
        print(f"applied: item={entry['item_id']} reason={entry['reason']} "
              f"decision={entry['decision']} → {entry['outcome']}")
    if args.decision == "DOWNLOAD_ONCE":
        print("download authorization recorded; consumed by the downloader "
              "(telegram download <item_id>, or the next reconcile)")
    return 0


# ---- TG3：认证 / 监听 / 来源发现（冻结设计 §5.5-§5.7，§7） ----


def _tg_session_dir() -> Path:
    return Path.home() / ".config" / "knowledge-ingest" / "telegram"


def _tg_repo_root() -> Path:
    import knowledge_ingest

    return Path(knowledge_ingest.__file__).resolve().parents[2]


def _tg_build_adapter(config: AppConfig):
    """从既有 session + credentials 构造 adapter；缺任一则提示 auth。"""
    from knowledge_ingest.telegram import auth as tg_auth
    from knowledge_ingest.telegram.telethon_adapter import TelethonAdapter

    session_dir = _tg_session_dir()
    if not tg_auth.has_session(session_dir):
        print("error: no telegram session; "
              "run: knowledge-ingest telegram auth", file=sys.stderr)
        return None
    try:
        credentials = tg_auth.load_credentials(
            session_dir / "credentials.env")
    except tg_auth.TelegramAuthRequiredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return TelethonAdapter(session_dir=session_dir, credentials=credentials)


def _cmd_telegram_auth(config: AppConfig, args, *, repo_root=None,
                       session_dir=None, signer=None) -> int:
    """H1 人工环节：交互式登录。Agent 不得代替用户输入凭据。"""
    from knowledge_ingest.telegram import auth as tg_auth
    from knowledge_ingest.telegram.client_port import (
        TelegramDependencyMissingError,
    )

    repo = Path(repo_root) if repo_root is not None else _tg_repo_root()
    directory = (Path(session_dir) if session_dir is not None
                 else _tg_session_dir())
    if signer is None:
        from knowledge_ingest.telegram.telethon_adapter import (
            TelethonAdapter,
        )

        def signer(directory: Path, credentials: dict) -> Path:
            adapter = TelethonAdapter(session_dir=directory,
                                      credentials=credentials)
            return adapter.interactive_sign_in()

    try:
        report = tg_auth.run_auth(repo_root=repo, session_dir=directory,
                                  signer=signer)
    except tg_auth.TelegramAuthRequiredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except TelegramDependencyMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"session: {report['session_path']}")
    print(f"session_exists: {report['session_exists']}")
    print(f"dir_mode_ok: {report['dir_mode_ok']}")
    print(f"file_mode_ok: {report['file_mode_ok']}")
    return 0


def _extract_json_text(raw: str) -> str | None:
    """从模型输出里取出第一个**完整** JSON 对象文本。

    容错模型爱加的代码围栏与前后寒暄；取不出来返回 None（调用方
    原样返回，交给 §21 判定为 unparseable_output → REVIEW，绝不猜
    内容、绝不把非 JSON 当结论）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _tg_budget_limits() -> tuple[int, int, int]:
    """分类通道的预算/熔断阈值（env 可调）。

    默认值沿用 API 分类器的口径；subprocess 通道（模型 CLI）的失败
    模式更直接，运维可按需放宽，避免两三次抖动就把通道熔断掉。
    """
    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid {name}: {raw!r}") from exc

    return (_int("KI_TELEGRAM_LLM_MAX_CALLS", 100),
            _int("KI_TELEGRAM_LLM_BREAKER_EMPTY", 3),
            _int("KI_TELEGRAM_LLM_BREAKER_RATE_LIMIT", 2))


def _tg_classifier_channel(store):
    """§7.6：分类模型通道——必须可注入，不写死任何 provider/SDK。

    运维通过环境变量指定一个命令行（prompt 走 stdin、结构化 JSON 走
    stdout），用于复用本机已有 worker/router：
        KI_TELEGRAM_LLM_CMD        例: "claude -p" / 自建 worker CLI
        KI_TELEGRAM_LLM_TIMEOUT    秒，默认 120
        KI_TELEGRAM_LLM_CWD        可选：模型 CLI 的工作目录（建议指向
                                   中性目录，避免加载项目上下文）
        KI_TELEGRAM_LLM_MAX_CALLS / _BREAKER_EMPTY / _BREAKER_RATE_LIMIT
                                   预算与熔断阈值（见 _tg_budget_limits）
        未配置 → (None, None)：规则路径照常，疑似内容进 REVIEW 等人。

    输出侧做一次 JSON 提取（代码围栏/寒暄容错）；提取不出来就原样
    返回，让严格解析器把它判成 unparseable_output（REVIEW）。
    """
    import shlex
    import subprocess

    command = (os.environ.get("KI_TELEGRAM_LLM_CMD") or "").strip()
    if not command:
        return None, None
    argv = shlex.split(command)
    if not argv:
        return None, None
    try:
        timeout = float(os.environ.get("KI_TELEGRAM_LLM_TIMEOUT", "120"))
    except ValueError as exc:
        raise ValueError(f"invalid KI_TELEGRAM_LLM_TIMEOUT: {exc}") from exc
    max_calls, breaker_empty, breaker_rate_limit = _tg_budget_limits()
    cwd = (os.environ.get("KI_TELEGRAM_LLM_CWD") or "").strip() or None

    def channel(prompt: str) -> str:
        proc = subprocess.run(argv, input=prompt, capture_output=True,
                              text=True, timeout=timeout, check=False,
                              cwd=cwd)
        if proc.returncode != 0:
            raise RuntimeError(
                f"classifier channel exit {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}")
        stdout = proc.stdout or ""
        return _extract_json_text(stdout) or stdout

    budget = AIBudgetGuard(store, max_calls=max_calls,
                           breaker_empty=breaker_empty,
                           breaker_rate_limit=breaker_rate_limit)
    return channel, budget


def _tg_watch_banner(*, handoff_enabled: bool, channel_enabled: bool) -> str:
    """启动横幅：如实报告 handoff 开关与分类通道状态（评审 R4）。"""
    handoff = ("ENABLED (KI_TELEGRAM_HANDOFF=1)" if handoff_enabled
               else "disabled")
    channel = ("env(KI_TELEGRAM_LLM_CMD)" if channel_enabled else "none")
    return "\n".join([
        f"telegram handoff scan: {handoff}",
        f"classifier channel: {channel}",
        ("telegram watcher running (Ctrl-C to stop); live updates + "
         "periodic reconcile + item pipeline"),
    ])


def _cmd_telegram_watch(config: AppConfig, args) -> int:
    from knowledge_ingest.telegram.watcher import TelegramWatcher

    adapter = _tg_build_adapter(config)
    if adapter is None:
        return 2
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("error: telegram state not initialized "
              "(run: telegram sources add first)", file=sys.stderr)
        return 2
    from knowledge_ingest.telegram.handoff import TelegramHandoffRunner
    from knowledge_ingest.telegram.items import SourceItemPipeline

    try:
        channel, budget = _tg_classifier_channel(store)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pipeline = SourceItemPipeline(store, data_root=config.pipeline_root,
                                  noise_llm=channel, interest_llm=channel,
                                  budget=budget)
    watcher = TelegramWatcher(store, client=adapter, pipeline=pipeline)
    handoff_scan = None
    if os.environ.get("KI_TELEGRAM_HANDOFF") == "1":
        # §8.2 交付编排：默认关闭——开启后 materialized/合格 item
        # 会自动创建 k2c job（真实 LLM 成本），由运维显式启用
        runner = TelegramHandoffRunner(store, config)
        handoff_scan = runner.scan
    # flush：launchd/nohup 下 stdout 是块缓冲，不刷新就看不到横幅
    print(_tg_watch_banner(handoff_enabled=handoff_scan is not None,
                           channel_enabled=channel is not None), flush=True)
    try:
        asyncio.run(watcher.run(handoff_scan=handoff_scan))
    except KeyboardInterrupt:
        print("watcher stopped", flush=True)
    return 0


def _cmd_telegram_download(config: AppConfig, args, *, client=None) -> int:
    """§6.7：人工显式触发一个 PDF Item 的下载（DOWNLOAD_ONCE 后/重试）。

    自动路径是 watcher 的周期 reconcile（≤50 MiB 的 INCLUDE PDF）；
    本命令用于人工授权后的 >50 MiB、或 5 次失败熔断后的显式重试。
    """
    from knowledge_ingest.telegram.items import SourceItemPipeline

    if client is None:
        client = _tg_build_adapter(config)
        if client is None:
            return 2
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("error: telegram state not initialized", file=sys.stderr)
        return 2
    item = store.get_source_item(args.item_id)
    if item is None:
        print(f"error: item not found: {args.item_id}", file=sys.stderr)
        return 2
    existing = store.get_download(args.item_id)
    if existing is not None and existing["status"] == "complete":
        print(f"already downloaded: {existing['local_path']}")
        return 0
    pipeline = SourceItemPipeline(store, data_root=config.pipeline_root)
    try:
        downloaded = asyncio.run(pipeline.download_pending_pdfs(
            client, item_id=args.item_id))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if downloaded:
        row = store.get_download(args.item_id)
        print(f"downloaded: {row['local_path']}  sha256={row['sha256']}")
        return 0
    # 不在这里调 download_gate 打印原因：它会消费授权（诊断不得改状态）
    row = store.get_download(args.item_id)
    print(f"no download performed (status={item['processing_status']}, "
          f"interest={item['interest_decision']}, "
          f"download={row['status'] if row else 'none'}/"
          f"{row['attempts'] if row else 0} attempts); "
          "see: knowledge-ingest telegram review list")
    return 1


def _cmd_telegram_classify_dryrun(config: AppConfig, args, *,
                                  channel=None) -> int:
    """用配置好的通道对现存 parked 样本试跑（只读：不写账本、不改状态）。

    开通道之前先看它对真实样本怎么判——合规率、判定分布、单次耗时；
    这一步零生产影响（预算不记账、Item 状态不动）。
    """
    from knowledge_ingest.telegram.classify import (
        classify_interest,
        classify_noise,
    )
    from knowledge_ingest.telegram.items import SourceItemPipeline

    store = _open_telegram_store(config, create=False)
    if store is None:
        print("telegram state not initialized (no state.db)")
        return 0
    if channel is None:
        channel, _budget = _tg_classifier_channel(store)
        if channel is None:
            print("error: no classifier channel configured "
                  "(set KI_TELEGRAM_LLM_CMD)", file=sys.stderr)
            return 2
    pipeline = SourceItemPipeline(store, data_root=config.pipeline_root)
    rows = [row for row in store.list_source_items()
            if row["processing_status"] in ("noise_review",
                                            "interest_review")]
    limit = getattr(args, "limit", None)
    if limit:
        rows = rows[:limit]
    if not rows:
        print("no parked items to dry-run")
        return 0
    decisions: dict[str, int] = {}
    elapsed_total = 0.0
    counted = 0
    for row in rows:
        started = time.monotonic()
        try:
            if row["kind"] == "pdf":
                message = store.get_message(row["source_id"],
                                            row["last_message_id"])
                source = store.get_source(row["source_id"])
                meta = {
                    "caption": message["text"] if message else None,
                    "filename": message["document_name"] if message else None,
                    "size_bytes": (message["document_size_bytes"]
                                   if message else None),
                    "source_display_name": (source["display_name"]
                                            if source else None),
                }
                decision = classify_interest(meta, llm=channel,
                                             source_id=row["source_id"])
                label, reason = decision.decision, decision.reason
            else:
                decision = classify_noise(pipeline.live_text(row["item_id"]),
                                          llm=channel,
                                          source_id=row["source_id"])
                label, reason = decision.decision, decision.reason_code
        except Exception as exc:            # noqa: BLE001 —— 试跑不中断
            print(f"{row['item_id']:20} ERROR  {exc}")
            continue
        elapsed = time.monotonic() - started
        elapsed_total += elapsed
        counted += 1
        decisions[label] = decisions.get(label, 0) + 1
        print(f"{row['item_id']:20} {label:8} conf={decision.confidence:<4} "
              f"{elapsed:5.1f}s  {str(reason)[:50]}")
    print(f"\ndry-run: {counted}/{len(rows)} item(s) classified, "
          f"{elapsed_total:.1f}s total, "
          f"mean {elapsed_total / max(counted, 1):.1f}s; "
          f"decisions={decisions or '{}'}")
    print("note: nothing was written (no budget, no item status change)")
    return 0


def _cmd_telegram_handoff(config: AppConfig, args) -> int:
    """§8.2：人工显式把一个 Source Item 交给 knowledge-ingest。

    自动扫描（KI_TELEGRAM_HANDOFF=1）之外的入口：由人挑定的样本走
    handoff → KI job（幂等：同一 item 绝不建第二个 Job），符合"人工
    门控、自动化止于 staged"。
    """
    from knowledge_ingest.telegram.handoff import TelegramHandoffRunner

    store = _open_telegram_store(config, create=False)
    if store is None:
        print("error: telegram state not initialized", file=sys.stderr)
        return 2
    item = store.get_source_item(args.item_id)
    if item is None:
        print(f"error: item not found: {args.item_id}", file=sys.stderr)
        return 2
    runner = TelegramHandoffRunner(store, config)
    already = item["knowledge_ingest_job_id"]
    result = runner.prepare_handoff(args.item_id)
    if result is None:
        reason = runner.gate(item)
        print(f"not handed off: {reason or 'unknown'}")
        return 1
    verb = "already handed off" if already else "handoff created"
    print(f"{verb}: item={result['item_id']} job={result['job_id']} "
          f"status={result['status']}")
    print(f"next: knowledge-ingest job show {result['job_id']}")
    return 0


def _cmd_telegram_budget(config: AppConfig, args) -> int:
    """§21.1 运维面：分类通道账本可见 + 可恢复（评审 R9）。"""
    store = _open_telegram_store(config, create=False)
    if store is None:
        print("telegram state not initialized (no state.db)")
        return 0
    if args.telegram_budget_command == "show":
        return _render_telegram_budget(store)
    source = getattr(args, "source", None)
    kind = getattr(args, "kind", None)
    removed = store.reset_ai_budget(source_id=source,
                                    classifier_kind=kind)
    scope = []
    if source:
        scope.append(f"source={source}")
    if kind:
        scope.append(f"kind={kind}")
    suffix = f" ({', '.join(scope)})" if scope else " (all)"
    print(f"budget ledger reset: {removed} row(s) removed{suffix}")
    if removed:
        print("channel is callable again; classification history stays in "
              "classifier_audit")
    return 0


def _render_telegram_budget(store) -> int:
    max_calls, breaker_empty, breaker_rate_limit = _tg_budget_limits()
    rows = store.list_ai_budgets()
    print(f"budget limits (this process env): max_calls={max_calls} "
          f"breaker_empty={breaker_empty} "
          f"breaker_rate_limit={breaker_rate_limit}")
    if not rows:
        print("budget: no ledger rows (channel never called)")
        return 0
    for row in rows:
        permits = len(json.loads(row["permits_json"] or "{}"))
        open_breakers = []
        if row["consecutive_empty"] >= breaker_empty:
            open_breakers.append("empty")
        if row["consecutive_rate_limit"] >= breaker_rate_limit:
            open_breakers.append("rate_limit")
        if row["calls"] >= max_calls:
            open_breakers.append("exhausted")
        state = "OPEN:" + ",".join(open_breakers) if open_breakers \
            else "closed"
        print(f"budget: {row['source_id']}:{row['classifier_kind']} "
              f"calls={row['calls']}/{max_calls} "
              f"attempts={row['attempts']} permits={permits} "
              f"empty={row['consecutive_empty']} "
              f"rate_limit={row['consecutive_rate_limit']} "
              f"breaker={state}")
    return 0


def _cmd_telegram_sources_discover(config: AppConfig, args) -> int:
    adapter = _tg_build_adapter(config)
    if adapter is None:
        return 2
    dialogs = asyncio.run(adapter.list_dialogs())
    if not dialogs:
        print("no dialogs visible")
        return 0
    for dialog in dialogs:
        username = f"@{dialog.username}" if dialog.username else "-"
        print(f"{dialog.chat_id}  {username}  {dialog.title}")
    return 0


def _cmd_telegram_sources_add(config: AppConfig, args, *, client=None):
    """§5.6：首次 add 即写 start_at（当前时刻）+ last_seen（当前边界），
    绝不导入历史消息。client 参数供测试注入 fake。"""
    from knowledge_ingest.telegram.client_port import (
        TelegramFloodWaitError,
        TelegramRPCError,
    )

    if client is None:
        client = _tg_build_adapter(config)
        if client is None:
            return 2

    async def flow():
        store = TelegramEventStore(_telegram_db_path(config))
        dialog = await client.resolve_chat(args.ref)
        latest = await client.latest_message(dialog.chat_id)
        last_seen = latest.message_id if latest is not None else 0
        register_source(store, args.source_id, chat_id=dialog.chat_id,
                        display_name=args.display_name or dialog.title)
        store.touch_source_cursor(args.source_id,
                                  last_seen_message_id=last_seen)
        print(f"source added: {args.source_id}  "
              f"chat_id={dialog.chat_id}  last_seen={last_seen}  "
              f"(start_at=now; no historical import)")

    try:
        asyncio.run(flow())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (TelegramFloodWaitError, TelegramRPCError) as exc:
        print(f"error: telegram call failed: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = AppConfig.load(
            None if args.config is None else Path(args.config).expanduser())
    except (FileNotFoundError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "doctor":
            return _cmd_doctor(config, args)
        if args.command == "telegram":
            return _cmd_telegram(config, args)
        if args.command == "job" and args.job_command == "create":
            return _cmd_job_create(config, args)
        if args.command == "source" and args.source_command == "register":
            return _cmd_source_register(config, args)
        if args.command == "route":
            return _cmd_route(config, args)
        if args.command == "preprocess":
            return _cmd_preprocess(config, args)
        if args.command == "next":
            return _cmd_next(config, args)
        if args.command == "gate":
            return _cmd_gate(config, args)
        if args.command == "budget":
            return _cmd_budget(config, args)
        if args.command == "target" and args.target_command == "start":
            return _cmd_target_start(config, args)
        if args.command == "target" and args.target_command == "complete":
            return _cmd_target_complete(config, args)
        if args.command == "target" and args.target_command == "checkpoint":
            return _cmd_target_checkpoint(config, args)
        if args.command == "target" and args.target_command == "resume":
            return _cmd_target_resume(config, args)
        if args.command == "status":
            return _cmd_status(config, args)
        if args.command == "report":
            return _cmd_report(config, args)
        if args.command == "resume":
            return _cmd_resume(config, args)
        if args.command == "watchdog":
            from knowledge_ingest import watchdog

            return getattr(watchdog, args.watchdog_command)(config)
        if args.command == "job" and args.job_command == "amend":
            return _cmd_job_amend(config, args)
        if args.command == "source" and args.source_command == "init":
            return _cmd_source_init(config, args)
        if args.command == "distill" and args.distill_command == "prepare":
            return _cmd_distill_prepare(config, args)
    except (InvalidTransition, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
