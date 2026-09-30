"""网易云 MusicProvider 实现（key="netease"），由 core.provider.get_provider 惰性加载。

协议见 core/provider.py：search / lyrics / comments / card_payload，全部不向外抛异常。
组装关系：endpoints 构造请求 -> HttpTransport（或任意 transport）取 JSON ->
parser 做纯函数解析 -> 本模块拼装成 SongInfo / Lyric / CommentPage / 卡片载荷。

transport 兼容性（重要）
------------------------
- 传入 HttpTransport：使用其 base_url / 超时 / 重试 / Cookie 配置，模式以 transport.mode 为准；
- 传入裸 aiohttp.ClientSession（core.provider.Transport 约定）：由 fetch_json 临时包一层，
  主机取 netease_api_base（为空则按模式取默认主机），不做重试（超时由 session 决定）；
- provider 从不关闭调用方传入的 transport。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..config import RuntimeConfig, ensure_runtime_config
from ..models import CommentPage, Lyric, MusicQuery, SongInfo
from ..provider import CardPayload, register_provider as register_to_registry
from .endpoints import (
    MODE_OFFICIAL,
    EndpointRequest,
    build_audio_request,
    build_comments_request,
    build_detail_request,
    build_lyric_request,
    build_search_request,
    normalise_mode,
    song_web_url,
)
from .http import HttpTransport, fetch_json
from .parser import (
    extract_songs,
    parse_comments,
    parse_lyric,
    parse_search,
    parse_song_detail,
    prioritise_by_artist,
)

logger = logging.getLogger(__name__)

__all__ = ["NeteaseProvider", "PROVIDER", "build_transport", "register_provider"]

MAX_LIMIT = 50
"""单次搜索/评论请求的条数上限。"""

DEFAULT_SEARCH_LIMIT = 10
"""未配置时的搜索条数。"""

DETAIL_BATCH = 10
"""单次 /song/detail 批量取封面的 id 个数上限。"""

DEFAULT_COMMENT_LIMIT = 20
"""未配置时的评论条数。"""


def _as_str(value: Any, default: str = "") -> str:
    """安全转 str：None / 空串 / 非标量一律回落到 default。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_int(value: Any, default: int) -> int:
    """安全转 int。"""
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


