"""网易云解析层 / HTTP 传输层 / Provider 单元测试（禁止真实联网）。

覆盖三类：
1. 解析器（parse_search / parse_lyric / parse_comments / parse_song_detail）的正常与畸形输入；
2. HttpTransport（本地 aiohttp mock server + 未监听端口）：超时、5xx 重试、4xx 不重试、
   非 JSON、非对象 JSON、业务 code、异步上下文管理器的关闭行为；
3. NeteaseProvider：两种模式的路径与参数、歌手优先排序、歌词/评论/详情/播放地址/卡片载荷。

所有 HTTP 都指向 127.0.0.1 的 aiohttp.test_utils 本地 mock server，或未监听的本地端口。
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import time
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.config import RuntimeConfig
from core.models import CommentPage, Lyric, LyricLine, MusicQuery, SongInfo
from core.netease.endpoints import (
    MODE_OFFICIAL,
    MODE_SELF_HOSTED,
    OFFICIAL_PATH_LYRIC,
    OFFICIAL_PATH_SEARCH,
    OFFICIAL_PATH_SEARCH_LEGACY,
    OFFICIAL_PATH_SONG_DETAIL,
    OFFICIAL_PATH_SONG_URL,
    PATH_COMMENTS,
    PATH_LYRIC,
    PATH_SEARCH,
    PATH_SONG_DETAIL,
    PATH_SONG_URL,
    build_audio_request,
    build_comments_request,
    build_detail_request,
    build_lyric_request,
    build_search_request,
    comment_sort_value,
    join_url,
    normalise_base_url,
    normalise_mode,
    picture_url,
    song_web_url,
)
from core.netease.http import HttpTransport, fetch_json
from core.netease.parser import (
    artist_match_score,
    build_song,
    extract_songs,
    name_match_score,
    parse_comments,
    parse_lrc,
    parse_lyric,
    parse_search,
    parse_song_detail,
    prioritise_by_artist,
)
from core.netease.provider import NeteaseProvider
from core.provider import PROVIDER_REGISTRY, Transport, get_provider

# ------------------------------------------------------------------ fixture

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

SEARCH_LEGACY_JSON = """
{
  "result": {
    "songCount": 1,
    "songs": [
      {"id": 186016, "name": "晴天", "artists": [{"id": 6452, "name": "周杰伦"}],
       "album": {"id": 18917, "name": "叶惠美"}, "duration": 269000}
    ]
  },
  "code": 200
}
"""

LYRIC_JSON = """
{
  "lrc": {"version": 1, "lyric": "[ar:周杰伦]\\n[ti:晴天]\\n[00:00.000] 作词 : 周杰伦\\n[00:01.00] 晴天\\n[00:02.50][00:05.50]故事的小黄花\\n[00:20.123]从出生那年就飘着\\n"},
  "tlyric": {"version": 1, "lyric": "[00:01.00] Sunny Day\\n[00:02.500] The little yellow flower\\n[00:20.100] Drifting since I was born\\n"},
  "code": 200
}
"""

LYRIC_PLAIN_JSON = """
{
  "lyric": "第一行\\n第二行\\n第三行",
  "tlyric": "Line one\\nLine two\\nLine three",
  "code": 200
}
"""

COMMENTS_JSON = """
{
  "hotComments": [
    {"commentId": 100, "user": {"nickname": "用户A", "avatarUrl": "http://a/1.jpg"},
     "content": "  热门   评论  ", "likedCount": 999, "time": 1600000000000},
    {"commentId": 2, "user": {"nickname": "用户B"}, "content": "", "likedCount": 5}
  ],
  "comments": [
    {"commentId": 1, "user": {"nickname": "用户A", "avatarUrl": "http://a/1.jpg"},
     "content": "很好听的一首歌", "likedCount": 123, "time": 1600000000000},
    {"commentId": 3, "user": {"nickname": "用户C"}, "content": "单曲循环中", "likedCount": 7,
     "time": 1600000000000, "beReplied": [{"user": {"nickname": "用户E"}, "content": "同感！", "likedCount": 1}]}
  ],
  "total": 42,
  "more": true,
  "code": 200
}
"""

DETAIL_JSON = """
{
  "songs": [
    {"id": 186016, "name": "晴天", "ar": [{"id": 6452, "name": "周杰伦"}],
     "al": {"id": 18917, "name": "叶惠美", "picUrl": "http://p1.music.126.net/cover.jpg"}, "dt": 269000}
  ],
  "code": 200
}
"""

AUDIO_JSON = """
{"code": 200, "data": [{"id": 186016, "url": "http://m8.music.126.net/song.mp3", "br": 128000}]}
"""


def load(raw: str) -> Any:
    """把内置 JSON 字符串转成对象。"""
    return json.loads(raw)


def make_config(**overrides: Any) -> RuntimeConfig:
    """构造测试用 RuntimeConfig（默认最小依赖、无重试、短超时）。"""
    base: dict[str, Any] = {
        "provider": "netease",
        "netease_mode": MODE_SELF_HOSTED,
        "search_limit": 5,
        "pick_strategy": "top",
        "max_retries": 0,
        "search_timeout": 3.0,
        "api_timeout": 3.0,
        "comments_max_chars": 0,
        "comments_sort": "hot",
    }
    base.update(overrides)
    return RuntimeConfig.from_mapping(base)


# ------------------------------------------------------------------ mock 服务


class MockApi:
    """本地 mock 接口：记录每次请求，按路径返回预置响应。"""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.base_url = ""

    async def handle(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        if request.method == "POST":
            with contextlib.suppress(Exception):
                body = dict(await request.post())
        index = self.counts.get(request.path, 0)
        self.counts[request.path] = index + 1
        self.calls.append(
            {
                "method": request.method,
                "path": request.path,
                "params": dict(request.query),
                "body": body,
                "headers": dict(request.headers),
                "index": index,
            }
        )
        spec = self.routes.get(request.path)
        if spec is None:
            return web.json_response({"code": 404, "msg": "not found"}, status=404)
        if callable(spec):
            spec = spec(index)
        status, payload = spec
        if isinstance(payload, (dict, list)):
            return web.json_response(payload, status=status)
        if payload is None:
            return web.Response(status=status, body=b"not-json", content_type="text/plain")
        return web.Response(status=status, text=str(payload), content_type="text/plain")

    def calls_of(self, path: str) -> list[dict[str, Any]]:
        """该路径上的全部请求。"""
        return [call for call in self.calls if call["path"] == path]


@contextlib.asynccontextmanager
async def mock_api(routes: dict[str, Any]):
    """启动本地 mock 接口（仅监听 127.0.0.1，用于替代真实联网）。"""
    api = MockApi(routes)
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", api.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        api.base_url = str(server.make_url("/")).rstrip("/")
        yield api
    finally:
        await server.close()


def closed_port() -> int:
    """取一个当前没有监听的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------------ endpoints


