"""网易云接口端点常量与请求构造（自建 API / 官方直连两种模式）。

模式语义
--------
- self_hosted_api：自建 NeteaseCloudMusicApi（默认 http://127.0.0.1:3000），
  路径为 /cloudsearch、/lyric、/comment/new、/song/detail、/song/url/v1，
  一律 GET + 查询参数。
- official_direct：官方站点直连（https://music.163.com），路径为
  /api/cloudsearch/pc、/api/song/lyric、/api/v1/resource/comments/R_SO_4_<id>、
  /api/song/detail、/api/song/enhance/player/url。

官方直连模式的已知限制（无法靠参数绕过，配置说明与用户提示需据此撰写）
----------------------------------------------------------------------
1. 评论：只有 /api/v1/resource/comments/R_SO_4_<id> 可用，只按「推荐」序返回，
   sortType 参数无效（comments_sort=new 在官方直连下不可靠）；绝大多数歌曲需要
   有效 Cookie（MUSIC_U）才返回内容，缺 Cookie 时常见空数组或 code=250/301，
   被风控时返回 403/460。响应里 hotComments 与 comments 不做区分。
2. 歌词：lrc 与 tlyric（翻译）通常可取；klyric（逐字）/ romalrc（罗马音）/ yrc
   在官方直连下拿不到；无版权歌曲返回空 lyric。
3. 搜索：/api/cloudsearch/pc 返回新字段形态（al / ar / dt，含专辑封面 picUrl）；
   旧接口 /api/search/get 返回老形态（album / artists / duration）且不含封面，
   需要再用 /api/song/detail 补全。官方直连偶发 code=460（风控）。
4. 详情：/api/song/detail 需要 ids=[id] 形式的 JSON 数组串，单次建议 <= 10 个 id，
   返回体不含播放地址。
5. 播放地址：/api/song/enhance/player/url 需要 Cookie，且多数歌曲返回 code=403/404
   的空 url —— 官方直连基本拿不到 SongInfo.audio_url。
6. 官方直连没有「按时间排序评论」「逐字歌词」「音质选择」「歌曲可用性」能力；
   需要这些能力请把 netease_mode 设为 self_hosted_api 并填写 netease_api_base。
7. 官方直连必须带浏览器 UA / Referer（HttpTransport 默认已带），否则容易被拒。
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "MODES",
    "MODE_OFFICIAL",
    "MODE_SELF_HOSTED",
    "OFFICIAL_BASE_URL",
    "OFFICIAL_COMMENT_PATH_TEMPLATE",
    "OFFICIAL_PATH_LYRIC",
    "OFFICIAL_PATH_SEARCH",
    "OFFICIAL_PATH_SEARCH_LEGACY",
    "OFFICIAL_PATH_SONG_DETAIL",
    "OFFICIAL_PATH_SONG_URL",
    "OFFICIAL_REFERER",
    "PATH_COMMENTS",
    "PATH_COMMENTS_HOT",
    "PATH_COMMENTS_MUSIC",
    "PATH_LYRIC",
    "PATH_SEARCH",
    "PATH_SEARCH_SIMPLE",
    "PATH_SONG_DETAIL",
    "PATH_SONG_URL",
    "PATH_SONG_URL_LEGACY",
    "SELF_HOSTED_DEFAULT_BASE_URL",
    "EndpointRequest",
    "build_audio_request",
    "build_comments_request",
    "build_classic_comments_request",
    "build_detail_request",
    "build_lyric_request",
    "build_search_request",
    "comment_sort_value",
    "is_self_hosted",
    "join_url",
    "normalise_base_url",
    "normalise_mode",
    "picture_url",
    "request_url",
    "song_web_url",
]

MODE_OFFICIAL = "official_direct"
"""官方站点直连模式（默认）。"""

MODE_SELF_HOSTED = "self_hosted_api"
"""自建 NeteaseCloudMusicApi 模式。"""

MODES: tuple[str, ...] = (MODE_OFFICIAL, MODE_SELF_HOSTED)
"""全部合法模式。"""

_MODE_ALIASES: dict[str, str] = {
    "official": MODE_OFFICIAL,
    "official_direct": MODE_OFFICIAL,
    "direct": MODE_OFFICIAL,
    "netease": MODE_OFFICIAL,
    "self_hosted": MODE_SELF_HOSTED,
    "self_hosted_api": MODE_SELF_HOSTED,
    "selfhosted": MODE_SELF_HOSTED,
    "selfhostedapi": MODE_SELF_HOSTED,
    "api": MODE_SELF_HOSTED,
    "local": MODE_SELF_HOSTED,
}

OFFICIAL_BASE_URL = "https://music.163.com"
"""官方直连主机。"""

SELF_HOSTED_DEFAULT_BASE_URL = "http://127.0.0.1:3000"
"""自建 NeteaseCloudMusicApi 的默认地址（netease_api_base 为空时使用）。"""

OFFICIAL_REFERER = "https://music.163.com/"
"""官方直连必须携带的 Referer。"""

DEFAULT_TIMEOUT = 10.0
"""默认请求超时（秒）。"""

DEFAULT_MAX_RETRIES = 2
"""默认重试次数（3xx/4xx 不重试；5xx 与网络异常会退避重试）。"""

# ------------------------------------------------------------ 自建 API 路径常量
PATH_SEARCH = "/cloudsearch"
PATH_SEARCH_SIMPLE = "/search"
PATH_LYRIC = "/lyric"
PATH_COMMENTS = "/comment/new"
PATH_COMMENTS_HOT = "/comment/hot"
PATH_COMMENTS_MUSIC = "/comment/music"
PATH_SONG_DETAIL = "/song/detail"
PATH_SONG_URL = "/song/url/v1"
PATH_SONG_URL_LEGACY = "/song/url"

# ------------------------------------------------------------ 官方直连路径常量
OFFICIAL_PATH_SEARCH = "/api/cloudsearch/pc"
OFFICIAL_PATH_SEARCH_LEGACY = "/api/search/get"
OFFICIAL_PATH_LYRIC = "/api/song/lyric"
OFFICIAL_PATH_SONG_DETAIL = "/api/song/detail"
OFFICIAL_PATH_SONG_URL = "/api/song/enhance/player/url"
OFFICIAL_COMMENT_PATH_TEMPLATE = "/api/v1/resource/comments/R_SO_4_{song_id}"
"""官方评论路径模板（R_SO_4_ 表示单曲资源类型）。"""

_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_PICTURE_MAGIC = "3go8&$8*3*3h0k(2)2"
"""网易云图片域名加密用的社区已知魔数。"""


@dataclass(frozen=True)
class EndpointRequest:
    """一次接口调用的描述（路径 + 参数 + 方法 + 表单体 + 超时）。

    路径是相对路径（不含主机），由 HttpTransport 用自身 base_url 拼接；
    method=POST 时 params 作为查询串、data 作为表单体。
    """

    path: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    method: str = "GET"
    data: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None

    @property
    def is_post(self) -> bool:
        """是否 POST 请求。"""
        return self.method.upper() == "POST"

    def to_dict(self) -> dict[str, Any]:
        """调试用普通字典。"""
        return {
            "path": self.path,
            "params": dict(self.params),
            "method": self.method,
            "data": dict(self.data),
            "timeout": self.timeout,
        }


def _int(value: Any, default: int = 0) -> int:
    """安全转 int（失败给默认值）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def normalise_mode(mode: Any) -> str:
    """把任意值规范成 official_direct / self_hosted_api，未知值回退官方直连。"""
    text = str(mode or "").strip().lower()
    return _MODE_ALIASES.get(text, MODE_OFFICIAL)


