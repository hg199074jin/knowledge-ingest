"""knowledge-ingest command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.doctor import has_fail, run_doctor
from knowledge_ingest.manifest_store import ManifestStore
from knowledge_ingest.models import JobRequest
from knowledge_ingest.router import route_source
from knowledge_ingest.state_machine import InvalidTransition, transition_to

BAIDU_APP_PREFIXES = ("apps/bdpan", "/apps/bdpan")


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


def _block(manifest, reason: str) -> None:
    manifest.errors.append({"reason": reason})
    transition_to(manifest, "BLOCKED")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-ingest",
        description="Multi-source knowledge ingestion orchestrator",
    )
    parser.add_argument("--config", default=None, help="path to config YAML")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify machine baseline")
    doctor.add_argument("--json", dest="json_output", action="store_true",
                        help="emit machine-readable JSON")

    create = sub.add_parser("job", help="job operations")
    job_sub = create.add_subparsers(dest="job_command", required=True)
    create_cmd = job_sub.add_parser("create", help="create a new ingest job")
    create_cmd.add_argument("--provider", required=True,
                            choices=["local", "baidu", "quark"])
    create_cmd.add_argument("--source", required=True,
                            help="cloud path, share link, or local path")
    create_cmd.add_argument("--target", dest="targets", action="append",
                            required=True, choices=["cangjie", "personal"])
    create_cmd.add_argument("--prompt", default="",
                            help="raw user request text for provenance")

    register = sub.add_parser("source", help="source operations")
    source_sub = register.add_subparsers(dest="source_command", required=True)
    register_cmd = source_sub.add_parser("register",
                                         help="register a completed Source Handoff")
    register_cmd.add_argument("job_id")
    register_cmd.add_argument("--handoff", required=True,
                              help="path to source.json handoff file")

    route = sub.add_parser("route", help="classify source files (documents/media)")
    route.add_argument("job_id")
    route.add_argument("--exclude", dest="excludes", action="append", default=[],
                       metavar="PATH",
                       help="user-approved exclusion (repeatable)")

    preprocess = sub.add_parser(
        "preprocess",
        help="route -> media(when present) -> document set -> docchunk -> verify",
    )
    preprocess.add_argument("job_id")

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
    request = JobRequest(
        raw_prompt=args.prompt or args.source,
        provider=args.provider,
        source=args.source,
        targets=list(args.targets),
    )
    manifest = _store(config).create(request)
    print(manifest.job_id)
    return 0


def _cmd_source_register(config: AppConfig, args) -> int:
    store = _store(config)
    manifest = _load_job(store, args.job_id)
    handoff_path = Path(args.handoff).expanduser()
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    manifest.source = handoff

    remote = handoff.get("remote") or {}
    if manifest.request.provider == "baidu":
        remote_path = str(remote.get("path") or "")
        if not remote_path.startswith(BAIDU_APP_PREFIXES):
            manifest.source["scope_violation"] = True
            _advance(manifest, ["DISCOVERING"])
            _block(manifest, "baidu_scope_limited")
            store.save(manifest)
            print(f"BLOCKED: baidu_scope_limited ({remote_path or '<empty>'})")
            return 1

    _advance(manifest, ["DISCOVERING", "DOWNLOADING", "DOWNLOADED"])
    store.save(manifest)
    print(f"source registered: {handoff.get('local_path')}")
    print(f"status: {manifest.status}")
    return 0


def _cmd_route(config: AppConfig, args) -> int:
    store = _store(config)
    manifest = _load_job(store, args.job_id)
    local_path = Path(manifest.source.get("local_path") or
                      manifest.request.source)
    if not local_path.exists():
        print(f"error: source path missing: {local_path}", file=sys.stderr)
        return 2
    _advance(manifest, ["ROUTING"])
    result = route_source(local_path)
    manifest.routing = {
        "collection": result.is_collection,
        "documents": len(result.documents),
        "media": len(result.media),
        "unsupported": len(result.unsupported),
        "document_paths": [str(p) for p in result.documents],
        "media_paths": [str(p) for p in result.media],
        "unsupported_paths": [str(p) for p in result.unsupported],
        "excluded": list(args.excludes),
    }
    excluded = {Path(p).expanduser().resolve() for p in args.excludes}
    remaining_unsupported = [
        p for p in result.unsupported if p not in excluded
    ]
    if remaining_unsupported:
        manifest.routing["blocked_unsupported"] = [
            str(p) for p in remaining_unsupported]
        _block(manifest, "unsupported_inputs")
        store.save(manifest)
        for p in remaining_unsupported:
            print(f"unsupported (exclude explicitly to continue): {p}")
        print("BLOCKED: unsupported_inputs")
        return 1
    store.save(manifest)
    print(
        f"routed: documents={len(result.documents)} media={len(result.media)} "
        f"unsupported={len(result.unsupported)} collection={result.is_collection}"
    )
    print(f"status: {manifest.status}")
    return 0


def _cmd_preprocess(config: AppConfig, args) -> int:
    from datetime import datetime, timezone

    from knowledge_ingest.adapters.docchunk import DocchunkAdapter
    from knowledge_ingest.adapters.media import MediaAdapter
    from knowledge_ingest.cache import (
        TranscriptCache,
        TranscriptCacheEntry,
        build_transcript_cache_key,
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
    if manifest.status not in {"ROUTING", "DOCCHUNKING", "VERIFYING"}:
        print(f"error: preprocess expects ROUTING (or interrupted "
              f"DOCCHUNKING/VERIFYING), got {manifest.status}", file=sys.stderr)
        return 2

    routing = manifest.routing
    excluded = {str(Path(p).expanduser().resolve()) for p in
                (routing.get("excluded") or [])}
    document_paths = [Path(p) for p in routing.get("document_paths") or []]
    media_paths = [Path(p) for p in routing.get("media_paths") or []]
    if not document_paths and not media_paths:
        print("error: nothing to preprocess (all inputs excluded?)",
              file=sys.stderr)
        return 2

    job_dir = store.job_dir(manifest.job_id)
    handoff_dir = job_dir / "handoff" / "document-set"

    transcripts: dict[str, str] = {}
    if media_paths:
        tsm(manifest, "TRANSCRIBING")
        manifest.media.status = "running"
        manifest.media.started_at = datetime.now(timezone.utc)
        store.save(manifest)

        media_adapter = MediaAdapter(
            project=config.media_project, output_root=config.media_output_root)
        cache = TranscriptCache(
            config.pipeline_root / "cache" / "transcript-index.json")
        mt_head = media_adapter.head()

        for media in media_paths:
            media_resolved = media.resolve()
            if media_resolved.as_posix() in excluded:
                continue
            source_sha = fingerprint_file(media_resolved)
            key = build_transcript_cache_key(
                source_sha256=source_sha, mt_head=mt_head, config_sha=None,
                device=config.processing.media_device,
                timestamp=config.processing.media_timestamp,
                glossary=None, hotwords=None)
            entry = cache.lookup(key)
            if entry is None:
                try:
                    result = media_adapter.transcribe(
                        media_resolved,
                        device=config.processing.media_device,
                        timestamp=config.processing.media_timestamp)
                except (RuntimeError, OSError) as exc:
                    manifest.media.status = "failed"
                    manifest.media.error = f"{media}: {exc}"
                    _block(manifest, "media_failed")
                    store.save(manifest)
                    print(f"BLOCKED: media_failed ({media})")
                    return 1
                entry = TranscriptCacheEntry(
                    cache_key=result.cache_key, source_path=media_resolved,
                    markdown_path=result.markdown_path,
                    metadata_path=result.metadata_path,
                    transcript_sha256=result.transcript_sha256)
                cache.put(entry)
            transcripts[media_resolved.as_posix()] = str(entry.markdown_path)
            manifest.media.outputs.append(MediaOutput(
                source_relative_path=media.name,
                source_sha256=source_sha,
                transcript=entry.markdown_path,
                transcript_sha256=entry.transcript_sha256,
                metadata=entry.metadata_path,
                cache_key=entry.cache_key,
            ))
        manifest.media.status = "success"
        manifest.media.completed_at = datetime.now(timezone.utc)

    if manifest.status == "ROUTING":
        tsm(manifest, "DOCCHUNKING")
    manifest.docchunk.status = "running"
    store.save(manifest)

    source_root = Path(manifest.source.get("local_path") or
                       manifest.request.source)
    clear_handoff(handoff_dir)
    try:
        built = build_document_set(
            handoff_dir=handoff_dir, source_root=source_root,
            document_paths=document_paths, media_paths=media_paths,
            transcripts=transcripts, excluded=excluded)
    except CollectionIncomplete as exc:
        _block(manifest, "collection_incomplete")
        store.save(manifest)
        print(f"BLOCKED: collection_incomplete ({exc})")
        return 1
    manifest.routing["document_set_map"] = str(built.map_path)
    store.save(manifest)

    docchunk_adapter = DocchunkAdapter(project=config.docchunk_project)
    corpus = docchunk_adapter.split(handoff_dir)

    if manifest.status != "VERIFYING":
        tsm(manifest, "VERIFYING")
    store.save(manifest)
    verified = docchunk_adapter.verify(corpus)

    manifest.docchunk.corpus_path = corpus
    manifest.docchunk.verify = "PASS" if verified else "FAIL"
    manifest.docchunk.status = "verified" if verified else "failed"
    if verified:
        tsm(manifest, "CORPUS_READY")
        store.save(manifest)
        print(f"corpus verified: {corpus}")
        print(f"status: {manifest.status}")
        return 0
    _block(manifest, "corpus_verify_failed")
    store.save(manifest)
    print("BLOCKED: corpus_verify_failed")
    return 1


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
        if args.command == "job" and args.job_command == "create":
            return _cmd_job_create(config, args)
        if args.command == "source" and args.source_command == "register":
            return _cmd_source_register(config, args)
        if args.command == "route":
            return _cmd_route(config, args)
        if args.command == "preprocess":
            return _cmd_preprocess(config, args)
    except InvalidTransition as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
