"""按 umo + user_id 的冷却与每日限额（asyncio.Lock 保护）。

契约
----
- RateLimiter.check_and_consume(key) -> (allowed, message)：
  允许时消费一次并返回 (True, None)；拒绝时返回 (False, 可读中文提示)；
- cfg.cooldown_seconds=0 表示不限冷却，cfg.daily_limit=0 表示不限次数；
- 每日计数按日期清理，冷却状态过期后清理；达到 key 上限时暂拒新 key，
  保留有效限额并保证长时间运行内存有界；
- reset() 清空全部状态（测试用）；
- 时钟与日期可注入（clock / today），单测不需要真的等待。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import ensure_runtime_config
from .logging_utils import get_logger

logger = get_logger("ratelimit")

__all__ = [
    "DEFAULT_MAX_KEYS",
    "RateLimiter",
    "build_key",
    "cooldown_message",
    "daily_limit_message",
]

DEFAULT_MAX_KEYS = 4096
"""最多保留多少个有效限额 key（容量不足时暂拒新 key）。"""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def build_key(umo: Any, user_id: Any = "") -> str:
    """把 unified_msg_origin 与 user_id 拼成限额键（空值给稳定结果）。"""
    left = str(umo or "").strip()
    right = str(user_id or "").strip()
    if left and right:
        return f"{left}:{right}"
    return left or right


def cooldown_message(seconds: float) -> str:
    """冷却提示（中文、可读、带剩余秒数）。"""
    remaining = max(1, math.ceil(seconds))
    return f"点歌太频繁啦，请等 {remaining} 秒后再试～"


def daily_limit_message(limit: int) -> str:
    """每日限额提示。"""
    return f"今天的点歌次数已用完（上限 {limit} 次），明天再来吧～"


@dataclass
class RateLimitState:
    """一次判定的结构化结果（日志/调试用）。"""

    allowed: bool
    key: str = ""
    reason: str = ""
    message: str | None = None
    retry_after: float = 0.0
    used_today: int = 0
    remaining_today: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "key": self.key,
            "reason": self.reason,
            "message": self.message,
            "retry_after": round(self.retry_after, 3),
            "used_today": self.used_today,
            "remaining_today": self.remaining_today,
        }


@dataclass
class RateLimiter:
    """按 key 的冷却 + 每日限额（并发安全）。"""

    cooldown_seconds: int = 0
    daily_limit: int = 0
    max_keys: int = DEFAULT_MAX_KEYS
    clock: Callable[[], float] = time.monotonic
    today: Callable[[], str] = field(
        default_factory=lambda: (lambda: datetime.now().strftime("%Y-%m-%d"))
    )
    _last_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _daily: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _day: str = field(default="", init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cooldown_seconds = max(0, _as_int(self.cooldown_seconds, 0))
        self.daily_limit = max(0, _as_int(self.daily_limit, 0))
        self.max_keys = max(16, _as_int(self.max_keys, DEFAULT_MAX_KEYS))
        if not callable(self.clock):
            self.clock = time.monotonic
        if not callable(self.today):
            now = datetime.now()
            self.today = lambda: now.strftime("%Y-%m-%d")

    @classmethod
    def from_config(cls, cfg: Any = None, **overrides: Any) -> RateLimiter:
        """由 RuntimeConfig / 配置映射构造。"""
        config = ensure_runtime_config(cfg)
        values: dict[str, Any] = {
            "cooldown_seconds": config.cooldown_seconds,
            "daily_limit": config.daily_limit,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    # ------------------------------------------------------------ 主逻辑

    async def check_and_consume(self, key: Any = "") -> tuple[bool, str | None]:
        """判定并消费一次配额：允许返回 (True, None)，拒绝返回 (False, 提示)。"""
        decision = await self.decide(key)
        return decision.allowed, decision.message

    async def decide(self, key: Any = "") -> RateLimitState:
        """与 check_and_consume 同源的结构化判定（用于日志与测试）。"""
        normalised = self._normalise_key(key)
        async with self._lock:
            self._roll_day()
            now = self._clock()
            self._expire_state(now)
            state = RateLimitState(allowed=True, key=normalised)
            cooldown = self.cooldown_seconds
            if cooldown > 0:
                last = self._last_at.get(normalised)
                if last is not None:
                    elapsed = now - last
                    if elapsed < cooldown:
                        wait = cooldown - max(0.0, elapsed)
                        state.allowed = False
                        state.reason = "cooldown"
                        state.retry_after = wait
                        state.message = cooldown_message(wait)
                        state.used_today = self._daily.get(normalised, 0)
                        state.remaining_today = self._remaining_locked(normalised)
                        return state
            limit = self.daily_limit
            used = self._daily.get(normalised, 0)
            if limit > 0 and used >= limit:
                state.allowed = False
                state.reason = "daily_limit"
                state.message = daily_limit_message(limit)
                state.used_today = used
                state.remaining_today = 0
                return state
            active_keys = self._last_at.keys() | self._daily.keys()
            if (
                (cooldown > 0 or limit > 0)
                and normalised not in active_keys
                and len(active_keys) >= self.max_keys
            ):
                state.allowed = False
                state.reason = "capacity"
                state.message = "点歌服务繁忙，请稍后再试～"
                state.remaining_today = self._remaining_locked(normalised)
                return state
            if cooldown > 0:
                self._last_at[normalised] = now
            if limit > 0:
                self._daily[normalised] = used + 1
            state.used_today = self._daily.get(normalised, 0)
            state.remaining_today = self._remaining_locked(normalised)
            return state

    async def remaining(self, key: Any = "") -> int:
        """今日剩余次数；daily_limit=0（不限）时返回 -1。"""
        if self.daily_limit <= 0:
            return -1
        normalised = self._normalise_key(key)
        async with self._lock:
            self._roll_day()
            return self._remaining_locked(normalised)

    def reset(self) -> None:
        """清空全部状态（冷却 + 每日计数）；同步方法，测试方便。"""
        self._last_at.clear()
        self._daily.clear()
        self._day = ""
        logger.debug("限流状态已重置")

    def snapshot(self) -> dict[str, Any]:
        """当前状态快照（调试用）。"""
        return {
            "day": self._day,
            "cooldown_seconds": self.cooldown_seconds,
            "daily_limit": self.daily_limit,
            "tracked_keys": len(self._last_at.keys() | self._daily.keys()),
            "daily_keys": len(self._daily),
        }

    # ------------------------------------------------------------ 内部

    def _normalise_key(self, key: Any) -> str:
        if key is None or isinstance(key, bool):
            return ""
        if isinstance(key, str):
            return key.strip()
        if isinstance(key, (int, float)):
            return str(key)
        return str(key)

    def _clock(self) -> float:
        value = _as_float(self.clock(), 0.0)
        return value

    def _roll_day(self) -> None:
        """日期变化时清空每日计数（计数表有界的第一道保证）。"""
        current = str(self.today() or "")
        if current != self._day:
            if self._day:
                logger.debug("跨天（%s -> %s），清空每日限额计数。", self._day, current)
            self._day = current
            self._daily.clear()

    def _remaining_locked(self, key: str) -> int:
        if self.daily_limit <= 0:
            return -1
        return max(0, self.daily_limit - self._daily.get(key, 0))

    def _expire_state(self, now: float) -> None:
        """只释放不再需要的状态，不删除有效的冷却或当日计数。"""
        if self.cooldown_seconds <= 0:
            self._last_at.clear()
        else:
            stale = [
                key for key, at in self._last_at.items()
                if now - at >= self.cooldown_seconds
            ]
            for key in stale:
                self._last_at.pop(key, None)
        if self.daily_limit <= 0:
            self._daily.clear()