def test_normalise_mode_and_base_url() -> None:
    assert normalise_mode("self_hosted_api") == MODE_SELF_HOSTED
    assert normalise_mode("SELF_HOSTED") == MODE_SELF_HOSTED
    assert normalise_mode("official_direct") == MODE_OFFICIAL
    assert normalise_mode("unknown-value") == MODE_OFFICIAL
    assert normalise_mode(None) == MODE_OFFICIAL
    assert normalise_base_url("", mode=MODE_SELF_HOSTED) == "http://127.0.0.1:3000"
    assert normalise_base_url("", mode=MODE_OFFICIAL) == "https://music.163.com"
    assert normalise_base_url("127.0.0.1:3000/", mode=MODE_SELF_HOSTED) == "http://127.0.0.1:3000"
    assert normalise_base_url("https://api.example.com/", mode=MODE_OFFICIAL) == "https://api.example.com"
    assert join_url("http://x.test/", "/cloudsearch") == "http://x.test/cloudsearch"
    assert join_url("http://x.test", "http://y.test/z") == "http://y.test/z"
    assert song_web_url("186016") == "https://music.163.com/song?id=186016"
    assert song_web_url("") == ""


def test_picture_url_shape() -> None:
    url = picture_url(109951163011055531, size=200)
    assert url.startswith("https://p3.music.126.net/")
    assert url.endswith("/109951163011055531.jpg?param=200y200")
    segment = url.split("/")[3]
    assert "/" not in segment and "+" not in segment
    assert picture_url("http://p1.music.126.net/a.jpg") == "http://p1.music.126.net/a.jpg"
    assert picture_url("") == ""
    assert picture_url("not-an-id") == ""
    assert picture_url("123", size=0).endswith("/123.jpg")


def test_build_search_request_modes() -> None:
    self_hosted = build_search_request("晴天 周杰伦", limit=3, offset=1, mode=MODE_SELF_HOSTED)
    assert self_hosted.path == PATH_SEARCH
    assert self_hosted.method == "GET"
    assert self_hosted.params["keywords"] == "晴天 周杰伦"
    assert self_hosted.params["limit"] == 3
    assert self_hosted.params["offset"] == 1
    assert self_hosted.params["type"] == 1

    official = build_search_request("晴天", limit=2, mode=MODE_OFFICIAL)
    assert official.path == OFFICIAL_PATH_SEARCH
    assert official.method == "POST"
    assert official.params == {}
    assert official.data["s"] == "晴天"
    assert official.data["limit"] == "2"

    legacy = build_search_request("晴天", limit=2, mode=MODE_OFFICIAL, legacy=True)
    assert legacy.path == OFFICIAL_PATH_SEARCH_LEGACY
    assert legacy.method == "GET"
    assert legacy.params["s"] == "晴天"


def test_build_other_requests_modes() -> None:
    lyric = build_lyric_request(186016, mode=MODE_SELF_HOSTED)
    assert lyric.path == PATH_LYRIC and lyric.params["tv"] == -1 and lyric.params["id"] == "186016"
    assert build_lyric_request(186016, mode=MODE_OFFICIAL).path == OFFICIAL_PATH_LYRIC

    comments = build_comments_request(186016, limit=10, offset=0, sort="hot", mode=MODE_SELF_HOSTED)
    assert comments.path == PATH_COMMENTS and comments.params["sortType"] == 2
    assert comments.params["type"] == 0 and comments.params["pageSize"] == 10
    assert comments.params["pageNo"] == 1 and "offset" not in comments.params
    assert build_comments_request(1, sort="new", mode=MODE_SELF_HOSTED).params["sortType"] == 3
    official_comments = build_comments_request(186016, mode=MODE_OFFICIAL)
    assert official_comments.path == "/api/v1/resource/comments/R_SO_4_186016"
    assert "sortType" not in official_comments.params

    detail = build_detail_request(186016, mode=MODE_SELF_HOSTED)
    assert detail.path == PATH_SONG_DETAIL and detail.params["ids"] == "186016"
    assert build_detail_request(186016, mode=MODE_OFFICIAL).params["ids"] == "[186016]"

    audio = build_audio_request(186016, mode=MODE_SELF_HOSTED)
    assert audio.path == PATH_SONG_URL and audio.params["level"] == "standard"
    assert build_audio_request(186016, mode=MODE_OFFICIAL).path == OFFICIAL_PATH_SONG_URL
    assert comment_sort_value("hot") == 2 and comment_sort_value("new") == 3


