"""resolve_song / pick_song 选曲逻辑测试（禁止真实联网）。

覆盖：关键词规范化与 80 字截断、pick_strategy=top/first、歌手优先匹配、无结果返回 None、
provider 缺失/抛异常时的降级，以及走本地 aiohttp mock server 的端到端选曲（真实 provider）。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.config import RuntimeConfig
from core.models import MusicQuery, SongInfo
from core.netease.endpoints import (
    MODE_OFFICIAL,
    MODE_SELF_HOSTED,
    OFFICIAL_PATH_SEARCH,
    PATH_SEARCH,
)
from core.netease.http import HttpTransport
from core.netease.provider import NeteaseProvider
from core.provider import PROVIDER_REGISTRY
from core.search import (
    MAX_KEYWORD_CHARS,
    normalise_keyword,
    pick_song,
    resolve_song,
    search_songs,
)

SEARCH_JSON = """
{
  "result": {
    "songCount": 3,
    "songs": [
      {"id": 5097785, "name": "晴天 (女声版)", "ar": [{"id": 1001, "name": "某翻唱歌手"}],
       "al": {"id": 11, "name": "翻唱合集", "picUrl": "http://p2.music.126.net/cover2.jpg"}, "dt": 240000},
      {"id": 186016, "name": "晴天", "ar": [{"id": 6452, "name": "周杰伦"}],
       "al": {"id": 18917, "name": "叶惠美", "picUrl": "http://p1.music.126.net/cover.jpg"}, "dt": 269000},
      {"id": 999, "name": "晴天", "ar": [{"id": 2002, "name": "群星"}],
       "al": {"id": 12, "name": "合辑"}, "dt": 250000}
    ]
  },
  "code": 200
}
"""

EMPTY_SEARCH_JSON = '{"code": 200, "result": {"songCount": 0, "songs": []}}'


def make_config(**overrides: Any) -> RuntimeConfig:
    """构造测试用 RuntimeConfig（默认无重试、短超时）。"""
    base: dict[str, Any] = {
        "provider": "netease",
        "search_limit": 5,
        "pick_strategy": "top",
        "netease_mode": MODE_SELF_HOSTED,
        "max_retries": 0,
        "search_timeout": 3.0,
        "api_timeout": 3.0,
    }
    base.update(overrides)
    return RuntimeConfig.from_mapping(base)


def make_songs() -> list[SongInfo]:
    """固定候选集：歌手与歌名都刻意与关键词不完全一致。"""
    return [
        SongInfo(id="1", name="晴天 (女声版)", artists=["某翻唱歌手"]),
        SongInfo(id="2", name="晴天", artists=["周杰伦"]),
        SongInfo(id="3", name="晴天", artists=["群星"]),
    ]


class FakeProvider:
    """只实现协议所需方法的假 provider（记录收到的 query）。"""

    key = "netease"
    display_name = "假 provider"

    def __init__(self, songs: Any = None, error: Exception | None = None) -> None:
        self.songs = list(songs or [])
        self.error = error
        self.queries: list[MusicQuery] = []
        self.transports: list[Any] = []

    async def search(self, query: MusicQuery, transport: Any) -> list[SongInfo]:
        self.queries.append(query)
        self.transports.append(transport)
        if self.error is not None:
            raise self.error
        return list(self.songs)

    async def lyrics(self, song: Any, transport: Any) -> None:
        return None

    async def comments(self, song: Any, transport: Any, limit: int = 20, offset: int = 0) -> None:
        return None

    def card_payload(self, song: Any) -> dict[str, Any]:
        return {"kind": "music"}


@contextlib.asynccontextmanager
async def mock_api(routes: dict[str, Any]):
    """本地 mock 接口（仅 127.0.0.1）。"""
    calls: list[dict[str, Any]] = []

    async def handle(request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        if request.method == "POST":
            with contextlib.suppress(Exception):
                body = dict(await request.post())
        calls.append(
            {
                "method": request.method,
                "path": request.path,
                "params": dict(request.query),
                "body": body,
            }
        )
        spec = routes.get(request.path)
        if spec is None:
            return web.json_response({"code": 404, "msg": "not found"}, status=404)
        status, payload = spec
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield str(server.make_url("/")).rstrip("/"), calls
    finally:
        await server.close()


# ------------------------------------------------------------------ 关键词规范化


def test_normalise_keyword() -> None:
    assert normalise_keyword("  晴天  ") == "晴天"
    assert normalise_keyword("晴天   周杰伦") == "晴天 周杰伦"
    assert normalise_keyword(None) == ""
    assert normalise_keyword(True) == ""
    assert normalise_keyword(123) == "123"
    long_text = "歌" * 200
    assert len(normalise_keyword(long_text)) == MAX_KEYWORD_CHARS
    assert normalise_keyword(long_text) == "歌" * MAX_KEYWORD_CHARS
    assert normalise_keyword("x", max_chars=1) == "x"
    assert normalise_keyword("   ") == ""


# ------------------------------------------------------------------ pick_song


def test_pick_song_strategies() -> None:
    songs = make_songs()
    assert pick_song(songs, keyword="晴天", strategy="top").id == "1"
    assert pick_song(songs, keyword="晴天", strategy="first").id == "2"
    assert pick_song([], keyword="晴天") is None
    assert pick_song(None, keyword="晴天") is None
    assert pick_song([None, "x", SongInfo()], keyword="晴天") is None
    assert pick_song(songs, keyword="晴天", strategy="unexpected").id == "1"


def test_pick_song_prioritises_artist() -> None:
    songs = make_songs()
    assert pick_song(songs, keyword="晴天", artist="周杰伦", strategy="top").id == "2"
    assert pick_song(songs, keyword="晴天", artist="周杰伦", strategy="first").id == "2"
    assert pick_song(songs, keyword="晴天", artist="群星", strategy="top").id == "3"
    assert pick_song(songs, keyword="晴天", artist="群星", strategy="first").id == "3"
    assert pick_song(songs, keyword="晴天", artist="查无此人", strategy="top").id == "1"


# ------------------------------------------------------------------ resolve_song


async def test_resolve_song_top_and_first(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    transport = object()
    top = await resolve_song(make_config(pick_strategy="top"), transport, "晴天")
    assert top is not None and top.id == "1"
    first = await resolve_song(make_config(pick_strategy="first"), transport, "晴天")
    assert first is not None and first.id == "2"
    assert fake.transports == [transport, transport]
    assert [query.keyword for query in fake.queries] == ["晴天", "晴天"]
    assert [query.limit for query in fake.queries] == [5, 5]


async def test_resolve_song_artist_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    song = await resolve_song(make_config(), None, "晴天", "周杰伦")
    assert song is not None and song.id == "2"
    assert fake.queries[0].artist == "周杰伦"
    assert fake.queries[0].search_text == "晴天 周杰伦"


async def test_resolve_song_respects_search_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    songs = await search_songs(make_config(search_limit=2), None, "晴天")
    assert [song.id for song in songs] == ["1", "2"]
    assert fake.queries[0].limit == 2


async def test_resolve_song_truncates_long_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    await resolve_song(make_config(), None, "歌" * 200)
    assert len(fake.queries[0].keyword) == MAX_KEYWORD_CHARS


async def test_resolve_song_artist_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    song = await resolve_song(make_config(), None, "", "群星")
    assert song is not None and song.id == "3"
    assert fake.queries[0].keyword == "群星"
    assert fake.queries[0].artist == ""


async def test_resolve_song_without_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider([])
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    assert await resolve_song(make_config(), None, "不存在的歌") is None
    assert await resolve_song(make_config(), None, "   ") is None
    assert [query.keyword for query in fake.queries] == ["不存在的歌"]


async def test_resolve_song_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs(), error=RuntimeError("boom"))
    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", fake)
    assert await resolve_song(make_config(), None, "晴天") is None
    assert await search_songs(make_config(), None, "晴天") == []


async def test_resolve_song_missing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.search.get_provider", lambda key: None)
    assert await resolve_song(make_config(), None, "晴天") is None


async def test_resolve_song_injected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(make_songs())
    monkeypatch.setattr("core.search.get_provider", lambda key: None)
    song = await resolve_song(make_config(), None, "晴天", "周杰伦", provider=fake)
    assert song is not None and song.id == "2"


async def test_resolve_song_converts_mapping_results(monkeypatch: pytest.MonkeyPatch) -> None:
    class MappingProvider(FakeProvider):
        async def search(self, query: MusicQuery, transport: Any) -> list[Any]:
            self.queries.append(query)
            return [{"id": 7, "name": "晴天", "artists": [{"name": "周杰伦"}]}, {"name": ""}]

    monkeypatch.setitem(PROVIDER_REGISTRY, "netease", MappingProvider())
    song = await resolve_song(make_config(), None, "晴天", "周杰伦")
    assert song is not None and song.id == "7" and song.artists == ["周杰伦"]


# ------------------------------------------------------------------ 端到端


async def test_resolve_song_end_to_end_self_hosted() -> None:
    async with mock_api({PATH_SEARCH: (200, json.loads(SEARCH_JSON))}) as (base_url, calls):
        cfg = make_config(netease_api_base=base_url)
        async with HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            song = await resolve_song(cfg, transport, "晴天", "周杰伦")
    assert song is not None
    assert song.id == "186016"
    assert song.artists == ["周杰伦"]
    assert song.cover_url == "http://p1.music.126.net/cover.jpg"
    assert song.url == "https://music.163.com/song?id=186016"
    assert calls[0]["path"] == PATH_SEARCH
    assert calls[0]["params"]["keywords"] == "晴天 周杰伦"
    assert calls[0]["params"]["limit"] == "5"


async def test_resolve_song_end_to_end_first_strategy() -> None:
    async with mock_api({PATH_SEARCH: (200, json.loads(SEARCH_JSON))}) as (base_url, _calls):
        cfg = make_config(netease_api_base=base_url, pick_strategy="first")
        async with HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            song = await resolve_song(cfg, transport, "晴天")
    assert song is not None and song.id == "186016"


async def test_resolve_song_end_to_end_official_mode() -> None:
    async with mock_api({OFFICIAL_PATH_SEARCH: (200, json.loads(SEARCH_JSON))}) as (base_url, calls):
        cfg = make_config(netease_mode=MODE_OFFICIAL)
        async with HttpTransport(base_url=base_url, mode=MODE_OFFICIAL, max_retries=0) as transport:
            song = await resolve_song(cfg, transport, "晴天")
    assert song is not None and song.id == "5097785"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"]["s"] == "晴天"


async def test_resolve_song_end_to_end_no_result() -> None:
    async with mock_api({PATH_SEARCH: (200, json.loads(EMPTY_SEARCH_JSON))}) as (base_url, _calls):
        cfg = make_config(netease_api_base=base_url)
        async with HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            assert await resolve_song(cfg, transport, "不存在的歌") is None


async def test_resolve_song_end_to_end_with_registered_provider() -> None:
    async with mock_api({PATH_SEARCH: (200, json.loads(SEARCH_JSON))}) as (base_url, _calls):
        cfg = make_config(netease_api_base=base_url)
        provider = NeteaseProvider(cfg)
        PROVIDER_REGISTRY["netease"] = provider
        try:
            async with HttpTransport(base_url=base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
                song = await resolve_song(cfg, transport, "晴天", "群星")
            assert song is not None and song.id == "999"
        finally:
            PROVIDER_REGISTRY.pop("netease", None)
