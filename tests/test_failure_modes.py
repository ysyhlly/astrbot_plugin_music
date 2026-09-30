"""t12 故障注入、降级路径与反例/边界。

五种故障（全部指向 127.0.0.1 的假服务 / 未监听端口，不联网）：
  (a) 搜索端点 500；(b) 搜索返回空 songs；(c) 歌词端点超时；
  (d) t2i 端点返回非 200（以及 200 但缺少图片 id）；(e) base_url 指向未监听端口。

每种都断言：不抛未捕获异常、最多一条可读中文提示、日志不刷屏（warning 数量有界）。
另外覆盖边界输入（空/空白/超长/emoji/特殊字符）、模板转义、并发多用户互不串台，
以及「测试进程的 AstrBot 数据目录已隔离到仓库外」。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_music.core.config import RuntimeConfig  # noqa: E402
from astrbot_plugin_music.core.lyrics_flow import run_lyrics_flow  # noqa: E402
from astrbot_plugin_music.core.models import Lyric, LyricLine, SongInfo  # noqa: E402
from astrbot_plugin_music.core.netease.endpoints import (  # noqa: E402
    MODE_SELF_HOSTED,
    PATH_COMMENTS,
    PATH_LYRIC,
    PATH_SEARCH,
    PATH_SONG_DETAIL,
    PATH_SONG_URL,
)
from astrbot_plugin_music.core.netease.http import HttpTransport  # noqa: E402
from astrbot_plugin_music.core.netease.provider import NeteaseProvider  # noqa: E402
from astrbot_plugin_music.core.ratelimit import RateLimiter, build_key  # noqa: E402
from astrbot_plugin_music.core.renderer import DefaultRenderer  # noqa: E402
from astrbot_plugin_music.core.t2i.templates import LYRICS_TEMPLATE, render_template  # noqa: E402
from tests.conftest import require_astrbot  # noqa: E402

AUDIO_URL = "https://m8.music.126.net/song.mp3"
COVER_URL = "http://p1.music.126.net/cover.jpg"
PLUGIN_LOGGER_PREFIX = "astrbot_plugin_music"


def _plugin_main():
    if not require_astrbot():
        pytest.skip("AstrBot 参考源码不可用")
    return importlib.import_module("astrbot_plugin_music.main")


def make_config(**overrides: Any) -> RuntimeConfig:
    base: dict[str, Any] = {
        "provider": "netease",
        "netease_mode": MODE_SELF_HOSTED,
        "max_retries": 0,
        "search_timeout": 3.0,
        "api_timeout": 3.0,
    }
    base.update(overrides)
    return RuntimeConfig.from_mapping(base)


def make_song(**overrides: Any) -> SongInfo:
    values: dict[str, Any] = {
        "id": "186016",
        "name": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "duration_ms": 269000,
        "cover_url": COVER_URL,
    }
    values.update(overrides)
    return SongInfo(**values)


SEARCH_PAYLOAD = {
    "code": 200,
    "result": {
        "songCount": 3,
        "songs": [
            {
                "id": 5097785,
                "name": "晴天 (女声版)",
                "ar": [{"id": 1001, "name": "某翻唱歌手"}],
                "al": {"id": 11, "name": "翻唱合集", "picUrl": "http://p2.music.126.net/cover2.jpg"},
                "dt": 240000,
            },
            {
                "id": 186016,
                "name": "晴天",
                "ar": [{"id": 6452, "name": "周杰伦"}],
                "al": {"id": 18917, "name": "叶惠美", "picUrl": COVER_URL},
                "dt": 269000,
            },
            {
                "id": 999,
                "name": "晴天",
                "ar": [{"id": 2002, "name": "群星"}],
                "al": {"id": 12, "name": "合辑"},
                "dt": 250000,
            },
        ],
    },
}
EMPTY_SEARCH_PAYLOAD = {"code": 200, "result": {"songCount": 0, "songs": []}}
LYRIC_PAYLOAD = {"code": 200, "lrc": {"lyric": "[00:00.00]第一句\n[00:03.00]第二句"}, "tlyric": {"lyric": ""}}
COMMENTS_PAYLOAD = {
    "code": 200,
    "data": {
        "totalCount": 2,
        "comments": [
            {"user": {"nickname": "小明"}, "content": "好听", "likedCount": 5, "time": 1580000000000},
            {"user": {"nickname": "小红"}, "content": "循环", "likedCount": 1, "time": 1590000000000},
        ],
        "hasMore": False,
    },
}
SONGS_BY_ID: dict[str, dict[str, Any]] = {
    "186016": {"id": 186016, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美", "picUrl": COVER_URL}, "dt": 269000},
    "999": {"id": 999, "name": "晴天", "ar": [{"name": "群星"}], "al": {"name": "合辑", "picUrl": COVER_URL}, "dt": 250000},
    "5097785": {"id": 5097785, "name": "晴天 (女声版)", "ar": [{"name": "某翻唱歌手"}], "al": {"name": "翻唱合集", "picUrl": COVER_URL}, "dt": 240000},
}


def _detail_songs(ids: Any) -> list[dict[str, Any]]:
    """按请求的 ids 回显详情（真实 /song/detail 的行为）。

    固定回同一首歌会掩盖选曲错误：ids=999 时返回 186016 会让「选曲正确性」断言假失败。
    """
    text = str(ids or "").strip().strip("[]")
    return [SONGS_BY_ID[item.strip()] for item in text.split(",") if item.strip() in SONGS_BY_ID]


AUDIO_PAYLOAD = {"code": 200, "data": [{"id": 186016, "url": AUDIO_URL}]}


class RecordingRenderer:
    def __init__(self, result: Any = "https://img.example.com/lyrics.png") -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    async def render_html(self, template: str, data: Any = None, options: Any = None) -> Any:
        self.calls.append({"template": template, "data": dict(data or {}), "options": options})
        return self.result

    async def render_markdown(self, md: str, options: Any = None) -> Any:
        self.calls.append({"md": md})
        return self.result


@contextlib.asynccontextmanager
async def mock_netease(overrides: dict[str, Any] | None = None):
    rules = dict(overrides or {})
    calls: list[dict[str, Any]] = []

    async def handle(request: web.Request) -> web.Response:
        calls.append({"path": request.path, "params": dict(request.query)})
        rule = rules.get(request.path)
        if callable(rule):
            return await rule(request)
        if isinstance(rule, tuple):
            status, payload = rule
            return web.json_response(payload, status=status)
        if request.path == PATH_SONG_DETAIL:
            return web.json_response({"code": 200, "songs": _detail_songs(request.query.get("ids", ""))})
        defaults = {
            PATH_SEARCH: SEARCH_PAYLOAD,
            PATH_LYRIC: LYRIC_PAYLOAD,
            PATH_COMMENTS: COMMENTS_PAYLOAD,
            PATH_SONG_URL: AUDIO_PAYLOAD,
        }
        payload = defaults.get(request.path)
        if payload is None:
            return web.json_response({"code": 404, "msg": "not found"}, status=404)
        return web.json_response(payload)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/"), calls
    finally:
        await server.close()


def open_transport(base_url: str, *, max_retries: int = 0, timeout: float = 3.0) -> HttpTransport:
    return HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=max_retries, timeout=timeout)


def plugin_logs(caplog: pytest.LogCaptureFixture, level: int = logging.WARNING) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith(PLUGIN_LOGGER_PREFIX) and record.levelno >= level]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- (a) 搜索 500


async def test_search_500_degrades_to_one_message(caplog: pytest.LogCaptureFixture) -> None:
    main = _plugin_main()

    async def boom(request: web.Request) -> web.Response:
        return web.json_response({"code": 500}, status=500)

    with caplog.at_level(logging.WARNING, logger=PLUGIN_LOGGER_PREFIX):
        async with mock_netease({PATH_SEARCH: boom}) as (base_url, calls):
            cfg = make_config(netease_api_base=base_url)
            async with open_transport(base_url) as transport:
                outcome = await main.run_music_request(
                    cfg, keyword="晴天", provider=NeteaseProvider(cfg), transport=transport, renderer=RecordingRenderer()
                )
    assert outcome.ok is False and outcome.status == "not_found"
    assert len(outcome.messages) == 1 and outcome.messages[0][0] == "text"
    assert "没有找到" in outcome.messages[0][1]
    assert len(calls) == 1, "max_retries=0 时 5xx 不应重试"
    assert len(plugin_logs(caplog)) <= 3, [record.getMessage() for record in plugin_logs(caplog)]


async def test_search_500_retries_are_bounded(caplog: pytest.LogCaptureFixture) -> None:
    main = _plugin_main()

    async def boom(request: web.Request) -> web.Response:
        return web.json_response({"code": 500}, status=500)

    with caplog.at_level(logging.WARNING, logger=PLUGIN_LOGGER_PREFIX):
        async with mock_netease({PATH_SEARCH: boom}) as (base_url, calls):
            cfg = make_config(netease_api_base=base_url, max_retries=2)
            async with open_transport(base_url, max_retries=2) as transport:
                outcome = await main.run_music_request(
                    cfg, keyword="晴天", provider=NeteaseProvider(cfg), transport=transport, renderer=RecordingRenderer()
                )
    assert outcome.status == "not_found" and len(outcome.messages) == 1
    assert 1 <= len(calls) <= 3, f"重试次数应有界，实际 {len(calls)} 次"
    assert len(plugin_logs(caplog)) <= 6


# ---------------------------------------------------------------- (b) 空结果


async def test_search_empty_songs_degrades_to_one_message() -> None:
    main = _plugin_main()
    async with mock_netease({PATH_SEARCH: (200, EMPTY_SEARCH_PAYLOAD)}) as (base_url, calls):
        cfg = make_config(netease_api_base=base_url)
        async with open_transport(base_url) as transport:
            outcome = await main.run_music_request(
                cfg, keyword="不存在的歌", provider=NeteaseProvider(cfg), transport=transport, renderer=RecordingRenderer()
            )
    assert outcome.ok is False and outcome.status == "not_found"
    assert len(outcome.messages) == 1 and "没有找到" in outcome.messages[0][1]
    assert len(calls) == 1


# ---------------------------------------------------------------- (c) 歌词超时


async def test_lyric_timeout_falls_back_to_text(caplog: pytest.LogCaptureFixture) -> None:
    main = _plugin_main()

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(1.0)
        return web.json_response(LYRIC_PAYLOAD)

    with caplog.at_level(logging.WARNING, logger=PLUGIN_LOGGER_PREFIX):
        async with mock_netease({PATH_LYRIC: slow}) as (base_url, _calls):
            cfg = make_config(netease_api_base=base_url, api_timeout=0.3, search_timeout=3.0)
            async with open_transport(base_url, timeout=3.0) as transport:
                outcome = await main.run_music_request(
                    cfg, keyword="晴天", provider=NeteaseProvider(cfg), transport=transport, renderer=RecordingRenderer()
                )

    assert outcome.status == "ok", outcome.to_dict()
    assert outcome.lyrics is not None
    assert outcome.lyrics.has_image is False
    assert "歌词" in (outcome.lyrics.text or ""), outcome.lyrics.to_dict()
    texts = [payload for kind, payload in outcome.messages if kind == "text"]
    assert len(texts) == 1, outcome.to_dict()
    assert len(plugin_logs(caplog)) <= 6, [record.getMessage() for record in plugin_logs(caplog)]


# ---------------------------------------------------------------- (d) t2i 非 200


async def test_t2i_endpoint_500_falls_back_to_text(caplog: pytest.LogCaptureFixture) -> None:
    lyric = Lyric(text="第一句\n第二句", lines=[LyricLine(text="第一句"), LyricLine(text="第二句")])

    posts: list[dict[str, Any]] = []

    async def boom(request: web.Request) -> web.Response:
        body = await request.json()
        posts.append(dict((body or {}).get("options") or {}))
        return web.json_response({"code": 500, "msg": "render failed"}, status=500)

    with caplog.at_level(logging.WARNING, logger=PLUGIN_LOGGER_PREFIX):
        async with mock_netease({"/text2img/generate": boom}) as (base_url, calls):
            cfg = make_config(lyrics_t2i_endpoint=f"{base_url}/text2img", lyrics_render_mode="network")
            renderer = DefaultRenderer(star=None)
            result = await run_lyrics_flow(cfg, make_song(), None, None, renderer, lyric=lyric)

    assert result is not None and result.has_image is False and result.has_text is True
    # HTML 卡片与安全文本图片各最多进行一次 viewport 兼容重试，之后发送纯文本。
    assert [call["path"] for call in calls] == ["/text2img/generate"] * len(posts)
    assert len(posts) == 4, posts
    for first, retry in zip(posts[::2], posts[1::2]):
        assert "viewport_width" in first and "viewport_height" in first
        assert "viewport_width" not in retry and "viewport_height" not in retry
    assert len(plugin_logs(caplog, logging.ERROR)) <= 4


async def test_viewport_rejection_is_cached_and_not_retried() -> None:
    """端点拒绝 viewport 键时：去掉 viewport 重试一次即成功，并把该端点记入拒绝缓存。"""
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])
    posts: list[dict[str, Any]] = []

    async def viewport_intolerant(request: web.Request) -> web.Response:
        body = await request.json()
        options = dict((body or {}).get("options") or {})
        posts.append(options)
        if "viewport_width" in options or "viewport_height" in options:
            return web.json_response({"code": 500, "msg": "unknown option"}, status=500)
        return web.json_response({"code": 0, "message": "success", "data": {"id": "data/rendered_cached.jpeg"}})

    async with mock_netease({"/text2img/generate": viewport_intolerant}) as (base_url, _calls):
        cfg = make_config(lyrics_t2i_endpoint=f"{base_url}/text2img", lyrics_render_mode="network")
        first = await run_lyrics_flow(cfg, make_song(), None, None, DefaultRenderer(star=None), lyric=lyric)
        first_round = len(posts)
        second = await run_lyrics_flow(cfg, make_song(), None, None, DefaultRenderer(star=None), lyric=lyric)

    assert first is not None and first.has_image
    assert first_round == 2, posts
    assert "viewport_width" in posts[0] and "viewport_width" not in posts[1]
    assert second is not None and second.has_image
    assert len(posts) - first_round == 1, "拒绝缓存生效后不应再重复试探"
    assert "viewport_width" not in posts[-1]


async def test_t2i_endpoint_without_id_falls_back_to_text() -> None:
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])

    async def no_id(request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "message": "success", "data": {}})

    async with mock_netease({"/text2img/generate": no_id}) as (base_url, _calls):
        cfg = make_config(lyrics_t2i_endpoint=f"{base_url}/text2img", lyrics_render_mode="network")
        result = await run_lyrics_flow(cfg, make_song(), None, None, DefaultRenderer(star=None), lyric=lyric)
    assert result is not None and result.has_image is False and result.has_text is True


# ---------------------------------------------------------------- (e) 端口不可达


async def test_unreachable_base_url_degrades_without_hanging() -> None:
    main = _plugin_main()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    cfg = make_config(netease_api_base=base_url, search_timeout=1.0, api_timeout=1.0)
    started = time.monotonic()
    async with open_transport(base_url, timeout=1.0) as transport:
        outcome = await main.run_music_request(
            cfg, keyword="晴天", provider=NeteaseProvider(cfg), transport=transport, renderer=RecordingRenderer()
        )
    elapsed = time.monotonic() - started
    assert outcome.ok is False and outcome.status == "not_found"
    assert len(outcome.messages) == 1 and "没有找到" in outcome.messages[0][1]
    assert elapsed < 5.0, f"不可达主机不应长时间阻塞（{elapsed:.2f}s）"


# ---------------------------------------------------------------- 边界输入


def test_parse_command_text_边界() -> None:
    main = _plugin_main()
    parse = main.parse_command_text
    assert parse("").error == "empty"
    assert parse("    ").error == "empty"
    assert parse(None).error == "empty"
    assert parse("/点歌").error == "missing_keyword"
    assert parse("/点歌   ").error == "missing_keyword"
    assert parse("/点歌 晴天").keyword == "晴天"
    assert parse("/点歌 晴天 周杰伦").artist == "周杰伦"
    assert parse("/点歌 晴天 周 杰 伦").artist == "周 杰 伦"
    assert parse("/点歌 晴天       ").artist == ""
    assert parse("／点歌 晴天").keyword == "晴天"

    long_name = "长" * 300
    request = parse(f"/点歌 {long_name}")
    assert request.is_valid and len(request.keyword) == 80

    emoji = "/点歌 🎵<b>晴天</b> 周杰伦"
    parsed = parse(emoji)
    assert parsed.is_valid and "🎵" in parsed.keyword
    assert parsed.artist == "周杰伦"
    assert parse("/点歌 ../../etc/passwd").is_valid
    assert parse("/点歌 \u200b\u3000").error in {"empty", "missing_keyword"}


async def test_overlong_keyword_is_truncated_before_http() -> None:
    main = _plugin_main()
    async with mock_netease() as (base_url, calls):
        cfg = make_config(netease_api_base=base_url)
        async with open_transport(base_url) as transport:
            outcome = await main.run_music_request(
                cfg,
                keyword="长" * 300,
                provider=NeteaseProvider(cfg),
                transport=transport,
                renderer=RecordingRenderer(),
            )
    assert outcome.status in {"ok", "not_found"}
    search_calls = [call for call in calls if call["path"] == PATH_SEARCH]
    assert search_calls, calls
    assert len(search_calls[0]["params"]["keywords"]) <= 80


def test_lyric_template_escapes_user_content() -> None:
    from astrbot_plugin_music.core.t2i import build_lyrics_data

    lyric = Lyric(text="正常歌词", lines=[LyricLine(text="正常歌词")])
    cfg = make_config()
    data = build_lyrics_data(make_song(name="<script>alert(1)</script>"), lyric, cfg)
    html = render_template(LYRICS_TEMPLATE, data)
    assert html is not None
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------- 并发隔离


async def test_three_concurrent_users_are_isolated() -> None:
    main = _plugin_main()
    limiter = RateLimiter(cooldown_seconds=60, daily_limit=0)
    users = {
        "10001": ("周杰伦", "186016"),
        "10002": ("群星", "999"),
        "10003": ("某翻唱歌手", "5097785"),
    }

    async def request_for(user_id: str) -> Any:
        artist, _expected = users[user_id]
        async with mock_netease() as (base_url, _calls):
            cfg = make_config(netease_api_base=base_url, search_limit=5)
            async with open_transport(base_url) as transport:
                return await main.run_music_request(
                    cfg,
                    keyword="晴天",
                    artist=artist,
                    provider=NeteaseProvider(cfg),
                    transport=transport,
                    renderer=RecordingRenderer(),
                    limiter=limiter,
                    rate_key=build_key("aiocqhttp:GroupMessage:1", user_id),
                )

    outcomes = await asyncio.gather(*(request_for(user_id) for user_id in users))
    observed = {user_id: outcome.song.id for user_id, outcome in zip(users, outcomes)}
    assert observed == {user_id: expected for user_id, (_artist, expected) in users.items()}
    assert all(outcome.status == "ok" for outcome in outcomes)
    assert all(outcome.messages for outcome in outcomes), "任何用户都不能什么都收不到"

    # 冷却按用户隔离：已消费的用户被拒，新用户仍可点歌
    blocked, message = await limiter.check_and_consume(build_key("aiocqhttp:GroupMessage:1", "10001"))
    assert blocked is False and "秒" in (message or "")
    fresh, _ = await limiter.check_and_consume(build_key("aiocqhttp:GroupMessage:1", "10004"))
    assert fresh is True


async def test_concurrent_same_user_only_one_passes_with_cooldown() -> None:
    main = _plugin_main()
    limiter = RateLimiter(cooldown_seconds=60, daily_limit=0)
    key = build_key("aiocqhttp:GroupMessage:1", "10001")

    async def request_once() -> Any:
        async with mock_netease() as (base_url, _calls):
            cfg = make_config(netease_api_base=base_url)
            async with open_transport(base_url) as transport:
                return await main.run_music_request(
                    cfg,
                    keyword="晴天",
                    provider=NeteaseProvider(cfg),
                    transport=transport,
                    renderer=RecordingRenderer(),
                    limiter=limiter,
                    rate_key=key,
                )

    outcomes = await asyncio.gather(*(request_once() for _ in range(3)))
    statuses = sorted(outcome.status for outcome in outcomes)
    assert statuses == ["ok", "rate_limited", "rate_limited"], statuses
    limited = next(outcome for outcome in outcomes if outcome.status == "rate_limited")
    assert len(limited.messages) == 1 and limited.messages[0][0] == "text"
    assert "秒" in limited.messages[0][1]


# ---------------------------------------------------------------- 数据目录隔离


def test_astrbot_root_is_redirected_outside_repo() -> None:
    """conftest 必须把 AstrBot 数据目录重定向到仓库外（否则会在仓库里写 data/）。"""
    root = os.environ.get("ASTRBOT_ROOT", "").strip()
    assert root, "ASTRBOT_ROOT 未设置：pytest 可能在仓库根写出 data/"
    assert Path(root).resolve() != PLUGIN_ROOT.resolve()
    assert PLUGIN_ROOT.resolve() not in Path(root).resolve().parents