# ------------------------------------------------------------------ 搜索解析


def test_parse_search_new_shape() -> None:
    songs = parse_search(load(SEARCH_JSON))
    assert [song.name for song in songs] == ["晴天 (女声版)", "晴天", "晴天"]
    first = songs[0]
    assert first.id == "5097785"
    assert first.artists == ["某翻唱歌手"]
    assert first.album == "翻唱合集"
    assert first.duration_ms == 240000
    assert first.cover_url == "http://p2.music.126.net/cover2.jpg"
    assert first.url == "https://music.163.com/song?id=5097785"
    assert first.provider_key == "netease"
    assert first.duration_text == "04:00"


def test_parse_search_legacy_shape_and_limit() -> None:
    songs = parse_search(load(SEARCH_LEGACY_JSON))
    assert len(songs) == 1
    assert songs[0].artists == ["周杰伦"]
    assert songs[0].duration_ms == 269000
    assert parse_search(load(SEARCH_JSON), limit=2) == parse_search(load(SEARCH_JSON))[:2]


def test_parse_search_keeps_entry_without_artists() -> None:
    payload = {"code": 200, "result": {"songs": [{"id": 7, "name": "无歌手条目", "al": {"name": "合辑"}}]}}
    songs = parse_search(payload)
    assert len(songs) == 1
    assert songs[0].artists == []
    assert songs[0].artist_text == ""


def test_parse_search_does_not_fabricate_cover_from_picture_id() -> None:
    """绝不用封面 id 拼 URL。

    历史回归：曾用 al.pic 拼 https://p3.music.126.net/<encrypt(pic)>/<pic>.jpg，
    但网易云的路径段与文件名是分别签发的，两者经常差几位，拼出来的是不存在的
    对象，服务端返回 HTTP 400 {"Code":"NotAnImage"}，表现为"封面没加载出来"。
    真实案例：愛言葉III 的 album.pic=109951170600289630 不可用，权威 picUrl 的
    文件名是 109951170600289625。

    现在缺封面时留空，由 provider 调 /song/detail 回填权威 picUrl。
    """
    payload = {"songs": [{"id": 7, "name": "有封面 id", "al": {"pic": 109951163011055531}}]}
    song = parse_search(payload)[0]
    assert song.cover_url == ""


def test_parse_search_uses_real_picurl_when_present() -> None:
    """接口给了真 URL 就直接用，不再二次加工。"""
    real = "https://p3.music.126.net/Nwj8XTsbHVdcyk637YROBw==/109951170600289625.jpg"
    payload = {
        "songs": [
            {"id": 7, "name": "有封面", "al": {"pic": 999, "picUrl": real}},
        ]
    }
    song = parse_search(payload)[0]
    assert song.cover_url == real


MALFORMED_SEARCH_CASES = [
    ("payload_is_none", None),
    ("payload_is_empty_dict", {}),
    ("payload_is_string", "boom"),
    ("songs_is_none", {"code": 200, "result": {"songs": None}}),
    ("songs_is_mapping", {"code": 200, "result": {"songs": {"x": 1}}}),
    ("artists_missing", {"code": 200, "result": {"songs": [{"id": 1}]}}),
    ("artists_is_none", {"code": 200, "result": {"songs": [{"id": 2, "name": "", "ar": None}]}}),
    ("entry_not_mapping", {"code": 200, "result": {"songs": [None, 7, "x"]}}),
    ("songs_empty_list", {"code": 200, "result": {"songs": []}}),
]


@pytest.mark.parametrize("payload", [case[1] for case in MALFORMED_SEARCH_CASES], ids=[case[0] for case in MALFORMED_SEARCH_CASES])
def test_parse_search_malformed(payload: Any) -> None:
    assert parse_search(payload) == []


def test_extract_songs_variants() -> None:
    assert extract_songs(None) == []
    assert extract_songs({"result": {"songs": [{"id": 1}]}}) == [{"id": 1}]
    assert extract_songs({"songs": [{"id": 2}]}) == [{"id": 2}]
    assert extract_songs({"data": {"songs": [{"id": 3}]}}) == [{"id": 3}]
    assert extract_songs({"song": {"id": 4, "name": "x"}}) == [{"id": 4, "name": "x"}]
    assert extract_songs({"songs": None}) == []


# ------------------------------------------------------------------ 歌词解析


def test_parse_lrc_timestamps() -> None:
    pairs = parse_lrc("[00:01.00]a\n[01:02.5]b\n[02:03.456]c\n[00:00]d")
    assert pairs == [(0, "d"), (1000, "a"), (62500, "b"), (123456, "c")]
    multi = parse_lrc("[00:02.50][00:05.50]同一句")
    assert multi == [(2500, "同一句"), (5500, "同一句")]
    assert parse_lrc("[ar:某人]\n[by:xx]\n[offset:0]") == []
    assert parse_lrc("") == []
    assert parse_lrc(None) == []


