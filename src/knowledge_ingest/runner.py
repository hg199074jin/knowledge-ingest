"""Safe subprocess execution: no shell, redacted logs, long-task support."""

from __future__ import annotations

import hashlib
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


def _file_sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def worktree_revision(project: Path, *, label: str = "git",
                      timeout: int = 60) -> str:
    """git 工作区修订（v0.3 冻结规格 11）：clean → 纯 HEAD；dirty → HEAD-dirty-<fp>。

    worktree_fingerprint = sha256( `git diff --binary HEAD` 输出
        + sorted( untracked_relative_path + sha256(untracked_file_bytes) ) )，
    untracked 由 `git ls-files --others --exclude-standard` 发现。
    clean 时返回值与 v0.2 的纯 HEAD 完全一致（缓存零失效）；
    dirty 时键不同 → 自然 miss 重算，禁止任何 legacy fallback。
    """
    project = Path(project)
    head = run_checked(["git", "rev-parse", "HEAD"], cwd=project,
                       timeout=timeout)
    if head.returncode != 0:
        raise RuntimeError(f"cannot resolve {label} git HEAD")
    head_sha = head.stdout.strip()
    diff = run_checked(["git", "diff", "--binary", "HEAD"], cwd=project,
                       timeout=timeout)
    if diff.returncode != 0:
        raise RuntimeError(f"cannot diff {label} worktree against HEAD")
    others = run_checked(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=project, timeout=timeout)
    if others.returncode != 0:
        raise RuntimeError(f"cannot list {label} untracked files")
    untracked = sorted(
        line for line in others.stdout.splitlines() if line.strip())
    if not diff.stdout and not untracked:
        return head_sha
    digest = hashlib.sha256()
    digest.update(diff.stdout.encode("utf-8"))
    for rel in untracked:
        digest.update(rel.encode("utf-8"))
        digest.update(b"+")
        digest.update(_file_sha256_hex(project / rel).encode("utf-8"))
        digest.update(b"\n")
    return f"HEAD-dirty-{digest.hexdigest()}"