def is_self_hosted(mode: Any) -> bool:
    """是否自建 API 模式。"""
    return normalise_mode(mode) == MODE_SELF_HOSTED


def normalise_base_url(base: Any, *, mode: Any = MODE_OFFICIAL) -> str:
    """规范化 base_url：去空白与尾部斜杠，缺 scheme 时补 http://；空值给模式默认主机。"""
    text = str(base or "").strip()
    if not text:
        return SELF_HOSTED_DEFAULT_BASE_URL if is_self_hosted(mode) else OFFICIAL_BASE_URL
    if "://" not in text:
        text = "http://" + text
    return text.rstrip("/")


def join_url(base: Any, path: Any) -> str:
    """把 base_url 与相对路径拼成完整 URL（path 已是完整 URL 时原样返回）。"""
    text = str(path or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    prefix = str(base or "").rstrip("/")
    if not text:
        return prefix
    return prefix + "/" + text.lstrip("/")


def song_web_url(song_id: Any) -> str:
    """歌曲网页外链（卡片跳转用）；id 为空时返回空串。"""
    text = str(song_id or "").strip()
    return f"https://music.163.com/song?id={text}" if text else ""


def _encrypt_picture_id(pic_id: str) -> str:
    """按社区已知算法计算网易云图片域名片段。"""
    magic = _PICTURE_MAGIC
    salted = "".join(
        chr(ord(char) ^ ord(magic[index % len(magic)]))
        for index, char in enumerate(pic_id)
    )
    digest = hashlib.md5(salted.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii").replace("/", "_").replace("+", "-")


def picture_url(pic_id: Any, *, size: int = 300) -> str:
    """由封面 id（al.pic）拼出封面直链；已是 URL 时原样返回。

    仅用于搜索结果缺 picUrl 的兜底（旧 /api/search/get 形态）。注意官方图片有防盗链，
    个别图片可能 403，因此只作为兜底而不覆盖接口已给出的 picUrl。
    """
    text = str(pic_id or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if not text.isdigit():
        return ""
    suffix = f"?param={size}y{size}" if size and size > 0 else ""
    return f"https://p3.music.126.net/{_encrypt_picture_id(text)}/{text}.jpg{suffix}"


def _safe_id(value: Any) -> str:
    """把歌曲 id 转成可安全放进路径的字符串。"""
    return _ID_SAFE_RE.sub("", str(value or "").strip())


def comment_sort_value(sort: Any) -> int:
    """评论排序映射到 NeteaseCloudMusicApi 的 sortType（1 推荐 / 2 热度 / 3 时间）。"""
    text = str(sort or "").strip().lower()
    if text in {"new", "time", "latest", "3"}:
        return 3
    if text in {"rec", "recommend", "1"}:
        return 1
    return 2


def build_search_request(
    keyword: Any,
    *,
    limit: int = 10,
    offset: int = 0,
    mode: Any = MODE_OFFICIAL,
    legacy: bool = False,
    timeout: float | None = None,
) -> EndpointRequest:
    """构造搜索请求（自建 GET /cloudsearch；官方 POST /api/cloudsearch/pc）。

    legacy=True 时官方模式改用旧的 GET /api/search/get（老字段形态、无封面）。
    """
    text = str(keyword or "").strip()
    count = max(1, min(_int(limit, 10), 100))
    start = max(0, _int(offset, 0))
    if is_self_hosted(mode):
        return EndpointRequest(
            path=PATH_SEARCH,
            params={
                "keywords": text,
                "type": 1,
                "limit": count,
                "offset": start,
                "total": "true",
            },
            timeout=timeout,
        )
    if legacy:
        return EndpointRequest(
            path=OFFICIAL_PATH_SEARCH_LEGACY,
            params={"s": text, "type": 1, "limit": count, "offset": start},
            timeout=timeout,
        )
    return EndpointRequest(
        path=OFFICIAL_PATH_SEARCH,
        method="POST",
        data={"s": text, "type": 1, "limit": str(count), "offset": str(start)},
        timeout=timeout,
    )


def build_lyric_request(
    song_id: Any,
    *,
    mode: Any = MODE_OFFICIAL,
    timeout: float | None = None,
) -> EndpointRequest:
    """构造歌词请求（lv=-1 原文、tv=-1 翻译、kv=-1 逐字；官方直连拿不到逐字歌词）。"""
    params: dict[str, Any] = {"id": _safe_id(song_id), "lv": -1, "kv": -1, "tv": -1}
    path = PATH_LYRIC if is_self_hosted(mode) else OFFICIAL_PATH_LYRIC
    return EndpointRequest(path=path, params=params, timeout=timeout)


def build_comments_request(
    song_id: Any,
    *,
    limit: int = 20,
    offset: int = 0,
    sort: Any = "hot",
    mode: Any = MODE_OFFICIAL,
    timeout: float | None = None,
    cursor: Any = None,
) -> EndpointRequest:
    """自建新版评论按页排序；最新页的 cursor 由 provider 用前页时间定位。"""
    count = max(1, min(_int(limit, 20), 100))
    start = max(0, _int(offset, 0))
    if is_self_hosted(mode):
        sort_type = comment_sort_value(sort)
        params = {
            "id": _safe_id(song_id),
            "type": 0,
            "pageSize": count,
            "pageNo": start // count + 1,
            "sortType": sort_type,
        }
        if sort_type == 3:
            if cursor is not None:
                params["cursor"] = str(cursor)
            elif start < count:
                params["cursor"] = "0"
        return EndpointRequest(
            path=PATH_COMMENTS,
            params=params,
            timeout=timeout,
        )
    return EndpointRequest(
        path=OFFICIAL_COMMENT_PATH_TEMPLATE.format(song_id=_safe_id(song_id)),
        params={"limit": count, "offset": start},
        timeout=timeout,
    )


def build_classic_comments_request(
    song_id: Any,
    *,
    limit: int = 20,
    offset: int = 0,
    sort: Any = "hot",
    timeout: float | None = None,
) -> EndpointRequest:
    """旧服务用专用热门端点与普通评论 offset；不假定 sortType 语义。"""
    params: dict[str, Any] = {
        "id": _safe_id(song_id),
        "limit": max(1, min(_int(limit, 20), 100)),
        "offset": max(0, _int(offset, 0)),
    }
    hot = str(sort or "").strip().lower() != "new"
    if hot:
        params["type"] = 0
    return EndpointRequest(
        path=PATH_COMMENTS_HOT if hot else PATH_COMMENTS_MUSIC,
        params=params, timeout=timeout,
    )


def build_detail_request(
    song_id: Any,
    *,
    ids: Any = None,
    mode: Any = MODE_OFFICIAL,
    timeout: float | None = None,
) -> EndpointRequest:
    """构造歌曲详情请求（官方要求 ids=[1,2] 形式的 JSON 数组串）。"""
    raw_ids = ids if ids is not None else [song_id]
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    cleaned = [_safe_id(item) for item in raw_ids]
    joined = ",".join(item for item in cleaned if item)
    if is_self_hosted(mode):
        return EndpointRequest(
            path=PATH_SONG_DETAIL, params={"ids": joined}, timeout=timeout
        )
    return EndpointRequest(
        path=OFFICIAL_PATH_SONG_DETAIL, params={"ids": f"[{joined}]"}, timeout=timeout
    )


def build_audio_request(
    song_id: Any,
    *,
    level: str = "standard",
    mode: Any = MODE_OFFICIAL,
    timeout: float | None = None,
) -> EndpointRequest:
    """构造播放地址请求（官方直连通常返回空 url，见模块 docstring 限制 5）。"""
    safe = _safe_id(song_id)
    if is_self_hosted(mode):
        return EndpointRequest(
            path=PATH_SONG_URL,
            params={"id": safe, "level": str(level or "standard")},
            timeout=timeout,
        )
    return EndpointRequest(
        path=OFFICIAL_PATH_SONG_URL,
        params={"ids": f"[{safe}]", "br": 128000},
        timeout=timeout,
    )


def request_url(base: Any, request: Any) -> str:
    """把 EndpointRequest（或含 path 的字典）拼成完整 URL。"""
    path = getattr(request, "path", None)
    if path is None and isinstance(request, Mapping):
        path = request.get("path")
    return join_url(base, path or "")