def test_parse_lyric_alignment() -> None:
    lyric = parse_lyric(load(LYRIC_JSON))
    assert isinstance(lyric, Lyric)
    assert len(lyric.lines) == 5
    assert [line.text for line in lyric.lines] == [
        "作词 : 周杰伦",
        "晴天",
        "故事的小黄花",
        "故事的小黄花",
        "从出生那年就飘着",
    ]
    assert [line.translation for line in lyric.lines] == [
        "",
        "Sunny Day",
        "The little yellow flower",
        "",
        "Drifting since I was born",
    ]
    assert lyric.line_count() == 5
    assert lyric.text.startswith("作词 : 周杰伦")
    assert "[00:01.00]" not in lyric.text
    assert lyric.translated == "Sunny Day\nThe little yellow flower\nDrifting since I was born"
    assert lyric.to_dict()["has_translation"] is True
    assert all(isinstance(line, LyricLine) for line in lyric.lines)


def test_parse_lyric_translation_multi_timestamp() -> None:
    payload = {
        "lrc": {"lyric": "[00:01.00][00:02.00]重复的一句"},
        "tlyric": {"lyric": "[00:01.00][00:02.00]Repeated line"},
    }
    lyric = parse_lyric(payload)
    assert [line.text for line in lyric.lines] == ["重复的一句", "重复的一句"]
    assert [line.translation for line in lyric.lines] == ["Repeated line", "Repeated line"]


def test_parse_lyric_without_translation() -> None:
    payload = {"code": 200, "lrc": {"lyric": "[00:01.00]only\n[00:02.00]lines"}, "tlyric": {"lyric": ""}}
    lyric = parse_lyric(payload)
    assert [line.translation for line in lyric.lines] == ["", ""]
    assert lyric.translated == ""


def test_parse_lyric_plain_text_aligned_by_order() -> None:
    lyric = parse_lyric(load(LYRIC_PLAIN_JSON))
    assert [line.text for line in lyric.lines] == ["第一行", "第二行", "第三行"]
    assert [line.translation for line in lyric.lines] == ["Line one", "Line two", "Line three"]


def test_parse_lyric_translation_tolerance_boundary() -> None:
    payload = {
        "lrc": {"lyric": "[00:10.000]line"},
        "tlyric": {"lyric": "[00:10.300]near"},
    }
    assert parse_lyric(payload).lines[0].translation == "near"
    far = {
        "lrc": {"lyric": "[00:10.000]line"},
        "tlyric": {"lyric": "[00:11.000]far"},
    }
    assert parse_lyric(far).lines[0].translation == ""


MALFORMED_LYRIC_CASES = [
    ("payload_is_none", None),
    ("payload_is_empty_dict", {}),
    ("payload_is_string", "boom"),
    ("lrc_is_none", {"code": 200, "lrc": None, "tlyric": None}),
    ("lrc_lyric_is_none", {"code": 200, "lrc": {"lyric": None}}),
    ("lrc_field_missing", {"code": 200, "tlyric": {"lyric": ""}}),
    ("lrc_empty_string", {"code": 200, "lrc": {"lyric": ""}}),
    ("lrc_only_tags", {"code": 200, "lrc": {"lyric": "[ar:某人]\n[ti:歌名]\n"}}),
]


@pytest.mark.parametrize("payload", [case[1] for case in MALFORMED_LYRIC_CASES], ids=[case[0] for case in MALFORMED_LYRIC_CASES])
def test_parse_lyric_malformed(payload: Any) -> None:
    lyric = parse_lyric(payload)
    assert lyric.is_empty() is True
    assert lyric.lines == []


# ------------------------------------------------------------------ 评论解析


def test_parse_comments_hot_order() -> None:
    page = parse_comments(load(COMMENTS_JSON))
    assert isinstance(page, CommentPage)
    assert [item.user for item in page.items] == ["用户A", "用户A", "用户C"]
    assert page.items[0].content == "热门 评论"
    assert page.items[0].liked == 999
    assert page.items[0].avatar_url == "http://a/1.jpg"
    assert page.total == 42
    assert page.has_more is True
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", page.items[0].time)
    assert page.items[2].replies[0].user == "用户E"
    assert page.items[2].replies[0].content == "同感！"


def test_parse_comments_new_sort_skips_hot() -> None:
    page = parse_comments(load(COMMENTS_JSON), sort="new")
    assert [item.content for item in page.items] == ["很好听的一首歌", "单曲循环中"]
    assert page.total == 42


def test_parse_comments_truncate_and_dedupe() -> None:
    page = parse_comments(load(COMMENTS_JSON), 6)
    assert page.items[0].content == "热门 评论"
    assert page.items[1].content == "很好听的一首…"
    assert len(page.items[1].content) == 7
    assert len(page.items) == 3
    dedupe = {
        "code": 200,
        "comments": [
            {"commentId": 1, "user": {"nickname": "A"}, "content": "同一条"},
            {"commentId": 1, "user": {"nickname": "A"}, "content": "同一条"},
        ],
    }
    assert len(parse_comments(dedupe).items) == 1


def test_parse_comments_has_more_fallback() -> None:
    page = parse_comments({"comments": [{"commentId": 1, "user": {"nickname": "A"}, "content": "x"}], "total": 5})
    assert page.has_more is True
    assert parse_comments({"comments": [], "total": 0}).has_more is False


def test_parse_comments_keeps_comment_without_user() -> None:
    page = parse_comments({"comments": [{"content": "匿名评论"}]})
    assert len(page.items) == 1
    assert page.items[0].user == ""
    assert page.items[0].content == "匿名评论"


