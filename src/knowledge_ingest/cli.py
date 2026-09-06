"""knowledge-ingest command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from knowledge_ingest.config import AppConfig
from knowledge_ingest.doctor import has_fail, run_doctor


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge-ingest",
        description="Multi-source knowledge ingestion orchestrator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify machine baseline")
    doctor.add_argument("--config", default=None, help="path to config YAML")
    doctor.add_argument("--json", dest="json_output", action="store_true",
                        help="emit machine-readable JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        try:
            config = AppConfig.load(
                None if args.config is None else Path(args.config).expanduser())
        except (FileNotFoundError, OSError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
