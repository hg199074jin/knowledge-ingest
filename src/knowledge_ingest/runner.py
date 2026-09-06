"""Safe subprocess execution: no shell, redacted logs, long-task support."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SECRET_FLAG = re.compile(
    r"token|cookie|authorization|auth[-_]?code|password|secret",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    started_at: datetime
    ended_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def safe_argv(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Drop values following credential-ish flags; never log secrets."""
    items = list(argv)
    kept: list[str] = []
    skip_next = False
    for item in items:
        if skip_next:
            kept.append("[REDACTED]")
            skip_next = False
            continue
        kept.append(item)
        if SECRET_FLAG.search(item) and not item.startswith("-"):
            continue
        if item.startswith("--") and "=" in item:
            flag, _, value = item.partition("=")
            if SECRET_FLAG.search(flag):
                kept[-1] = f"{flag}=[REDACTED]"
                continue
        if SECRET_FLAG.search(item) and item.startswith("--"):
            skip_next = True
    return tuple(kept)


def run_checked(
    argv: list[str],
    cwd: Path,
    timeout: int | None = None,
) -> CommandResult:
    """Run argv without shell; nonzero exits are returned, not raised."""
    started = _now()
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        shell=False,
    )
    return CommandResult(
        argv=safe_argv(argv),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        started_at=started,
        ended_at=_now(),
    )


@dataclass
class RunningTask:
    argv: tuple[str, ...]
    cwd: Path
    log_path: Path
    popen: subprocess.Popen
    started_at: datetime


def spawn(argv: list[str], cwd: Path, log_path: Path) -> RunningTask:
    """Start a long task (e.g. docchunk split via MinerU) without blocking.

    The caller polls with poll(task); combined stdout/stderr goes to log_path.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    log_file.write(f"$ {safe_argv(argv)}\ncwd={cwd}\n")
    log_file.flush()
    popen = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    return RunningTask(
        argv=safe_argv(argv), cwd=Path(cwd), log_path=log_path,
        popen=popen, started_at=_now(),
    )


def poll(task: RunningTask) -> CommandResult | None:
    """Return CommandResult when finished, None while still running."""
    returncode = task.popen.poll()
    if returncode is None:
        return None
    try:
        text = task.log_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return CommandResult(
        argv=task.argv,
        returncode=returncode,
        stdout=text,
        stderr="",
        started_at=task.started_at,
        ended_at=_now(),
    )