MALFORMED_COMMENT_CASES = [
    ("payload_is_none", None),
    ("payload_is_empty_dict", {}),
    ("payload_is_string", "boom"),
    ("comments_empty_list", {"code": 200, "comments": [], "hotComments": []}),
    ("comments_is_none", {"code": 200, "comments": None, "hotComments": None}),
    ("comment_without_user", {"code": 200, "comments": [{"content": "", "likedCount": 3}]}),
    ("entries_not_mapping", {"code": 200, "comments": [None, 42, "x"]}),
    ("content_not_string", {"code": 200, "comments": [{"user": {"nickname": "A"}, "content": None}]}),
]


@pytest.mark.parametrize("payload", [case[1] for case in MALFORMED_COMMENT_CASES], ids=[case[0] for case in MALFORMED_COMMENT_CASES])
def test_parse_comments_malformed(payload: Any) -> None:
    page = parse_comments(payload)
    assert page.items == []
    assert page.total == 0


# ------------------------------------------------------------------ 详情解析


def test_parse_song_detail_fills_fields() -> None:
    base = SongInfo(id="186016", name="晴天")
    song = parse_song_detail(load(DETAIL_JSON), base)
    assert song.id == "186016"
    assert song.artists == ["周杰伦"]
    assert song.album == "叶惠美"
    assert song.duration_ms == 269000
    assert song.cover_url == "http://p1.music.126.net/cover.jpg"
    assert song.url == "https://music.163.com/song?id=186016"
    assert song.provider_key == "netease"


def test_parse_song_detail_keeps_existing_values() -> None:
    base = SongInfo(id="1", name="旧名", artists=["旧歌手"], album="旧专辑", cover_url="http://old.jpg", audio_url="http://a.mp3")
    song = parse_song_detail({"songs": [{"id": 1, "name": "新名"}]}, base)
    assert song.name == "新名"
    assert song.artists == ["旧歌手"]
    assert song.cover_url == "http://old.jpg"
    assert song.audio_url == "http://a.mp3"


def test_parse_song_detail_single_song_object_without_base() -> None:
    song = parse_song_detail({"song": {"id": 5, "name": "单曲"}}, None)
    assert song.id == "5" and song.name == "单曲"
    assert song.url == "https://music.163.com/song?id=5"


MALFORMED_DETAIL_CASES = [
    ("payload_is_none", None),
    ("payload_is_empty_dict", {}),
    ("payload_is_string", "boom"),
    ("songs_is_none", {"code": 200, "songs": None}),
    ("songs_empty_list", {"code": 200, "songs": []}),
    ("song_is_none", {"code": 200, "song": None}),
]


@pytest.mark.parametrize("payload", [case[1] for case in MALFORMED_DETAIL_CASES], ids=[case[0] for case in MALFORMED_DETAIL_CASES])
def test_parse_song_detail_malformed(payload: Any) -> None:
    base = SongInfo(id="186016", name="晴天")
    song = parse_song_detail(payload, base)
    assert isinstance(song, SongInfo)
    assert song.id == "186016" and song.name == "晴天"
    assert song.url == "https://music.163.com/song?id=186016"
    assert parse_song_detail(payload, None).id == ""


WEIRD_PAYLOADS = [
    None,
    {},
    [],
    (),
    "",
    0,
    3.5,
    True,
    b"bytes",
    {"result": {"songs": [{"id": None, "name": None, "ar": [None, {}]}]}},
    {"lrc": {"lyric": 123}, "tlyric": ["x"]},
    {"comments": [{"user": 5, "content": {}}]},
    {"songs": [{"al": "not-a-mapping", "ar": 5, "dt": "abc"}]},
    object(),
]


@pytest.mark.parametrize("payload", WEIRD_PAYLOADS)
def test_parsers_never_raise(payload: Any) -> None:
    assert isinstance(parse_search(payload), list)
    assert isinstance(parse_lyric(payload), Lyric)
    assert isinstance(parse_comments(payload), CommentPage)
    assert isinstance(parse_song_detail(payload, SongInfo(id="1", name="x")), SongInfo)
    assert isinstance(build_song(payload), SongInfo)
    assert isinstance(extract_songs(payload), list)


# ------------------------------------------------------------------ 匹配打分


def test_match_scores_and_prioritise() -> None:
    assert artist_match_score(["周杰伦"], "周杰伦") == 3
    assert artist_match_score(["周杰伦", "费玉清"], "费玉清") == 3
    assert artist_match_score(["周杰伦 / 费玉清"], "费玉清") == 2
    assert artist_match_score(["某人"], "周杰伦") == 0
    assert artist_match_score([], "周杰伦") == 0
    assert artist_match_score(["周杰伦"], "") == 0
    assert name_match_score("晴天", "晴天") == 3
    assert name_match_score("晴天 (女声版)", "晴天") == 2
    assert name_match_score("晴天", "雨天") == 0

    songs = parse_search(load(SEARCH_JSON))
    ordered = prioritise_by_artist(songs, "周杰伦")
    assert [song.id for song in ordered] == ["186016", "5097785", "999"]
    assert prioritise_by_artist(songs, "无此人") == songs
    assert prioritise_by_artist([], "周杰伦") == []


def test_build_song_malformed() -> None:
    assert build_song(None).name == ""
    assert build_song("x").id == ""
    assert build_song({"id": 3, "name": "名"}).url == "https://music.163.com/song?id=3"


