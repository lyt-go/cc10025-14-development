"""统一时钟：业务判断（到期、豁免生效、执行时间）都从这里取时间。

默认走系统 UTC 时间；测试或演练可以用 `use_clock()` 固定到某个时刻，
让「10 点提交、10 点半新增豁免」这类时序可以被稳定复现。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator

Clock = Callable[[], datetime]

_override: Clock | None = None


def now() -> datetime:
    """当前时刻（UTC，带时区）。"""
    current = _override() if _override is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def now_iso() -> str:
    return now().isoformat()


def today() -> str:
    return now().strftime("%Y-%m-%d")


@contextmanager
def use_clock(clock: Clock | None) -> Iterator[None]:
    """临时替换时钟；传 None 表示恢复系统时间。"""
    global _override
    previous = _override
    _override = clock
    try:
        yield
    finally:
        _override = previous


def fixed_clock(moment: datetime) -> Clock:
    """返回一个固定时刻的时钟。"""
    frozen = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return lambda: frozen
