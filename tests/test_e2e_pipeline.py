"""t12 端到端验证：真实 AstrBot 组件契约 + 本地 mock 网易云 API 全链路。

本文件回答两个问题：

1. main.py / core/cards 在生产配置下产出的消息链，能被 **真实 AstrBot 4.28.1** 的
   respond stage 判定为「非空且可发送」吗？
   - 判定函数直接复用 astrbot.core.pipeline.respond.stage.RespondStage._component_validators
     （依据文件 D:/项目/_astrbot_ref/astrbot/core/pipeline/respond/stage.py:21-50 的规则表，
      以及 stage.py:109-128 的 _is_empty_message_chain 逻辑）；
   - 额外断言平台识别层：Music 组件必须有 _type 且 toDict()["data"]["type"] 正确。
     （实测：pydantic.v1 的 BaseModel 默认 extra=ignore，Music(_type="163", ...) 会静默丢字段，
      respond stage 的校验器会抛 AttributeError 并被 stage.py:246 吞掉 —— 见本文件最后一个用例。）

2. 把 netease_api_base 指向本地假 NeteaseCloudMusicApi，全链路（搜索 → 歌词 → 评论 → 渲染）
   跑通时，各配置项是否真的控制输出。

单测不联网：HTTP 只打到 127.0.0.1 的 aiohttp TestServer；渲染器是记录型假对象。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_music.core.cards import build_card_result, build_card_result_from_payload, music_component_is_valid  # noqa: E402
from astrbot_plugin_music.core.config import RuntimeConfig  # noqa: E402
from astrbot_plugin_music.core.models import SongInfo  # noqa: E402
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
from astrbot_plugin_music.core.t2i.templates import LYRICS_TEMPLATE, render_template  # noqa: E402
from tests.conftest import require_astrbot  # noqa: E402

AUDIO_URL = "https://m8.music.126.net/song.mp3"
COVER_URL = "http://p1.music.126.net/cover.jpg"

LYRIC_LINES = 8
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


def _lrc() -> str:
    return "\n".join(f"[00:{index * 3:02d}.00]第{index + 1}句歌词" for index in range(LYRIC_LINES))


LYRIC_PAYLOAD = {
    "code": 200,
    "lrc": {"lyric": _lrc()},
    "tlyric": {"lyric": "[00:00.00]Line one"},
}

HOT_COMMENTS = [
    {
        "user": {"nickname": f"热门用户{index}", "avatarUrl": "http://a/x.jpg"},
        "content": f"热门评论{index}",
        "likedCount": 1000 + index,
        "time": 1580000000000,
    }
    for index in range(1, 7)
]
NORMAL_COMMENTS = [
    {
        "user": {"nickname": f"普通用户{index}"},
        "content": f"普通评论{index}",
        "likedCount": index,
        "time": 1590000000000,
    }
    for index in range(1, 5)
]
NEW_COMMENTS = [
    {
        "user": {"nickname": f"最新用户{index}"},
        "content": f"最新评论{index}",
        "likedCount": index,
        "time": 1600000000000,
    }
    for index in range(1, 5)
]

DETAIL_PAYLOAD = {
    "code": 200,
    "songs": [
        {
            "id": 186016,
            "name": "晴天",
            "ar": [{"name": "周杰伦"}],
            "al": {"name": "叶惠美", "picUrl": COVER_URL},
            "dt": 269000,
        }
    ],
}
AUDIO_PAYLOAD = {"code": 200, "data": [{"id": 186016, "url": AUDIO_URL}]}


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


@contextlib.asynccontextmanager
async def mock_netease(overrides: dict[str, Any] | None = None):
    """本地假 NeteaseCloudMusicApi（自建 API 形态），并记录全部请求。"""
    rules = dict(overrides or {})
    calls: list[dict[str, Any]] = []

    async def handle(request: web.Request) -> web.Response:
        calls.append({"path": request.path, "params": dict(request.query), "method": request.method})
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
            sort_type = str(request.query.get("sortType", "2") or "2")
            if sort_type == "3":  # 按时间排序：真实 API 只填 comments
                payload = {
                    "code": 200,
                    "total": 42,
                    "hotComments": [],
                    "comments": NEW_COMMENTS[:limit],
                    "more": True,
                }
            else:
                hot = HOT_COMMENTS[:limit]
                rest = NORMAL_COMMENTS[: max(0, limit - len(hot))]
                payload = {
                    "code": 200,
                    "total": 42,
                    "hotComments": hot,
                    "comments": rest,
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


class RecordingRenderer:
    """记录 render_html / render_markdown 收到的模板、数据与渲染参数。"""

    def __init__(self, html_result: Any = None, md_result: Any = None) -> None:
        self.html_calls: list[dict[str, Any]] = []
        self.md_calls: list[dict[str, Any]] = []
        self._html_result = html_result
        self._md_result = md_result

    @property
    def html_result(self) -> str:
        if self._html_result is not None:
            return self._html_result
        return f"https://img.example.com/html-{len(self.html_calls)}.png"

    @property
    def md_result(self) -> str:
        if self._md_result is not None:
            return self._md_result
        return f"https://img.example.com/md-{len(self.md_calls)}.png"

    async def render_html(self, template: str, data: Any = None, options: Any = None) -> Any:
        self.html_calls.append(
            {"template": template, "data": dict(data or {}), "options": options}
        )
        return self.html_result

    async def render_markdown(self, md: str, options: Any = None) -> Any:
        self.md_calls.append({"md": md, "options": options})
        return self.md_result


def _plugin_main():
    """延迟导入插件入口（缺 astrbot 时 skip 而不是收集期报错）。"""
    if not require_astrbot():
        pytest.skip("AstrBot 参考源码不可用")
    return importlib.import_module("astrbot_plugin_music.main")


def _respond_stage():
    if not require_astrbot():
        pytest.skip("AstrBot 参考源码不可用")
    return importlib.import_module("astrbot.core.pipeline.respond.stage")


def _components():
    if not require_astrbot():
        pytest.skip("AstrBot 参考源码不可用")
    return importlib.import_module("astrbot.api.message_components")


def chain_is_empty(chain: list[Any], validators: dict[Any, Any]) -> bool:
    """复刻 respond/stage.py:109-128 的 _is_empty_message_chain（用真实规则表）。"""
    if not chain:
        return True
    for component in chain:
        rule = validators.get(type(component))
        if rule is not None and rule(component):
            return False
    return True


# ---------------------------------------------------------------- 1. 组件契约


async def test_production_card_passes_real_respond_stage_validators() -> None:
    """生产路径产出的卡片：真实校验器通过 + 平台层可见 type + 三处口径一致。"""
    components_module = _components()
    stage_module = _respond_stage()
    validators = stage_module.RespondStage._component_validators
    music_rule = validators[components_module.Music]
    cfg = make_config(card_type="163")

    card = await build_card_result(make_song(), cfg, provider=None, transport=None)

    assert card.components, "生产路径必须产出组件（否则用户收不到卡片）"
    card_component = card.components[0]
    assert isinstance(card_component, components_module.Music)
    # (1) 实例上必须能读到 _type（pydantic v1 会丢构造参数，见 ensure_music_type）
    assert getattr(card_component, "_type", "") == "163"
    # (2) 平台适配器实际读的是 toDict()["data"]["type"]
    assert card_component.toDict()["data"]["type"] == "163"
    # (3) 真实 respond stage 校验器
    assert bool(music_rule(card_component)) is True
    # 插件自身的复刻规则必须与真实规则一致
    assert music_component_is_valid(card_component) is True


async def test_full_message_chain_passes_real_stage_checks() -> None:
    """卡片 + 歌词图 + 评论图组成的完整链：非空、无空 Plain、图片 file 非空。"""
    components_module = _components()
    stage_module = _respond_stage()
    validators = stage_module.RespondStage._component_validators
    cfg = make_config(card_type="163", reply_with_at=True)

    card = await build_card_result(make_song(), cfg, provider=None, transport=None)
    chain = [
        *card.components,
        components_module.Image(file="https://img.example.com/lyrics.png"),
        components_module.Image(file="https://img.example.com/comments.png"),
    ]

    assert chain_is_empty(chain, validators) is False, "respond stage 会认为整条链是空的"
    music = next(item for item in chain if isinstance(item, components_module.Music))
    assert bool(validators[components_module.Music](music)) is True
    images = [item for item in chain if isinstance(item, components_module.Image)]
    assert len(images) == 2
    assert all(str(image.file or "").strip() for image in images), "图片 file 不能为空"
    plains = [item for item in chain if isinstance(item, components_module.Plain)]
    assert [item for item in plains if not str(item.text or "").strip()] == [], "不允许空 Plain"


async def test_music_without_type_is_rejected_by_real_validator() -> None:
    """故障场景：_type 未写回时真实校验器抛 AttributeError（上游静默失效）。"""
    components_module = _components()
    stage_module = _respond_stage()
    validators = stage_module.RespondStage._component_validators
    music_rule = validators[components_module.Music]

    broken = components_module.Music(
        _type="163", id=186016, url="https://music.163.com/song?id=186016", title="晴天"
    )
    # 坏固件：pydantic v1 丢掉了 _type
    assert hasattr(broken, "_type") is False
    assert "type" not in broken.toDict()["data"]
    with pytest.raises(AttributeError):
        music_rule(broken)
    # 插件自己的复刻规则对同一对象不会抛异常，只是判定为不可发送
    assert music_component_is_valid(broken) is False

    # 生产路径绝不产出这种组件：三种卡片类型都必须能被真实校验器接受
    for card_type in ("163", "share"):
        cfg = make_config(card_type=card_type)
        card = await build_card_result(make_song(), cfg, provider=None, transport=None)
        assert card.components, card_type
        for component in card.components:
            rule = validators.get(type(component))
            if rule is not None:
                assert bool(rule(component)) is True, (card_type, type(component).__name__)

    custom_cfg = make_config(card_type="custom")
    custom = build_card_result_from_payload(
        {"id": "186016", "title": "晴天", "url": "https://music.163.com/song?id=186016", "audio": AUDIO_URL},
        custom_cfg,
        song=make_song(),
    )
    assert custom.components and getattr(custom.components[0], "_type", "") == "custom"
    assert bool(validators[components_module.Music](custom.components[0])) is True
    assert custom.components[0].toDict()["data"]["type"] == "custom"


# ---------------------------------------------------------------- 2. 全链路


async def test_end_to_end_self_hosted_full_chain() -> None:
    """搜索 → 歌词 → 评论 → 渲染 全链路（真实 provider + 真实传输层 + 本地 mock API）。"""
    main = _plugin_main()
    renderer = RecordingRenderer()
    async with mock_netease() as (base_url, calls):
        cfg = make_config(
            netease_api_base=base_url,
            search_limit=5,
            pick_strategy="top",
            comments_count=3,
            lyrics_max_lines=4,
            lyrics_width=900,
        )
        async with open_transport(base_url) as transport:
            outcome = await main.run_music_request(
                cfg,
                keyword="晴天",
                artist="周杰伦",
                provider=NeteaseProvider(cfg),
                transport=transport,
                renderer=renderer,
            )

    assert outcome.ok is True and outcome.status == "ok"
    assert outcome.song is not None and outcome.song.id == "186016"
    kinds = [kind for kind, _ in outcome.messages]
    assert kinds == ["chain", "image", "image"], outcome.to_dict()

    paths = [call["path"] for call in calls]
    assert paths.index(PATH_SEARCH) < paths.index(PATH_LYRIC) < paths.index(PATH_COMMENTS)

    # 卡片：真实组件 + 真实校验器
    card_chain = outcome.messages[0][1]
    assert card_chain and getattr(card_chain[0], "_type", "") == "163"

    # 歌词图：模板数据受 lyrics_max_lines 控制，且 viewport 已注入
    lyrics_call = renderer.html_calls[0]
    lyrics_data = lyrics_call["data"]
    assert lyrics_data["max_lines"] == 4
    assert lyrics_data["total_lines"] == LYRIC_LINES
    html = render_template(lyrics_call["template"], lyrics_data)
    assert html is not None
    assert html.count('<div class="line">') == 4
    assert "仅显示前 4 行" in html
    screenshot_options = lyrics_call["options"].screenshot_options()
    assert screenshot_options["viewport_width"] == 900
    assert screenshot_options["viewport_height"] == 100

    # 评论图：模板数据受 comments_count 控制
    comments_call = renderer.html_calls[1]
    assert len(comments_call["data"]["items"]) == 3
    assert comments_call["data"]["items"][0]["content"] == "热门评论1"


async def test_comments_count_and_lyrics_max_lines_follow_config() -> None:
    """同一链路下改两个键，观察 API 参数与渲染数据的变化。"""
    main = _plugin_main()
    renderer = RecordingRenderer()
    async with mock_netease() as (base_url, calls):
        cfg = make_config(netease_api_base=base_url, comments_count=8, lyrics_max_lines=8)
        async with open_transport(base_url) as transport:
            outcome = await main.run_music_request(
                cfg,
                keyword="晴天",
                provider=NeteaseProvider(cfg),
                transport=transport,
                renderer=renderer,
            )
    assert outcome.status == "ok"
    comment_call = next(call for call in calls if call["path"] == PATH_COMMENTS)
    assert comment_call["params"]["limit"] == "8"
    assert len(renderer.html_calls[1]["data"]["items"]) == 8
    assert renderer.html_calls[0]["data"]["max_lines"] == 8
    html = render_template(renderer.html_calls[0]["template"], renderer.html_calls[0]["data"])
    assert html is not None
    assert html.count('<div class="line">') == LYRIC_LINES
    assert "仅显示前" not in html