# ------------------------------------------------------------------ HttpTransport


async def test_get_json_unreachable_returns_none_quickly() -> None:
    port = closed_port()
    transport = HttpTransport(
        base_url=f"http://127.0.0.1:{port}",
        mode=MODE_SELF_HOSTED,
        timeout=0.5,
        max_retries=0,
    )
    started = time.monotonic()
    async with transport:
        assert await transport.get_json(PATH_SEARCH, {"keywords": "晴天"}) is None
        assert await transport.post_json(PATH_LYRIC, data={"id": "1"}) is None
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert transport.closed is True


async def test_get_json_success_and_headers() -> None:
    async with mock_api({PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        transport = HttpTransport(
            base_url=api.base_url,
            mode=MODE_SELF_HOSTED,
            timeout=3.0,
            max_retries=0,
            cookie="MUSIC_U=token",
        )
        async with transport:
            payload = await transport.get_json(PATH_SEARCH, {"keywords": "晴天", "total": None})
    assert isinstance(payload, dict)
    call = api.calls_of(PATH_SEARCH)[0]
    assert call["params"]["keywords"] == "晴天"
    assert "total" not in call["params"]
    assert "Mozilla" in call["headers"]["User-Agent"]
    assert call["headers"]["Referer"] == "https://music.163.com/"
    assert call["headers"]["Cookie"] == "MUSIC_U=token"
    assert "cookie" not in call["params"]


async def test_post_json_sends_form_body() -> None:
    async with mock_api({OFFICIAL_PATH_SEARCH: (200, {"code": 200, "result": {"songs": []}})}) as api:
        transport = HttpTransport(base_url=api.base_url, mode=MODE_OFFICIAL, max_retries=0)
        async with transport:
            payload = await transport.post_json(OFFICIAL_PATH_SEARCH, {"s": "晴天", "type": 1})
    assert payload == {"code": 200, "result": {"songs": []}}
    call = api.calls_of(OFFICIAL_PATH_SEARCH)[0]
    assert call["method"] == "POST"
    assert call["body"] == {"s": "晴天", "type": "1"}
    assert "cookie" not in call["params"]


async def test_5xx_is_retried_with_exponential_backoff() -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with mock_api({PATH_SEARCH: (500, {"code": 500, "msg": "boom"})}) as api:
        transport = HttpTransport(
            base_url=api.base_url,
            mode=MODE_SELF_HOSTED,
            max_retries=2,
            retry_backoff=0.4,
            sleep=fake_sleep,
        )
        async with transport:
            assert await transport.get_json(PATH_SEARCH) is None
    assert len(api.calls_of(PATH_SEARCH)) == 3
    assert delays == [0.4, 0.8]


async def test_5xx_then_success() -> None:
    async with mock_api(
        {PATH_SEARCH: lambda index: (500, {"code": 500}) if index == 0 else (200, load(SEARCH_JSON))}
    ) as api:
        transport = HttpTransport(
            base_url=api.base_url,
            mode=MODE_SELF_HOSTED,
            max_retries=2,
            retry_backoff=0,
        )
        async with transport:
            payload = await transport.get_json(PATH_SEARCH)
    assert payload is not None and payload["code"] == 200
    assert len(api.calls_of(PATH_SEARCH)) == 2


async def test_4xx_is_not_retried() -> None:
    async with mock_api({PATH_SEARCH: (404, {"code": 404, "msg": "nope"})}) as api:
        transport = HttpTransport(
            base_url=api.base_url,
            mode=MODE_SELF_HOSTED,
            max_retries=3,
            retry_backoff=0,
        )
        async with transport:
            assert await transport.get_json(PATH_SEARCH) is None
    assert len(api.calls_of(PATH_SEARCH)) == 1


async def test_invalid_json_and_non_object_json_return_none() -> None:
    async with mock_api(
        {
            "/bad-json": (200, None),
            "/array-json": (200, [1, 2, 3]),
            "/string-json": (200, "plain"),
        }
    ) as api:
        transport = HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0)
        async with transport:
            assert await transport.get_json("/bad-json") is None
            assert await transport.get_json("/array-json") is None
            assert await transport.get_json("/string-json") is None
            assert await transport.get_json("") is None
            assert await transport.get_json("/missing") is None


async def test_business_code_check() -> None:
    async with mock_api({"/err-code": (200, {"code": 404, "msg": "接口异常"})}) as api:
        transport = HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0)
        async with transport:
            assert await transport.get_json("/err-code") is None
            assert await transport.get_json("/err-code", check_code=False) == {"code": 404, "msg": "接口异常"}


