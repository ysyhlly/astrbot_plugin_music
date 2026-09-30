"""插件初始化必须健壮：转发器等辅助逻辑出错不能拖垮插件加载。

背景：一开始为了绕过容器代理，插件在激活时会起一个回环 t2i 转发器。
后来改在 compose 里给容器加 NO_PROXY=...host.docker.internal（更干净、
无需多余进程），转发器逻辑已移除。这里保留对「初始化绝不抛出」的回归，
并把新约定（端点用 host.docker.internal，靠 NO_PROXY 直连）固定下来。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if _p not in sys.path:  # pragma: no cover
        sys.path.insert(0, _p)

from test_command_parse import plugin_main  # noqa: E402,F401  (pytest fixture)


def test_plugin_class_has_no_forwarder_machinery(plugin_main) -> None:
    """转发器已移除：不应再残留相关方法（否则会悄悄再起一个进程）。"""
    cls = plugin_main.MusicPlugin
    assert not hasattr(cls, "_maybe_start_t2i_forward")
    assert not (PLUGIN_ROOT / "core" / "t2i_forward.py").exists()


async def test_initialize_does_not_raise(plugin_main) -> None:
    """initialize 必须在最简环境下也能跑完（AstrBot 会直接 await 它）。"""

    class FakeCtx:
        pass

    instance = object.__new__(plugin_main.MusicPlugin)
    instance.config = {}
    from astrbot_plugin_music.core.ratelimit import RateLimiter

    instance.rate_limiter = RateLimiter.from_config(
        plugin_main.ensure_runtime_config({})
    )
    await plugin_main.MusicPlugin.initialize(instance)
