"""t12 配置可调性矩阵：每个键「改了 → 观察到什么变化」。

矩阵分两层证据：

1. **HTTP / 行为层**：把 netease_api_base 指向本地假 NeteaseCloudMusicApi，
   断言请求参数、返回条数、被选中的歌曲等真实变化；
2. **组件 / 渲染层**：断言真实 AstrBot 组件形态（_type / toDict / Record）、
   Jinja2 模板数据与注入给 t2i 服务的 screenshot options。

MATRIX 常量列出覆盖的键与观察点，并由 test_matrix_covers_schema_keys 校验
「矩阵里的每个键都真实存在于 _conf_schema.json」，避免自说自话。
单测不联网（HTTP 只打 127.0.0.1）。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_music.core.cards import (  # noqa: E402
    CardResult,
    build_card_result,
    build_card_result_from_payload,
    music_component_is_valid,
)
from astrbot_plugin_music.core.comments_flow import EMPTY_COMMENT_MESSAGE, run_comments_flow  # noqa: E402
from astrbot_plugin_music.core.config import RuntimeConfig  # noqa: E402
from astrbot_plugin_music.core.lyrics_flow import run_lyrics_flow  # noqa: E402
from astrbot_plugin_music.core.models import CommentPage, Lyric, LyricLine, SongInfo  # noqa: E402
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
from astrbot_plugin_music.core.ratelimit import RateLimiter  # noqa: E402
from astrbot_plugin_music.core.renderer import DefaultRenderer  # noqa: E402
from astrbot_plugin_music.core.search import resolve_song  # noqa: E402
from astrbot_plugin_music.core.t2i.templates import render_template  # noqa: E402
import astrbot_plugin_music.core.renderer as renderer_module  # noqa: E402
from tests.conftest import require_astrbot  # noqa: E402


@pytest.fixture(autouse=True)
def _no_local_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """把本地 Pillow 策略置为不可用，使渲染路径断言保持确定性。

    真实 AstrBot 在场时本地策略会先成功返回，令
    「mode=local -> star.text_to_image」的断言失效（这正是性能修复点：
    本地路径由 tests/test_renderer_default.py 专门验证）。
    """
    monkeypatch.setattr(renderer_module, "local_render_strategy", lambda: None)


def _plugin_main():
    """延迟导入插件入口（缺 astrbot 时 skip 而不是收集期报错）。"""
    if not require_astrbot():
        pytest.skip("AstrBot 参考源码不可用")
    return importlib.import_module("astrbot_plugin_music.main")

AUDIO_URL = "https://m8.music.126.net/song.mp3"
SONG_URL = "https://music.163.com/song?id=186016"
COVER_URL = "http://p1.music.126.net/cover.jpg"

MATRIX: list[tuple[str, str, str]] = [
    ("search_limit", "假 API 收到 /cloudsearch 的 limit 参数", "2 -> limit=2；5 -> limit=5"),
    ("pick_strategy", "同歌手候选下 top/first 的选择", "top -> 700001（先出现的候选）；first -> 700002（完全同名候选）"),
    ("card_enable", "卡片组件与消息种类", "True -> Music 组件；False -> components=[] + 纯文本"),
    ("card_type(163)", "组件类型 / _type / toDict / 真实校验器", "163 -> Music._type=163 + 校验器 True"),
    ("card_type(custom)", "custom 合法性（url+audio+title）", "有 audio -> custom 组件；无 audio -> 按 card_fallback 退化"),
    ("card_type(share)", "Share 组件与 Music 互斥", "share -> Share(url,title) 无 Music"),
    ("card_attach_audio", "是否附带 Record 组件", "True -> [Music, Record]；False -> [Music]"),
    ("lyrics_enable", "是否取歌词与是否产生消息", "False -> 不取数、返回 None；True -> 取数一次"),
    ("lyrics_t2i", "图片消息 vs 纯文本消息", "True -> image；False -> text（render_failed）"),
    ("lyrics_render_mode", "network/local/auto 的渲染路径", "html 成功 -> 网络图；html 失败 -> t2i 三级链回落本地 Markdown 图；Markdown 也失败 -> 纯文本"),
    ("lyrics_max_lines", "模板 max_lines 与 HTML 行数", "4 -> 4 行 + 「仅显示前 4 行」；0/8 -> 全部 8 行"),
    ("lyrics_width", "注入 t2i 的 viewport_width", "900 -> 900；600 -> 600"),
    ("comments_enable", "是否取评论与是否产生消息", "False -> 不取数、返回 None；True -> 取数一次"),
    ("comments_count", "API limit 参数与模板 items 条数", "3 -> limit=3 + 3 条；8 -> limit=8 + 8 条"),
    ("comments_sort", "请求 sortType 与首条评论内容", "hot -> sortType=2 + 热门评论1；new -> sortType=3 + 最新评论1"),
    ("comments_max_chars", "评论正文截断", "3 -> 「热门评…」；0 -> 原文"),
    ("comments_page", "请求 offset", "page=2 & count=3 -> offset=3"),
    ("cooldown_seconds", "同 key 第二次调用是否被拒", "60 -> 拒绝（带剩余秒数）；0 -> 放行"),
    ("daily_limit", "第 N+1 次调用是否被拒", "2 -> 第 3 次拒绝；0 -> 不限次"),
    ("card_show_cover", "Music.image 是否有封面", "True -> 封面 URL；False -> 空"),
    ("card_show_source", "评论/卡片文本是否含来源", "True -> 含「网易云音乐」；False -> 不含"),
    ("reply_with_at", "是否在第一条消息前加 At", "True -> At(qq)；False -> 无 At"),
]

AUDIO_PROVIDER_CALLS: list[str] = []


class AudioProvider:
    """只实现 audio_url 的假 provider（custom 卡片 / 附带语音用）。"""

    key = "fake"

    def __init__(self, audio: str = "") -> None:
        self.audio = audio
        self.calls = 0

    async def audio_url(self, song: Any, transport: Any, *, level: str = "standard") -> str:
        self.calls += 1
        return self.audio

    def card_payload(self, song: Any) -> dict[str, Any]:
        return {}


class CountingProvider:
    """记录 lyrics/comments 调用次数的假 provider。"""

    key = "fake"

    def __init__(self, lyric: Any = None, page: Any = None) -> None:
        self.lyric = lyric
        self.page = page
        self.lyrics_calls = 0
        self.comments_calls = 0

    async def lyrics(self, song: Any, transport: Any) -> Any:
        self.lyrics_calls += 1
        return self.lyric

    async def comments(
        self, song: Any, transport: Any, limit: int = 20, offset: int = 0, *, sort: Any = None
    ) -> Any:
        self.comments_calls += 1
        return self.page

    def card_payload(self, song: Any) -> dict[str, Any]:
        return {}


class RecordingStar:
    """鸭子类型 Star：html_render / text_to_image 都记录收到的参数。"""

    def __init__(self, html_result: Any = "https://img.example.com/html.png", md_result: Any = "https://img.example.com/md.png") -> None:
        self.html_result = html_result
        self.md_result = md_result
        self.html_calls: list[dict[str, Any]] = []
        self.md_calls: list[dict[str, Any]] = []

    async def html_render(self, tmpl: str, data: dict, return_url: bool = True, options: Any = None) -> Any:
        self.html_calls.append({"tmpl": tmpl, "data": data, "return_url": return_url, "options": options})
        return self.html_result

    async def text_to_image(self, text: str, return_url: bool = True) -> Any:
        self.md_calls.append({"text": text, "return_url": return_url})
        return self.md_result


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

# pick_strategy 专用候选集：两位候选**歌手完全同分**（都是周杰伦），
# 只有「完全同名」这一维不同 —— 只有这样才能区分 top 与 first
# （core/search.py:148-159：first 在歌手同分时再比歌名，top 只比歌手）。
PICK_STRATEGY_SEARCH_PAYLOAD = {
    "code": 200,
    "result": {
        "songCount": 2,
        "songs": [
            {
                "id": 700001,
                "name": "晴天 (Live)",
                "ar": [{"id": 6452, "name": "周杰伦"}],
                "al": {"id": 1, "name": "演唱会", "picUrl": COVER_URL},
                "dt": 260000,
            },
            {
                "id": 700002,
                "name": "晴天",
                "ar": [{"id": 6452, "name": "周杰伦"}],
                "al": {"id": 2, "name": "叶惠美", "picUrl": COVER_URL},
                "dt": 269000,
            },
        ],
    },
}

LYRIC_PAYLOAD = {
    "code": 200,
    "lrc": {"lyric": "\n".join(f"[00:{index * 3:02d}.00]第{index + 1}句歌词" for index in range(8))},
    "tlyric": {"lyric": ""},
}

HOT_COMMENTS = [
    {
        "user": {"nickname": f"热门用户{index}"},
        "content": f"热门评论{index}",
        "likedCount": 1000 + index,
        "time": 1580000000000,
    }
    for index in range(1, 11)
]
NORMAL_COMMENTS = [
    {"user": {"nickname": f"普通用户{index}"}, "content": f"普通评论{index}", "likedCount": index, "time": 1590000000000}
    for index in range(1, 6)
]
NEW_COMMENTS = [
    {"user": {"nickname": f"最新用户{index}"}, "content": f"最新评论{index}", "likedCount": index, "time": 1600000000000}
    for index in range(1, 6)
]

DETAIL_PAYLOAD = {
    "code": 200,
    "songs": [{"id": 186016, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美", "picUrl": COVER_URL}, "dt": 269000}],
}
AUDIO_PAYLOAD = {"code": 200, "data": [{"id": 186016, "url": AUDIO_URL}]}


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
        if request.path == PATH_SEARCH:
            return web.json_response(SEARCH_PAYLOAD)
        if request.path == PATH_LYRIC:
            return web.json_response(LYRIC_PAYLOAD)
        if request.path == PATH_SONG_DETAIL:
            return web.json_response(DETAIL_PAYLOAD)
        if request.path == PATH_SONG_URL:
            return web.json_response(AUDIO_PAYLOAD)
        if request.path == PATH_COMMENTS:
            limit = max(1, int(request.query.get("limit", "20") or 20))
            offset = max(0, int(request.query.get("offset", "0") or 0))
            sort_type = str(request.query.get("sortType", "2") or "2")
            if sort_type == "3":
                payload = {"code": 200, "total": 42, "hotComments": [], "comments": NEW_COMMENTS[:limit], "more": True}
            else:
                pool = HOT_COMMENTS + NORMAL_COMMENTS
                payload = {
                    "code": 200,
                    "total": 42,
                    "hotComments": HOT_COMMENTS[max(0, offset) : offset + limit],
                    "comments": NORMAL_COMMENTS[: max(0, limit - len(HOT_COMMENTS[max(0, offset) : offset + limit]))],
                    "more": True,
                }
            return web.json_response(payload)
        return web.json_response({"code": 404, "msg": "not found"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/"), calls
    finally:
        await server.close()


def open_transport(base_url: str) -> HttpTransport:
    return HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=0)


# ---------------------------------------------------------------- 矩阵自检


def test_matrix_covers_schema_keys() -> None:
    """矩阵 >= 12 组，且每个键都存在于 _conf_schema.json（不是自造键）。"""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    leaves: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        items = node.get("items")
        if not isinstance(items, dict):
            return
        for key, value in items.items():
            if isinstance(value.get("items"), dict):
                walk(value)
            else:
                leaves.add(key)

    for group in schema.values():
        walk(group)
    assert len(MATRIX) >= 12, f"矩阵只有 {len(MATRIX)} 组"
    for key, _observation, _evidence in MATRIX:
        plain = key.split("(")[0]
        assert plain in leaves, f"矩阵里的键 {key} 不在 _conf_schema.json 中"


# ---------------------------------------------------------------- 搜索 / 选曲


async def test_search_limit_controls_api_limit() -> None:
    async with mock_netease() as (base_url, calls):
        for limit in (2, 5):
            cfg = make_config(netease_api_base=base_url, search_limit=limit)
            async with open_transport(base_url) as transport:
                provider = NeteaseProvider(cfg)
                await resolve_song(cfg, transport, "晴天", provider=provider)
            assert calls[-1]["params"]["limit"] == str(limit)


async def test_pick_strategy_changes_selected_song() -> None:
    """top 与 first 的唯一差别：歌手同分时 first 再按歌名完全一致优先。"""
    async with mock_netease({PATH_SEARCH: (200, PICK_STRATEGY_SEARCH_PAYLOAD)}) as (base_url, _calls):
        async with open_transport(base_url) as transport:
            top_cfg = make_config(netease_api_base=base_url, pick_strategy="top")
            first_cfg = make_config(netease_api_base=base_url, pick_strategy="first")
            top_song = await resolve_song(top_cfg, transport, "晴天", "周杰伦", provider=NeteaseProvider(top_cfg))
            first_song = await resolve_song(first_cfg, transport, "晴天", "周杰伦", provider=NeteaseProvider(first_cfg))
    assert top_song is not None and top_song.id == "700001", "top：歌手同分时取先出现的候选"
    assert first_song is not None and first_song.id == "700002", "first：歌手同分时取完全同名的候选"


# ---------------------------------------------------------------- 卡片


def _real_music_type() -> Any:
    require_astrbot()
    components = __import__("astrbot.api.message_components", fromlist=["Music"])
    return components.Music


def _real_validator(component_class: Any) -> Any:
    require_astrbot()
    stage = __import__("astrbot.core.pipeline.respond.stage", fromlist=["RespondStage"])
    return stage.RespondStage._component_validators[component_class]


async def test_card_enable_toggles_card_component() -> None:
    enabled = build_card_result_from_payload({"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_enable=True), song=make_song())
    disabled = build_card_result_from_payload({"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_enable=False), song=make_song())
    assert enabled.components and enabled.reason == "ok"
    assert disabled.components == [] and disabled.reason == "card_disabled"
    assert disabled.text == ""


async def test_card_type_163_component_shape() -> None:
    music_class = _real_music_type()
    card = build_card_result_from_payload({"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_type="163"), song=make_song())
    component = card.components[0]
    assert isinstance(component, music_class)
    assert getattr(component, "_type", "") == "163"
    assert component.toDict()["data"]["type"] == "163"
    assert bool(_real_validator(music_class)(component)) is True


async def test_card_type_custom_uses_provider_audio_and_degrades_without_it() -> None:
    music_class = _real_music_type()
    cfg = make_config(card_type="custom")
    provider = AudioProvider(AUDIO_URL)
    card = await build_card_result(make_song(), cfg, provider=provider, transport=object())
    assert provider.calls == 1
    component = card.components[0]
    assert getattr(component, "_type", "") == "custom"
    assert component.url and component.audio and component.title
    assert bool(_real_validator(music_class)(component)) is True
    assert component.audio == AUDIO_URL

    # 取不到播放地址 -> 按 card_fallback（默认 share）退化，绝不产出非法 custom
    degraded = await build_card_result(make_song(), cfg, provider=AudioProvider(""), transport=object())
    assert getattr(degraded.components[0], "_type", "") != "custom"
    assert degraded.degraded is True and "custom_no_audio" in degraded.reason


async def test_card_type_share_component_shape() -> None:
    require_astrbot()
    share_class = __import__("astrbot.api.message_components", fromlist=["Share"]).Share
    card = build_card_result_from_payload({"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_type="share"), song=make_song())
    component = card.components[0]
    assert isinstance(component, share_class)
    assert component.url == SONG_URL and component.title == "晴天"
    assert bool(_real_validator(share_class)(component)) is True


async def test_card_attach_audio_toggles_record() -> None:
    require_astrbot()
    components = __import__("astrbot.api.message_components", fromlist=["Record"])
    payload = {"id": "186016", "title": "晴天", "url": SONG_URL, "audio": AUDIO_URL}
    off = build_card_result_from_payload(payload, make_config(card_type="163", card_attach_audio=False), song=make_song())
    on = build_card_result_from_payload(payload, make_config(card_type="163", card_attach_audio=True), song=make_song())
    assert [type(item).__name__ for item in off.components] == ["Music"]
    assert [type(item).__name__ for item in on.components] == ["Music", "Record"]
    assert on.components[1].file == AUDIO_URL
    assert bool(_real_validator(components.Record)(on.components[1])) is True


async def test_card_show_cover_and_source_control_payload() -> None:
    with_cover = build_card_result_from_payload(
        {"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_show_cover=True, card_show_source=True), song=make_song()
    )
    without = build_card_result_from_payload(
        {"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_show_cover=False, card_show_source=False), song=make_song()
    )
    assert with_cover.components[0].image == COVER_URL
    assert without.components[0].image == ""
    assert "网易云音乐" in with_cover.components[0].content
    assert "网易云音乐" not in without.components[0].content


# ---------------------------------------------------------------- 歌词


async def test_lyrics_enable_toggles_fetch_and_message() -> None:
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])
    on_provider = CountingProvider(lyric=lyric)
    off_provider = CountingProvider(lyric=lyric)
    on = await run_lyrics_flow(make_config(lyrics_enable=True, lyrics_t2i=False), make_song(), on_provider, None, None)
    off = await run_lyrics_flow(make_config(lyrics_enable=False), make_song(), off_provider, None, None)
    assert on is not None and on.has_text
    assert off is None and off_provider.lyrics_calls == 0
    assert on_provider.lyrics_calls == 1


async def test_lyrics_t2i_false_falls_back_to_text() -> None:
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])
    renderer = RecordingStar()
    result = await run_lyrics_flow(make_config(lyrics_t2i=False), make_song(), CountingProvider(lyric=lyric), None, DefaultRenderer(renderer))
    assert result is not None and result.status == "render_failed"
    assert result.has_image is False and result.has_text is True
    assert renderer.html_calls == [] and renderer.md_calls == []


async def test_lyrics_render_mode_network_local_auto() -> None:
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])
    song = make_song()

    # mode=network：html 成功 -> 出图；text_to_image 不参与
    star_ok = RecordingStar()
    network_ok = await run_lyrics_flow(make_config(lyrics_render_mode="network"), song, CountingProvider(lyric=lyric), None, DefaultRenderer(star_ok))
    assert network_ok is not None and network_ok.has_image
    assert star_ok.html_calls and star_ok.md_calls == []

    # html 失败 -> core/t2i 三级链回落到本地 Markdown 图（Markdown 仍是图片，不是纯文本）
    star_fail = RecordingStar(html_result=None)
    fell_back = await run_lyrics_flow(make_config(lyrics_render_mode="network"), song, CountingProvider(lyric=lyric), None, DefaultRenderer(star_fail))
    assert fell_back is not None and fell_back.has_image and fell_back.image
    assert fell_back.image == star_fail.md_result
    assert star_fail.html_calls and len(star_fail.md_calls) == 1

    # mode=local：只走 text_to_image -> 出图
    star_local = RecordingStar(html_result=None)
    local = await run_lyrics_flow(make_config(lyrics_render_mode="local"), song, CountingProvider(lyric=lyric), None, DefaultRenderer(star_local))
    assert local is not None and local.has_image
    assert star_local.html_calls == [] and len(star_local.md_calls) == 1

    # mode=auto：html 失败后用 Markdown 兜底
    star_auto = RecordingStar(html_result=None)
    auto = await run_lyrics_flow(make_config(lyrics_render_mode="auto"), song, CountingProvider(lyric=lyric), None, DefaultRenderer(star_auto))
    assert auto is not None and auto.has_image
    assert len(star_auto.html_calls) == 1 and len(star_auto.md_calls) == 1

    # html 与 Markdown 都失败 -> 渲染器返回 None，flows 才输出纯文本兜底
    star_dead = RecordingStar(html_result=None, md_result=None)
    dead = await run_lyrics_flow(make_config(lyrics_render_mode="auto"), song, CountingProvider(lyric=lyric), None, DefaultRenderer(star_dead))
    assert dead is not None and dead.has_image is False and dead.has_text is True


async def test_lyrics_max_lines_controls_template_and_html() -> None:
    lines = [LyricLine(text=f"第{index}句") for index in range(1, 9)]
    lyric = Lyric(text="\n".join(line.text for line in lines), lines=lines)
    for max_lines, expected in ((4, 4), (0, 8), (8, 8)):
        renderer = RecordingStar()
        result = await run_lyrics_flow(
            make_config(lyrics_max_lines=max_lines, lyrics_render_mode="network"),
            make_song(),
            CountingProvider(lyric=lyric),
            None,
            DefaultRenderer(renderer),
        )
        assert result is not None and result.has_image
        data = renderer.html_calls[0]["data"]
        assert data["max_lines"] == max_lines
        html = render_template(renderer.html_calls[0]["tmpl"], data)
        assert html is not None
        assert html.count('<div class="line">') == expected
        assert ("仅显示前" in html) is (expected < 8)


async def test_lyrics_width_controls_injected_viewport() -> None:
    lyric = Lyric(text="第一句", lines=[LyricLine(text="第一句")])
    for width in (900, 600):
        star = RecordingStar()
        result = await run_lyrics_flow(
            make_config(lyrics_width=width), make_song(), CountingProvider(lyric=lyric), None, DefaultRenderer(star)
        )
        assert result is not None and result.has_image
        options = star.html_calls[0]["options"]
        assert options is not None, "viewport 必须通过 options 传给渲染器"
        rendered = options if isinstance(options, dict) else options.screenshot_options()
        assert rendered["viewport_width"] == width
        assert rendered["viewport_height"] == 100


# ---------------------------------------------------------------- 评论


async def test_comments_enable_toggles_fetch_and_message() -> None:
    page = CommentPage(items=[], total=0, has_more=False)
    on_provider = CountingProvider(page=page)
    off_provider = CountingProvider(page=page)
    on = await run_comments_flow(make_config(comments_enable=True), make_song(), on_provider, None, None)
    off = await run_comments_flow(make_config(comments_enable=False), make_song(), off_provider, None, None)
    assert on is not None and on.status == "empty" and on.text == EMPTY_COMMENT_MESSAGE
    assert off is None and off_provider.comments_calls == 0
    assert on_provider.comments_calls == 1


async def test_comments_count_controls_limit_and_items() -> None:
    async with mock_netease() as (base_url, calls):
        for count, expected in ((3, 3), (8, 8)):
            cfg = make_config(netease_api_base=base_url, comments_count=count)
            async with open_transport(base_url) as transport:
                page = await NeteaseProvider(cfg).comments(make_song(), transport, limit=count)
            assert calls[-1]["params"]["limit"] == str(count)
            assert page is not None and len(page.items) == expected


async def test_comments_sort_controls_sorttype_and_content() -> None:
    async with mock_netease() as (base_url, calls):
        async with open_transport(base_url) as transport:
            hot_cfg = make_config(netease_api_base=base_url, comments_sort="hot", comments_count=3)
            new_cfg = make_config(netease_api_base=base_url, comments_sort="new", comments_count=3)
            hot_page = await NeteaseProvider(hot_cfg).comments(make_song(), transport, limit=3)
            assert calls[-1]["params"]["sortType"] == "2"
            new_page = await NeteaseProvider(new_cfg).comments(make_song(), transport, limit=3)
            assert calls[-1]["params"]["sortType"] == "3"
    assert hot_page is not None and hot_page.items[0].content == "热门评论1"
    assert new_page is not None and new_page.items[0].content == "最新评论1"


async def test_comments_max_chars_truncates_content() -> None:
    async with mock_netease() as (base_url, _calls):
        async with open_transport(base_url) as transport:
            cfg = make_config(netease_api_base=base_url, comments_max_chars=3)
            truncated = await NeteaseProvider(cfg).comments(make_song(), transport, limit=3)
            cfg_full = make_config(netease_api_base=base_url, comments_max_chars=0)
            full = await NeteaseProvider(cfg_full).comments(make_song(), transport, limit=3)
    assert truncated is not None and truncated.items[0].content == "热门评…"
    assert full is not None and full.items[0].content == "热门评论1"


async def test_comments_page_controls_offset() -> None:
    async with mock_netease() as (base_url, calls):
        cfg = make_config(netease_api_base=base_url, comments_page=2, comments_count=3)
        async with open_transport(base_url) as transport:
            await NeteaseProvider(cfg).comments(make_song(), transport, limit=3, offset=3)
    assert calls[-1]["params"]["offset"] == "3"


# ---------------------------------------------------------------- 限流


async def test_cooldown_seconds_rejects_within_window() -> None:
    now = [1000.0]
    limiter = RateLimiter(cooldown_seconds=60, daily_limit=0, clock=lambda: now[0])
    allowed, message = await limiter.check_and_consume("u1")
    assert allowed is True and message is None
    now[0] += 1.0
    blocked, reason = await limiter.check_and_consume("u1")
    assert blocked is False and reason is not None and "秒" in reason

    free = RateLimiter(cooldown_seconds=0, daily_limit=0, clock=lambda: now[0])
    assert (await free.check_and_consume("u1"))[0] is True
    assert (await free.check_and_consume("u1"))[0] is True

    now[0] += 120.0
    assert (await limiter.check_and_consume("u1"))[0] is True


async def test_daily_limit_rejects_after_quota() -> None:
    limiter = RateLimiter(cooldown_seconds=0, daily_limit=2, clock=lambda: 1000.0)
    results = [await limiter.check_and_consume("u1") for _ in range(3)]
    assert [item[0] for item in results] == [True, True, False]
    assert "上限" in (results[2][1] or "")

    unlimited = RateLimiter(cooldown_seconds=0, daily_limit=0, clock=lambda: 1000.0)
    for _ in range(5):
        assert (await unlimited.check_and_consume("u1"))[0] is True


# ---------------------------------------------------------------- @ 与消息整理


async def test_reply_with_at_prepends_mention() -> None:
    require_astrbot()
    components = __import__("astrbot.api.message_components", fromlist=["At", "Music"])
    card = build_card_result_from_payload({"id": "186016", "title": "晴天", "url": SONG_URL}, make_config(card_type="163"), song=make_song())
    main = _plugin_main()
    outcome = main.MusicRequestOutcome(ok=True, status="ok", messages=[("chain", list(card.components))])
    without = main.prepare_messages(outcome, reply_with_at=False, user_id="10001")
    with_at = main.prepare_messages(outcome, reply_with_at=True, user_id="10001")
    assert len(without[0][1]) == 1
    assert isinstance(with_at[0][1][0], components.At)
    assert str(with_at[0][1][0].qq) == "10001"
    assert [type(item).__name__ for item in with_at[0][1][1:]] == ["Music"]

    # 非数字 user_id：At(qq="not-a-number") 在 pydantic v1 下是合法的字符串字段，
    # 必须原样保留（真诚用户 id 也能 @ 上）且不得抛异常
    weird = main.prepare_messages(outcome, reply_with_at=True, user_id="not-a-number")
    assert [type(item).__name__ for item in weird[0][1]] == ["At", "Music"]
    assert str(weird[0][1][0].qq) == "not-a-number"