async def test_context_manager_keeps_injected_session_open() -> None:
    async with mock_api({PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        async with aiohttp.ClientSession() as session:
            transport = HttpTransport(session=session, base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0)
            async with transport:
                assert transport.session is session
                assert await transport.get_json(PATH_SEARCH) is not None
            assert session.closed is False
        assert session.closed is True


async def test_fetch_endpoint_request_and_tuple() -> None:
    async with mock_api({PATH_LYRIC: (200, load(LYRIC_JSON))}) as api:
        transport = HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0)
        async with transport:
            payload = await transport.fetch(build_lyric_request("186016", mode=MODE_SELF_HOSTED))
            assert payload is not None and "lrc" in payload
            assert await transport.fetch((PATH_SEARCH, {"keywords": "x"})) is None
            assert await transport.fetch(None) is None


async def test_fetch_json_accepts_duck_typed_transport() -> None:
    class FakeTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        async def get_json(self, path: str, params: Any = None) -> dict[str, Any]:
            self.calls.append((path, params))
            return {"code": 200, "ok": True}

        async def post_json(self, path: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((path, data))
            return {"code": 200, "posted": True}

    fake = FakeTransport()
    get_payload = await fetch_json(fake, build_lyric_request("1", mode=MODE_SELF_HOSTED))
    post_payload = await fetch_json(fake, build_search_request("x", mode=MODE_OFFICIAL))
    assert get_payload == {"code": 200, "ok": True}
    assert post_payload == {"code": 200, "posted": True}
    assert fake.calls[0][0] == PATH_LYRIC
    assert fake.calls[1][0] == OFFICIAL_PATH_SEARCH
    assert await fetch_json(None, build_lyric_request("1")) is None
    assert await fetch_json(fake, "??") is None


def test_transport_from_config_and_protocol_shape() -> None:
    cfg = make_config(netease_api_base="http://127.0.0.1:4000/", netease_mode=MODE_SELF_HOSTED, api_timeout=7.5, max_retries=1, cookie="a=b", user_agent="UA/1.0")
    transport = HttpTransport.from_config(cfg)
    assert transport.base_url == "http://127.0.0.1:4000"
    assert transport.mode == MODE_SELF_HOSTED
    assert transport.timeout == 7.5
    assert transport.max_retries == 1
    assert transport.cookie == "a=b"
    assert transport.user_agent == "UA/1.0"
    assert transport.get_json is not None
    assert isinstance(HttpTransport(), Transport)
    assert HttpTransport().base_url == "https://music.163.com"
    assert HttpTransport(mode=MODE_SELF_HOSTED).base_url == "http://127.0.0.1:3000"


# ------------------------------------------------------------------ Provider


def test_get_provider_returns_netease_provider() -> None:
    provider = get_provider("netease")
    assert isinstance(provider, NeteaseProvider)
    assert provider.key == "netease"
    assert provider.display_name
    assert callable(provider.search)
    assert callable(provider.lyrics)
    assert callable(provider.comments)
    assert callable(provider.card_payload)
    assert PROVIDER_REGISTRY["netease"] is provider
    assert get_provider("unknown-provider") is None


def test_card_payload_defaults() -> None:
    provider = NeteaseProvider(make_config())
    song = SongInfo(id="186016", name="晴天", artists=["周杰伦"], album="叶惠美", cover_url="http://c.jpg", duration_ms=269000)
    payload = provider.card_payload(song)
    assert set(payload) == {"kind", "type", "id", "title", "content", "image", "url", "audio"}
    assert payload["kind"] == "music"
    assert payload["type"] == "163"
    assert payload["id"] == "186016"
    assert payload["title"] == "晴天"
    assert payload["content"] == "周杰伦 · 叶惠美 · 网易云音乐"
    assert payload["image"] == "http://c.jpg"
    assert payload["url"] == "https://music.163.com/song?id=186016"
    assert payload["audio"] == ""


def test_card_payload_share_variant() -> None:
    cfg = make_config(card_type="share", card_show_cover=False, card_attach_audio=True, card_show_source=False)
    provider = NeteaseProvider(cfg)
    song = SongInfo(id="1", name="歌", artists=["A"], audio_url="http://a.mp3")
    payload = provider.card_payload(song)
    assert payload["kind"] == "share"
    assert payload["type"] == "share"
    assert payload["image"] == ""
    assert payload["audio"] == "http://a.mp3"
    assert payload["content"] == "A"
    assert NeteaseProvider(make_config(card_type="custom")).card_payload(song)["kind"] == "music"
    assert NeteaseProvider(make_config(card_type="weird")).card_payload(song)["type"] == "163"


async def test_provider_search_self_hosted() -> None:
    async with mock_api({PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            songs = await provider.search(MusicQuery(keyword="晴天", limit=3), transport)
    assert [song.id for song in songs] == ["5097785", "186016", "999"]
    call = api.calls_of(PATH_SEARCH)[0]
    assert call["params"]["keywords"] == "晴天"
    assert call["params"]["limit"] == "3"


async def test_provider_search_prioritises_artist() -> None:
    async with mock_api({PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url, search_limit=5))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            songs = await provider.search(MusicQuery(keyword="晴天", artist="周杰伦"), transport)
    assert [song.id for song in songs] == ["186016", "5097785", "999"]
    assert api.calls_of(PATH_SEARCH)[0]["params"]["keywords"] == "晴天 周杰伦"


async def test_provider_search_official_posts_to_cloudsearch() -> None:
    async with mock_api({OFFICIAL_PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        provider = NeteaseProvider(make_config(netease_mode=MODE_OFFICIAL))
        async with HttpTransport(base_url=api.base_url, mode=MODE_OFFICIAL, max_retries=0) as transport:
            songs = await provider.search(MusicQuery(keyword="晴天"), transport)
    assert len(songs) == 3
    call = api.calls_of(OFFICIAL_PATH_SEARCH)[0]
    assert call["method"] == "POST"
    assert call["body"]["s"] == "晴天"
    assert call["body"]["type"] == "1"
    assert api.calls_of(OFFICIAL_PATH_SEARCH_LEGACY) == []


async def test_provider_search_official_legacy_fallback() -> None:
    async with mock_api(
        {
            OFFICIAL_PATH_SEARCH: (200, {"code": 200, "result": {"songs": []}}),
            OFFICIAL_PATH_SEARCH_LEGACY: (200, load(SEARCH_LEGACY_JSON)),
        }
    ) as api:
        provider = NeteaseProvider(make_config(netease_mode=MODE_OFFICIAL))
        async with HttpTransport(base_url=api.base_url, mode=MODE_OFFICIAL, max_retries=0) as transport:
            songs = await provider.search(MusicQuery(keyword="晴天"), transport)
    assert [song.id for song in songs] == ["186016"]
    assert len(api.calls_of(OFFICIAL_PATH_SEARCH)) == 1
    assert len(api.calls_of(OFFICIAL_PATH_SEARCH_LEGACY)) == 1
    assert api.calls_of(OFFICIAL_PATH_SEARCH_LEGACY)[0]["params"]["s"] == "晴天"


async def test_provider_search_failures_return_empty_list() -> None:
    provider = NeteaseProvider(make_config(netease_api_base=f"http://127.0.0.1:{closed_port()}"))
    transport = HttpTransport(base_url=provider.config.netease_api_base, mode=MODE_SELF_HOSTED, timeout=0.5, max_retries=0)
    async with transport:
        assert await provider.search(MusicQuery(keyword="晴天"), transport) == []
    assert await provider.search(MusicQuery(keyword="   "), transport) == []
    assert await provider.search(MusicQuery(keyword="x"), None) == []


async def test_provider_lyrics() -> None:
    async with mock_api({PATH_LYRIC: (200, load(LYRIC_JSON))}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            lyric = await provider.lyrics(SongInfo(id="186016", name="晴天"), transport)
            assert lyric is not None
            assert len(lyric.lines) == 5
            assert lyric.lines[1].translation == "Sunny Day"
            empty = await provider.lyrics(SongInfo(id="", name="无 id"), transport)
            assert empty is None
    params = api.calls_of(PATH_LYRIC)[0]["params"]
    assert params["id"] == "186016" and params["tv"] == "-1" and params["lv"] == "-1"


async def test_provider_lyrics_missing_or_failed() -> None:
    async with mock_api({PATH_LYRIC: (200, {"code": 200, "lrc": {"lyric": ""}, "tlyric": {"lyric": ""}})}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            assert await provider.lyrics(SongInfo(id="1"), transport) is None
            assert await provider.lyrics(SongInfo(id="1"), None) is None


async def test_provider_comments_hot_and_new() -> None:
    old = load(COMMENTS_JSON)
    hot_items = [item for item in old["hotComments"] + old["comments"] if item["content"]]
    hot = {"code": 200, "data": {"comments": hot_items, "totalCount": 42, "hasMore": True}}
    new = {"code": 200, "data": {"comments": old["comments"], "totalCount": 42, "hasMore": True}}
    async with mock_api({PATH_COMMENTS: lambda index: (200, hot if index == 0 else new)}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url, comments_sort="hot", comments_max_chars=6))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            page = await provider.comments(SongInfo(id="186016"), transport, limit=3)
            assert page is not None
            assert [item.content for item in page.items] == ["热门 评论", "很好听的一首…", "单曲循环中"]
            assert page.total == 42 and page.has_more is True
            new_page = await provider.comments(SongInfo(id="186016"), transport, limit=3, sort="new")
            assert new_page is not None
            assert len(new_page.items) == 2
            assert await provider.comments(SongInfo(id=""), transport) is None
    first = api.calls_of(PATH_COMMENTS)[0]["params"]
    assert first["pageSize"] == "3" and first["sortType"] == "2" and first["pageNo"] == "1"
    assert api.calls_of(PATH_COMMENTS)[1]["params"]["sortType"] == "3"


async def test_provider_comments_failure_returns_none() -> None:
    async with mock_api({PATH_COMMENTS: (500, {"code": 500})}) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            assert await provider.comments(SongInfo(id="1"), transport) is None


async def test_provider_song_detail_and_audio() -> None:
    async with mock_api(
        {PATH_SONG_DETAIL: (200, load(DETAIL_JSON)), PATH_SONG_URL: (200, load(AUDIO_JSON))}
    ) as api:
        provider = NeteaseProvider(make_config(netease_api_base=api.base_url))
        async with HttpTransport(base_url=api.base_url, mode=MODE_SELF_HOSTED, max_retries=0) as transport:
            song = await provider.song_detail(SongInfo(id="186016", name="晴天"), transport)
            assert song.cover_url == "http://p1.music.126.net/cover.jpg"
            assert song.duration_ms == 269000
            url = await provider.audio_url(SongInfo(id="186016"), transport)
            assert url == "http://m8.music.126.net/song.mp3"
            assert await provider.audio_url(SongInfo(id=""), transport) == ""
    assert api.calls_of(PATH_SONG_DETAIL)[0]["params"]["ids"] == "186016"


async def test_provider_accepts_raw_client_session() -> None:
    async with mock_api({PATH_SEARCH: (200, load(SEARCH_JSON))}) as api:
        cfg = make_config(netease_api_base=api.base_url, max_retries=0)
        provider = NeteaseProvider(cfg)
        async with aiohttp.ClientSession() as session:
            songs = await provider.search(MusicQuery(keyword="晴天"), session)
            assert len(songs) == 3
            assert session.closed is False
    assert api.calls_of(PATH_SEARCH)[0]["params"]["keywords"] == "晴天"
