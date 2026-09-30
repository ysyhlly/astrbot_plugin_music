"""网易云数据面（本包由 provider-dev 独占写入，导入时不发起任何网络请求）。

模块划分
--------
- endpoints：端点路径常量 + 两种模式（自建 NeteaseCloudMusicApi / 官方直连）的请求构造，
  官方直连的已知限制写在模块 docstring；
- http：HttpTransport（aiohttp.ClientSession 封装：base_url / 超时 / 5xx 与网络异常重试 /
  UA / Cookie）与 fetch_json（兼容裸 aiohttp.ClientSession 的鸭子类型）；
- parser：纯函数解析层（搜索 / 歌词 / 评论 / 详情 + 歌手与歌名模糊匹配打分）；
- provider：MusicProvider 实现（key="netease"），由 core.provider.get_provider 惰性加载，
  模块级 PROVIDER 实例会被注册表直接复用，且从不关闭外部传入的 transport。
"""

from __future__ import annotations

from .endpoints import (
    MODE_OFFICIAL,
    MODE_SELF_HOSTED,
    EndpointRequest,
    build_audio_request,
    build_comments_request,
    build_detail_request,
    build_lyric_request,
    build_search_request,
    normalise_base_url,
    normalise_mode,
    picture_url,
    song_web_url,
)
from .http import HttpTransport, fetch_json
from .parser import (
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
from .provider import PROVIDER, NeteaseProvider, build_transport, register_provider

__all__ = [
    "MODE_OFFICIAL",
    "MODE_SELF_HOSTED",
    "PROVIDER",
    "EndpointRequest",
    "HttpTransport",
    "NeteaseProvider",
    "artist_match_score",
    "build_audio_request",
    "build_comments_request",
    "build_detail_request",
    "build_lyric_request",
    "build_search_request",
    "build_song",
    "build_transport",
    "extract_songs",
    "fetch_json",
    "name_match_score",
    "normalise_base_url",
    "normalise_mode",
    "parse_comments",
    "parse_lrc",
    "parse_lyric",
    "parse_search",
    "parse_song_detail",
    "picture_url",
    "prioritise_by_artist",
    "register_provider",
    "song_web_url",
]