class NeteaseProvider:
    """网易云 provider：搜索 / 歌词 / 评论 / 卡片载荷。"""

    key = "netease"
    """provider 唯一键（core.provider.PROVIDER_REGISTRY 的键）。"""

    display_name = "网易云音乐"
    """展示名称（日志与卡片来源文案）。"""

    def __init__(self, config: Any = None, **overrides: Any) -> None:
        """config 可以是 RuntimeConfig / 配置映射 / None（用默认值）。

        overrides 支持 search_limit / comment_sort / comments_max_chars /
        card_type / card_show_cover / card_show_source / card_attach_audio，
        用于在不改配置字典的前提下覆盖单项行为。
        """
        self._cfg: RuntimeConfig = ensure_runtime_config(config)
        self._overrides: dict[str, Any] = {
            str(name): value for name, value in overrides.items() if value is not None
        }

    # ------------------------------------------------------------ 配置

    @property
    def config(self) -> RuntimeConfig:
        """当前绑定的运行期配置（只读快照）。"""
        return self._cfg

    @property
    def overrides(self) -> dict[str, Any]:
        """当前覆盖项副本。"""
        return dict(self._overrides)

    def configure(self, config: Any) -> NeteaseProvider:
        """重新绑定配置（插件热重载用），返回自身便于链式调用。"""
        self._cfg = ensure_runtime_config(config)
        return self

    @classmethod
    def from_config(cls, config: Any) -> NeteaseProvider:
        """由配置构造 provider。"""
        return cls(config)

    def _option(self, name: str, default: Any = None) -> Any:
        """取覆盖项，其次取配置项，最后默认值。"""
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._cfg, name, default)

    # ------------------------------------------------------------ 内部工具

    def _mode_for(self, transport: Any) -> str:
        """请求模式：transport 自带 mode 时以其为准，否则用配置。"""
        mode = getattr(transport, "mode", None)
        if isinstance(mode, str) and mode.strip():
            return normalise_mode(mode)
        return normalise_mode(self._cfg.netease_mode)

    def _adapter_options(self, transport: Any) -> dict[str, Any]:
        """给 fetch_json 的兜底参数（仅在 transport 不是 HttpTransport 时生效）。"""
        base = getattr(transport, "base_url", "")
        return {
            "mode": self._mode_for(transport),
            "base_url": "" if isinstance(base, str) and base.strip() else self._cfg.netease_api_base,
            "max_retries": self._cfg.max_retries,
        }

    async def _fetch(self, transport: Any, request: EndpointRequest) -> dict[str, Any] | None:
        """执行请求并取 JSON（失败返回 None）。"""
        options = self._adapter_options(transport)
        return await fetch_json(
            transport,
            request,
            mode=options["mode"],
            base_url=options["base_url"],
            max_retries=options["max_retries"],
        )

    @staticmethod
    def _song_id(song: Any) -> str:
        """从 SongInfo / 映射 / 字符串里取歌曲 id。"""
        if isinstance(song, SongInfo):
            return song.id.strip()
        if isinstance(song, Mapping):
            for key in ("id", "song_id", "songId"):
                value = song.get(key)
                if value is not None and not isinstance(value, bool) and str(value).strip():
                    return str(value).strip()
            return ""
        if isinstance(song, (int, float)) and not isinstance(song, bool):
            return str(int(song))
        text = str(song or "").strip()
        return text if text.isdigit() else ""

    def _search_limit(self, query: MusicQuery) -> int:
        """搜索条数：覆盖项 > query.limit > 配置，范围 [1, MAX_LIMIT]。"""
        if "search_limit" in self._overrides:
            value = _as_int(self._overrides["search_limit"], DEFAULT_SEARCH_LIMIT)
        elif query.limit > 0:
            value = query.limit
        else:
            value = _as_int(self._cfg.search_limit, DEFAULT_SEARCH_LIMIT)
        return max(1, min(value, MAX_LIMIT))

    def _comment_limit(self, limit: Any) -> int:
        """评论条数：显式 limit > 配置 comments_count，范围 [1, MAX_LIMIT]。"""
        value = _as_int(limit, 0)
        if value <= 0:
            value = _as_int(self._cfg.comments_count, DEFAULT_COMMENT_LIMIT)
        if value <= 0:
            value = DEFAULT_COMMENT_LIMIT
        return max(1, min(value, MAX_LIMIT))

    def _comment_sort(self, sort: Any = None) -> str:
        """评论排序：显式参数 > 覆盖项 > 配置 comments_sort。"""
        text = str(sort or self._option("comment_sort", self._cfg.comments_sort) or "hot")
        text = text.strip().lower()
        return text if text in {"hot", "new"} else "hot"

    def _comment_max_chars(self) -> int:
        """评论正文字数上限（0 表示不截断）。"""
        return max(0, _as_int(self._option("comments_max_chars", self._cfg.comments_max_chars), 0))

    # ------------------------------------------------------------ 协议方法

    async def search(self, query: Any, transport: Any) -> list[SongInfo]:
        """搜索歌曲（歌名 + 歌手合并搜索，再按歌手模糊匹配优先排序）。

        失败返回空列表；官方直连下若新接口无结果会回退旧的 /api/search/get。
        """
        try:
            if not isinstance(query, MusicQuery):
                query = MusicQuery.from_text(query if isinstance(query, str) else "")
            text = query.search_text.strip()
            if not text:
                return []
            limit = self._search_limit(query)
            mode = self._mode_for(transport)
            timeout = self._cfg.search_timeout
            request = build_search_request(
                text, limit=limit, offset=0, mode=mode, timeout=timeout
            )
            payload = await self._fetch(transport, request)
            songs = parse_search(payload, provider_key=self.key, limit=limit)
            if not songs and mode == MODE_OFFICIAL:
                legacy = build_search_request(
                    text, limit=limit, offset=0, mode=mode, legacy=True, timeout=timeout
                )
                payload = await self._fetch(transport, legacy)
                songs = parse_search(payload, provider_key=self.key, limit=limit)
            if query.artist:
                songs = prioritise_by_artist(songs, query.artist)
            return songs
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("网易云搜索失败：%r", exc)
            return []

    async def lyrics(self, song: SongInfo, transport: Any) -> Lyric | None:
        """取歌词；无歌词、无 id 或请求失败返回 None。"""
        song_id = self._song_id(song)
        if not song_id:
            logger.debug("歌词请求缺少歌曲 id，已跳过")
            return None
        try:
            mode = self._mode_for(transport)
            request = build_lyric_request(
                song_id, mode=mode, timeout=self._cfg.api_timeout
            )
            payload = await self._fetch(transport, request)
            if payload is None:
                return None
            lyric = parse_lyric(payload)
            return None if lyric.is_empty() else lyric
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("网易云歌词获取失败：%r", exc)
            return None

    async def comments(
        self,
        song: SongInfo,
        transport: Any,
        limit: int = DEFAULT_COMMENT_LIMIT,
        offset: int = 0,
        *,
        sort: Any = None,
    ) -> CommentPage | None:
        """取评论分页；无 id 或请求失败返回 None（成功但无评论返回空页）。

        额外可选参数 sort（hot/new）覆盖配置 comments_sort，协议本身不含该参数。
        """
        song_id = self._song_id(song)
        if not song_id:
            logger.debug("评论请求缺少歌曲 id，已跳过")
            return None
        try:
            mode = self._mode_for(transport)
            count = self._comment_limit(limit)
            start = max(0, _as_int(offset, 0))
            sort_key = self._comment_sort(sort)
            request = build_comments_request(
                song_id,
                limit=count,
                offset=start,
                sort=sort_key,
                mode=mode,
                timeout=self._cfg.api_timeout,
            )
            payload = await self._fetch(transport, request)
            if payload is None:
                return None
            return parse_comments(
                payload, self._comment_max_chars(), sort=sort_key, offset=start
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("网易云评论获取失败：%r", exc)
            return None

    async def song_detail(self, song: SongInfo, transport: Any) -> SongInfo:
        """补全封面 / 时长 / 外链（协议外的额外能力，失败原样返回 song）。"""
        try:
            base = song if isinstance(song, SongInfo) else SongInfo.from_mapping(song)
            song_id = self._song_id(base)
            if not song_id:
                return base
            mode = self._mode_for(transport)
            request = build_detail_request(
                song_id, mode=mode, timeout=self._cfg.api_timeout
            )
            payload = await self._fetch(transport, request)
            if payload is None:
                return base
            return parse_song_detail(payload, base)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("网易云详情获取失败：%r", exc)
            return song if isinstance(song, SongInfo) else SongInfo.from_mapping(song)

    async def audio_url(self, song: SongInfo, transport: Any, *, level: str = "standard") -> str:
        """取播放地址（协议外的额外能力；失败或官方直连受限时返回空串）。"""
        song_id = self._song_id(song)
        if not song_id:
            return ""
        try:
            mode = self._mode_for(transport)
            request = build_audio_request(
                song_id, level=level, mode=mode, timeout=self._cfg.api_timeout
            )
            payload = await self._fetch(transport, request)
            if not payload:
                return ""
            for item in self._audio_candidates(payload):
                url = item.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
            return ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("网易云播放地址获取失败：%r", exc)
            return ""

    @staticmethod
    def _audio_candidates(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """从播放地址响应里取出候选条目（兼容 data 数组与 map 字典）。"""
        data = payload.get("data")
        if isinstance(data, (list, tuple)):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            return [item for item in data.values() if isinstance(item, Mapping)]
        if isinstance(payload.get("url"), str):
            return [payload]
        return []

    def card_payload(self, song: SongInfo) -> CardPayload:
        """构造歌曲卡片载荷（同步方法，键与 AstrBot Music/Share 组件参数对齐）。

        card_type="share" -> kind="share"，其余（163/custom）-> kind="music" 且 type=card_type；
        card_show_cover 控制 image，card_attach_audio 控制 audio，card_show_source 决定
        content 是否带来源名。
        """
        item = song if isinstance(song, SongInfo) else SongInfo.from_mapping(song)
        card_type = str(self._option("card_type", "163") or "163").strip().lower()
        if card_type not in {"163", "custom", "share"}:
            card_type = "163"
        show_cover = bool(self._option("card_show_cover", True))
        show_source = bool(self._option("card_show_source", True))
        attach_audio = bool(self._option("card_attach_audio", False))
        parts = [item.artist_text, item.album]
        if show_source:
            parts.append(self.display_name)
        content = " · ".join(part for part in parts if part)
        return CardPayload(
            kind="share" if card_type == "share" else "music",
            type=card_type,
            id=item.id,
            title=item.name,
            content=content,
            image=item.cover_url if show_cover else "",
            url=item.url or song_web_url(item.id),
            audio=item.audio_url if attach_audio else "",
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return f"NeteaseProvider(key={self.key!r}, mode={self._cfg.netease_mode!r})"


PROVIDER = NeteaseProvider()
"""模块级默认实例：core.provider.get_provider("netease") 直接复用它。"""


def register_provider(provider: Any = None) -> NeteaseProvider:
    """把 provider 注册进 core.provider.PROVIDER_REGISTRY（幂等），返回实例。"""
    instance = provider if isinstance(provider, NeteaseProvider) else PROVIDER
    registered = register_to_registry(instance)
    return registered if isinstance(registered, NeteaseProvider) else instance


def build_transport(config: Any = None) -> HttpTransport:
    """按配置构造 HttpTransport（供 main.py / 流程层使用）。"""
    return HttpTransport.from_config(config)
