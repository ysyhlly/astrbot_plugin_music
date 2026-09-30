"""core/ratelimit.py：按 umo+user_id 的冷却与每日限额（全程无网络、无真实等待）。

覆盖契约：check_and_consume 返回 (allowed, 可读中文提示)；cooldown_seconds=0 不限冷却；
daily_limit=0 不限次数；计数表按日期清理并保持有界；reset() 清空；并发下 asyncio.Lock
保证「每日限额」不会被超发。
"""

from __future__ import annotations

import asyncio
import re

import pytest

from core.config import RuntimeConfig
from core.ratelimit import (
    DEFAULT_MAX_KEYS,
    RateLimiter,
    build_key,
    cooldown_message,
    daily_limit_message,
)


class FakeClock:
    """可注入的时钟：monotonic 秒 + 日期字符串。"""

    def __init__(self, start: float = 1000.0, day: str = "2024-05-01") -> None:
        self.now = float(start)
        self.day = day

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def set_day(self, day: str, *, shift: float = 86400.0) -> None:
        self.day = day
        self.advance(shift)

    def __call__(self) -> float:
        return self.now

    def today(self) -> str:
        return self.day


def make_limiter(**kwargs):
    clock = kwargs.pop("clock", None) or FakeClock()
    limiter = RateLimiter(
        cooldown_seconds=kwargs.pop("cooldown_seconds", 0),
        daily_limit=kwargs.pop("daily_limit", 0),
        max_keys=kwargs.pop("max_keys", DEFAULT_MAX_KEYS),
        clock=clock,
        today=clock.today,
        **kwargs,
    )
    return limiter, clock


# ------------------------------------------------------------------ 冷却


async def test_cooldown_rejects_second_call_immediately():
    limiter, _ = make_limiter(cooldown_seconds=5)
    allowed, message = await limiter.check_and_consume("umo:user")
    assert (allowed, message) == (True, None)

    allowed, message = await limiter.check_and_consume("umo:user")
    assert allowed is False
    assert message
    assert "秒" in message
    assert re.search(r"[0-9]", message), "提示里应带剩余秒数"


async def test_cooldown_releases_after_window():
    limiter, clock = make_limiter(cooldown_seconds=5)
    await limiter.check_and_consume("k")
    clock.advance(4.9)
    allowed, message = await limiter.check_and_consume("k")
    assert allowed is False and message
    clock.advance(0.2)  # 累计 5.1s > 5s
    allowed, message = await limiter.check_and_consume("k")
    assert allowed is True and message is None


async def test_cooldown_is_per_key():
    limiter, _ = make_limiter(cooldown_seconds=30)
    assert (await limiter.check_and_consume("a"))[0] is True
    assert (await limiter.check_and_consume("b"))[0] is True
    assert (await limiter.check_and_consume("a"))[0] is False


async def test_cooldown_zero_means_unlimited():
    limiter, _ = make_limiter(cooldown_seconds=0)
    for _ in range(50):
        allowed, message = await limiter.check_and_consume("k")
        assert allowed is True and message is None


async def test_cooldown_message_is_readable_chinese():
    text = cooldown_message(2.4)
    assert text.startswith("点歌太频繁")
    assert "3 秒" in text  # 向上取整，避免显示 0 秒


# ------------------------------------------------------------------ 每日限额


async def test_daily_limit_zero_is_unlimited():
    limiter, _ = make_limiter(daily_limit=0)
    for _ in range(200):
        assert (await limiter.check_and_consume("k"))[0] is True
    assert await limiter.remaining("k") == -1


async def test_daily_limit_blocks_after_quota():
    limiter, _ = make_limiter(daily_limit=3)
    for index in range(3):
        allowed, message = await limiter.check_and_consume("k")
        assert allowed is True, index
        assert message is None
    allowed, message = await limiter.check_and_consume("k")
    assert allowed is False
    assert message == daily_limit_message(3)
    assert "上限 3 次" in message
    assert await limiter.remaining("k") == 0


async def test_daily_limit_is_per_key():
    limiter, _ = make_limiter(daily_limit=1)
    assert (await limiter.check_and_consume("a"))[0] is True
    assert (await limiter.check_and_consume("a"))[0] is False
    assert (await limiter.check_and_consume("b"))[0] is True


async def test_daily_counter_resets_on_new_day():
    limiter, clock = make_limiter(daily_limit=2)
    assert (await limiter.check_and_consume("k"))[0] is True
    assert (await limiter.check_and_consume("k"))[0] is True
    assert (await limiter.check_and_consume("k"))[0] is False

    clock.set_day("2024-05-02")
    allowed, message = await limiter.check_and_consume("k")
    assert allowed is True and message is None
    assert limiter.snapshot()["day"] == "2024-05-02"


