"""TG2: maintenance lock — 多步写入专用互斥（冻结设计 §8.4）。

适用：doctor --fix / retention cleanup / schema migration / 批量 repair。
拿不到锁宁可拒绝操作，也不与 watcher 同时改写事实层。
flock 风格与仓库既有 cache.py 纪律一致；不引入分布式锁。
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class MaintenanceLockBusy(RuntimeError):
    """maintenance lock 已被其他进程持有。"""


@contextmanager
def maintenance_lock(db_path: str | Path) -> Iterator[None]:
    """对给定 state.db 的旁路锁文件加非阻塞排它锁。"""
    lock_path = Path(str(db_path) + ".maintenance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceLockBusy(
                f"maintenance lock busy: {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
