"""并发编排回归测试：歌词与评论必须并行执行（captain 性能优化）。

背景：两者各取各的接口、各渲染各的模板，互不依赖。串行让耗时**相加**；
并发后总耗时约等于较慢的那一个。本测试用「人为让两个流程各睡 250ms」
来观测真实耗时——这正是这个 bug 的可证伪信号：

  串行 -> >= 0.50s
  并发 -> 约    0.25s

用真实时间断言（而非只数调用次数），否则无法区分串行与并发。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if _p not in sys.path:  # pragma: no cover
        sys.path.insert(0, _p)

from test_command_parse import (  # noqa: E402
    FakeProvider,
    FakeRenderer,
    fake_components,  # noqa: F401
    make_cfg,
    plugin_main,  # noqa: F401
)

DELAY = 0.25


async def test_lyrics_and_comments_run_concurrently(
    plugin_main, fake_components, monkeypatch
) -> None:
    """总耗时必须接近单个流程的耗时（并发），而不是两者之和（串行）。"""
    from astrbot_plugin_music.core import comments_flow as cf
    from astrbot_plugin_music.core import lyrics_flow as lf

    real_lyrics = lf.run_lyrics_flow
    real_comments = cf.run_comments_flow

    async def slow_lyrics(*args, **kwargs):
        await asyncio.sleep(DELAY)
        return await real_lyrics(*args, **kwargs)

    async def slow_comments(*args, **kwargs):
        await asyncio.sleep(DELAY)
        return await real_comments(*args, **kwargs)

    monkeypatch.setattr(plugin_main, "run_lyrics_flow", slow_lyrics)
    monkeypatch.setattr(plugin_main, "run_comments_flow", slow_comments)

    started = time.perf_counter()
    outcome = await plugin_main.run_music_request(
        make_cfg({}),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        # html=MD=None -> 两个流程都退化为纯文本，仍会各走一遍取数+渲染
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    elapsed = time.perf_counter() - started

    assert outcome.ok is True
    # 串行下界是 2*DELAY；留 0.75 倍余量避免 CI 抖动，但足以区分串行/并发
    assert elapsed < DELAY * 1.75, (
        f"歌词与评论疑似仍在串行执行：耗时 {elapsed:.3f}s，"
        f"串行下界 {DELAY * 2:.2f}s、并发预期约 {DELAY:.2f}s"
    )
    # 并发不能改变消息顺序或丢内容
    kinds = [k for k, _ in outcome.messages]
    assert kinds.count("text") >= 2, outcome.messages
    assert outcome.lyrics is not None
    assert outcome.comments is not None


async def test_concurrent_flow_failure_does_not_break_the_other(
    plugin_main, fake_components, monkeypatch
) -> None:
    """一个流程抛异常不应影响另一个流程（gather 的两侧各自隔离）。"""
    async def boom(*args, **kwargs):
        raise RuntimeError("lyrics exploded")

    monkeypatch.setattr(plugin_main, "run_lyrics_flow", boom)

    outcome = await plugin_main.run_music_request(
        make_cfg({}),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    assert outcome.ok is True
    assert outcome.comments is not None, "歌词失败不应拖垮评论流程"
    assert outcome.lyrics is None