async def test_daily_table_is_bounded():
    limiter, _ = make_limiter(daily_limit=5, max_keys=16)
    decisions = []
    for index in range(200):
        decisions.append(await limiter.decide(f"key-{index}"))
    assert all(state.allowed for state in decisions[:16])
    assert all(not state.allowed and state.reason == "capacity" for state in decisions[16:])
    # 已记录的 key 仍可使用剩余额度，满员不会重置其每日计数。
    assert await limiter.remaining("key-0") == 4
    for _ in range(4):
        assert (await limiter.check_and_consume("key-0"))[0] is True
    assert (await limiter.decide("key-0")).reason == "daily_limit"
    snapshot = limiter.snapshot()
    assert snapshot["daily_keys"] == 16
    assert snapshot["tracked_keys"] == 16


async def test_cooldown_table_is_bounded():
    limiter, clock = make_limiter(cooldown_seconds=1, max_keys=16)
    for index in range(200):
        await limiter.check_and_consume(f"key-{index}")
        clock.advance(2)
    assert limiter.snapshot()["tracked_keys"] <= 16


async def test_concurrent_calls_do_not_exceed_daily_limit():
    limiter, _ = make_limiter(daily_limit=5)
    results = await asyncio.gather(
        *(limiter.check_and_consume("same") for _ in range(40))
    )
    allowed = [item for item in results if item[0]]
    denied = [item for item in results if not item[0]]
    assert len(allowed) == 5
    assert len(denied) == 35
    assert all("上限" in (message or "") for _, message in denied)


async def test_remaining_reports_left():
    limiter, _ = make_limiter(daily_limit=4)
    assert await limiter.remaining("k") == 4
    await limiter.check_and_consume("k")
    assert await limiter.remaining("k") == 3


# ------------------------------------------------------------------ 其他


async def test_decide_returns_structured_state():
    limiter, _ = make_limiter(cooldown_seconds=10)
    first = await limiter.decide("k")
    assert first.allowed is True and first.reason == ""

    second = await limiter.decide("k")
    assert second.allowed is False
    assert second.reason == "cooldown"
    assert second.retry_after > 0
    assert second.to_dict()["allowed"] is False


async def test_reset_clears_state():
    limiter, _ = make_limiter(cooldown_seconds=60, daily_limit=1)
    await limiter.check_and_consume("k")
    assert (await limiter.check_and_consume("k"))[0] is False
    limiter.reset()
    assert (await limiter.check_and_consume("k"))[0] is True


async def test_from_config_reads_runtime_config():
    cfg = RuntimeConfig.from_mapping({"cooldown_seconds": 7, "daily_limit": 2})
    limiter = RateLimiter.from_config(cfg)
    assert limiter.cooldown_seconds == 7
    assert limiter.daily_limit == 2
    assert (await limiter.check_and_consume("k"))[0] is True
    # 冷却先于每日限额命中
    allowed, message = await limiter.check_and_consume("k")
    assert allowed is False and message and "秒" in message

    cfg2 = RuntimeConfig.from_mapping({"cooldown_seconds": 0, "daily_limit": 2})
    limiter2 = RateLimiter.from_config(cfg2)
    assert (await limiter2.check_and_consume("k"))[0] is True
    assert (await limiter2.check_and_consume("k"))[0] is True
    assert (await limiter2.check_and_consume("k"))[0] is False


async def test_weird_keys_do_not_raise():
    limiter, _ = make_limiter(cooldown_seconds=1, daily_limit=1)
    for key in (None, "", 0, 12, 3.5, True, object()):
        allowed, message = await limiter.check_and_consume(key)
        assert isinstance(allowed, bool)
        assert message is None or isinstance(message, str)


def test_build_key_variants():
    assert build_key("umo", "user") == "umo:user"
    assert build_key("umo", "") == "umo"
    assert build_key("", "user") == "user"
    assert build_key(None, None) == ""
    assert build_key("  umo  ", " u ") == "umo:u"


def test_default_max_keys_is_sane():
    assert DEFAULT_MAX_KEYS >= 64


async def test_default_capacity_preserves_cooldown_and_daily_limit():
    limiter, clock = make_limiter(cooldown_seconds=60, daily_limit=1)
    assert (await limiter.check_and_consume("victim"))[0] is True
    for index in range(DEFAULT_MAX_KEYS - 1):
        assert (await limiter.check_and_consume(f"other-{index}"))[0] is True

    # 第 4097 个 key 必须暂拒，而不是淘汰最早用户的有效限额。
    crowded = await limiter.decide("overflow")
    assert not crowded.allowed and crowded.reason == "capacity"
    assert crowded.message and "稍后再试" in crowded.message
    assert (await limiter.decide("victim")).reason == "cooldown"
    clock.advance(60)
    assert (await limiter.decide("victim")).reason == "daily_limit"
    assert (await limiter.decide("overflow")).reason == "capacity"
    assert await limiter.remaining("victim") == 0
    assert limiter.snapshot()["tracked_keys"] == DEFAULT_MAX_KEYS
    assert limiter.snapshot()["daily_keys"] == DEFAULT_MAX_KEYS


