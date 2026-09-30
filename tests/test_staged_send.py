"""分段发送（先卡片后图片）的回归测试。

为什么需要：歌词图/评论图各要 0.5~2s 渲染，而卡片是瞬时的。若等全部渲染完再发，
用户要盯着空白等 1.5s 以上。分段后第一段只产卡片，第二段补两张图，顺序不变。

这里固定三件事，防止以后改坏：
1) 产出顺序恒为 卡片 -> 歌词 -> 评论；
2) 第二段只含**新增**消息，卡片不会被重发；
3) 调用方不传 provider 时，第二段必须自己 resolve 回来（漏了会让图片退化成文字）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if _p not in sys.path:  # pragma: no cover
        sys.path.insert(0, _p)

import test_command_parse as T  # noqa: E402
from test_command_parse import plugin_main  # noqa: E402,F401  (pytest fixture)


def _collect(plugin_main, **kwargs: Any) -> list[list[str]]:
    """跑一次分段流程，返回每一段的 kind 列表。"""
    stages: list[list[str]] = []

    async def go() -> None:
        async for _outcome, fresh in plugin_main.run_music_request_staged(**kwargs):
            stages.append([kind for kind, _ in fresh])

    asyncio.run(go())
    return stages


def _cfg() -> dict[str, Any]:
    return {"cooldown_seconds": 0, "card_type": "163"}


def test_staged_sends_card_before_images(plugin_main) -> None:
    stages = _collect(
        plugin_main,
        cfg=_cfg(),
        keyword="晴天",
        provider=T.FakeProvider(),
        transport=None,
        renderer=T.FakeRenderer(),
    )
    assert stages, '至少产出一段'
    assert stages[0] == ["chain"], "第一段只能是卡片，不能等图片"
    assert stages[1] == ["image", "image"], "第二段是歌词图 + 评论图"
    flat = [k for stage in stages for k in stage]
    assert flat == ["chain", "image", "image"], "整体顺序：卡片 -> 歌词 -> 评论"


def test_staged_second_phase_does_not_resend_card(plugin_main) -> None:
    """第二段不能把卡片再发一遍（否则用户会收到两张卡片）。"""
    stages = _collect(
        plugin_main,
        cfg=_cfg(),
        keyword="晴天",
        provider=T.FakeProvider(),
        transport=None,
        renderer=T.FakeRenderer(),
    )
    flat = [k for stage in stages for k in stage]
    assert flat.count("chain") == 1


def test_staged_resolves_provider_when_absent(plugin_main, monkeypatch) -> None:
    """调用方不传 provider 时，第二段要自己 resolve——漏了图会变成文字。

    真实 handler 就是不传 provider 的（让流程自己 resolve），所以这条必须有。
    """
    monkeypatch.setattr(plugin_main, 'resolve_provider', lambda config: T.FakeProvider())
    stages = _collect(
        plugin_main,
        cfg=_cfg(),
        keyword="晴天",
        provider=None,
        transport=None,
        renderer=T.FakeRenderer(),
    )
    flat = [k for stage in stages for k in stage]
    assert flat == ["chain", "image", "image"], "缺 provider 时不应退化成文本"


def test_staged_front_failure_yields_single_stage(plugin_main) -> None:
    """限流时只产出一段错误文案，不再跑第二段。"""
    cfg = _cfg()
    cfg["cooldown_seconds"] = 60
    limiter = plugin_main.RateLimiter.from_config(plugin_main.ensure_runtime_config(cfg))

    async def go() -> list[list[str]]:
        stages: list[list[str]] = []
        # 先消耗掉配额
        await limiter.check_and_consume('k')
        async for _o, fresh in plugin_main.run_music_request_staged(
            cfg,
            keyword="晴天",
            provider=T.FakeProvider(),
            transport=None,
            renderer=T.FakeRenderer(),
            limiter=limiter,
            rate_key="k",
        ):
            stages.append([kind for kind, _ in fresh])
        return stages

    stages = asyncio.run(go())
    assert len(stages) == 1
    assert stages[0] == ["text"]


def test_prepare_messages_only_filters_to_given_slice(plugin_main) -> None:
    """prepare_messages(only=...) 只处理这一段，用于分段发送。"""
    outcome = plugin_main.MusicRequestOutcome()
    outcome.messages = [("chain", ["card"]), ("image", "a"), ("image", "b")]
    got = plugin_main.prepare_messages(outcome, only=outcome.messages[1:])
    assert got == [("image", "a"), ("image", "b")]