async def test_cooldown_expiry_releases_capacity_on_same_day():
    limiter, clock = make_limiter(cooldown_seconds=5, max_keys=16)
    for index in range(16):
        assert (await limiter.check_and_consume(f"key-{index}"))[0] is True
    clock.advance(4.9)
    assert (await limiter.decide("new")).reason == "capacity"
    assert (await limiter.decide("key-0")).reason == "cooldown"
    clock.advance(0.1)
    assert (await limiter.check_and_consume("new"))[0] is True
    assert (await limiter.check_and_consume("key-0"))[0] is True
    assert limiter.snapshot()["tracked_keys"] == 2
    assert limiter.snapshot()["daily_keys"] == 0


async def test_existing_daily_key_can_use_remaining_quota_after_cooldown_at_capacity():
    limiter, clock = make_limiter(cooldown_seconds=5, daily_limit=2, max_keys=16)
    for index in range(16):
        assert (await limiter.check_and_consume(f"key-{index}"))[0] is True
    assert (await limiter.decide("new")).reason == "capacity"
    clock.advance(5)
    assert (await limiter.check_and_consume("key-0"))[0] is True
    assert await limiter.remaining("key-0") == 0
    clock.advance(5)
    assert (await limiter.decide("key-0")).reason == "daily_limit"
    assert (await limiter.decide("new")).reason == "capacity"
    assert limiter.snapshot()["daily_keys"] == 16


async def test_daily_rollover_releases_capacity():
    limiter, clock = make_limiter(daily_limit=1, max_keys=16)
    for index in range(16):
        await limiter.check_and_consume(f"key-{index}")
    assert (await limiter.decide("new")).reason == "capacity"
    clock.set_day("2024-05-02", shift=0)
    assert (await limiter.check_and_consume("new"))[0] is True
    assert (await limiter.check_and_consume("key-0"))[0] is True
    assert (await limiter.decide("key-0")).reason == "daily_limit"
    assert limiter.snapshot()["daily_keys"] == 2


async def test_daily_rollover_keeps_unexpired_cooldown_when_capacity_is_full():
    limiter, clock = make_limiter(cooldown_seconds=60, daily_limit=1, max_keys=16)
    for index in range(16):
        await limiter.check_and_consume(f"key-{index}")
    clock.set_day("2024-05-02", shift=0)
    assert (await limiter.decide("new")).reason == "capacity"
    assert (await limiter.decide("key-0")).reason == "cooldown"
    clock.advance(60)
    assert (await limiter.check_and_consume("new"))[0] is True
    assert limiter.snapshot()["tracked_keys"] == 1
    assert limiter.snapshot()["daily_keys"] == 1


async def test_concurrent_new_keys_do_not_overfill_capacity_or_reset_quota():
    limiter, _ = make_limiter(daily_limit=2, max_keys=16)
    decisions = await asyncio.gather(*(limiter.decide(f"key-{index}") for index in range(80)))
    accepted = [state.key for state in decisions if state.allowed]
    assert len(accepted) == 16
    assert all(state.allowed or state.reason == "capacity" for state in decisions)
    second_round = await asyncio.gather(*(limiter.decide(key) for key in accepted))
    assert all(state.allowed and state.remaining_today == 0 for state in second_round)
    third_round = await asyncio.gather(*(limiter.decide(key) for key in accepted))
    assert all(not state.allowed and state.reason == "daily_limit" for state in third_round)
    assert limiter.snapshot()["tracked_keys"] == 16
    assert limiter.snapshot()["daily_keys"] == 16


async def test_disabled_limits_allow_more_than_capacity_without_storing_keys():
    limiter, _ = make_limiter(max_keys=16)
    decisions = await asyncio.gather(*(limiter.decide(f"key-{index}") for index in range(100)))
    assert all(state.allowed for state in decisions)
    assert limiter.snapshot()["tracked_keys"] == 0
    assert limiter.snapshot()["daily_keys"] == 0


async def test_disabling_limits_releases_previously_full_capacity():
    limiter, _ = make_limiter(cooldown_seconds=60, daily_limit=1, max_keys=16)
    for index in range(16):
        await limiter.check_and_consume(f"key-{index}")
    assert (await limiter.decide("new")).reason == "capacity"
    limiter.cooldown_seconds = 0
    limiter.daily_limit = 0
    for index in range(40):
        assert (await limiter.check_and_consume(f"new-{index}"))[0] is True
    assert limiter.snapshot()["tracked_keys"] == 0
    assert limiter.snapshot()["daily_keys"] == 0
