"""选曲逻辑：把「/点歌 歌名 歌手」的输入解析成一首 SongInfo。

契约（冻结）
------------
resolve_song(cfg, transport, keyword, artist) -> SongInfo | None
- 关键词先 strip、折叠空白并截断到 80 字；关键词与歌手都为空时返回 None；
- 搜索条数取 cfg.search_limit；歌手过滤：歌名与歌手合并搜索，再按歌手模糊匹配优先排序；
- 选曲策略 cfg.pick_strategy：
  * "top"   -> 歌手优先排序后的第一条；
  * "first" -> 歌手优先 + 完全同名优先（歌手匹配度高于同名度）；
- 任何失败（provider 缺失 / 抛异常 / 无结果）一律返回 None，不向上抛异常。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .config import RuntimeConfig, ensure_runtime_config
from .models import MusicQuery, SongInfo
from .netease.parser import artist_match_score, name_match_score
from .provider import Transport, get_provider

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_KEYWORD_CHARS",
    "MAX_ARTIST_CHARS",
    "normalise_keyword",
    "pick_song",
    "resolve_song",
    "search_songs",
]

MAX_KEYWORD_CHARS = 80
"""关键词最大长度（超出截断）。"""

MAX_ARTIST_CHARS = 40
"""歌手名最大长度（超出截断）。"""

DEFAULT_SEARCH_LIMIT = 5
"""未配置时的搜索条数。"""

_WHITESPACE_RE = re.compile(r"\s+")


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


def normalise_keyword(raw: Any, *, max_chars: int = MAX_KEYWORD_CHARS) -> str:
    """规范化关键词：非字符串给空串、折叠空白、按上限截断。"""
    if raw is None or isinstance(raw, bool):
        text = ""
    elif isinstance(raw, str):
        text = raw
    elif isinstance(raw, (int, float)):
        text = str(raw)
    else:
        text = ""
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    limit = max(1, _as_int(max_chars, MAX_KEYWORD_CHARS))
    return collapsed[:limit].strip()


def _limit_of(config: RuntimeConfig) -> int:
    """搜索条数（至少 1）。"""
    return max(1, _as_int(config.search_limit, DEFAULT_SEARCH_LIMIT))


async def search_songs(
    cfg: Any,
    transport: Any,
    keyword: Any,
    artist: Any = "",
    *,
    provider: Any = None,
) -> list[SongInfo]:
    """按配置搜索歌曲；失败返回空列表。

    provider 参数用于注入已构造的 provider（默认按 cfg.provider 从注册表取）。
    """
    config = ensure_runtime_config(cfg)
    key = normalise_keyword(keyword)
    artist_text = normalise_keyword(artist, max_chars=MAX_ARTIST_CHARS)
    if not key and not artist_text:
        return []
    source = provider if provider is not None else get_provider(config.provider)
    if source is None:
        logger.warning("未找到 provider %r，跳过搜索", config.provider)
        return []
    query = MusicQuery(
        keyword=key or artist_text,
        artist=artist_text if key else "",
        limit=_limit_of(config),
    )
    try:
        results = await source.search(query, transport)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("provider %r 搜索失败：%r", config.provider, exc)
        return []
    songs: list[SongInfo] = []
    for item in results or []:
        if isinstance(item, SongInfo):
            songs.append(item)
        else:
            converted = SongInfo.from_mapping(item)
            if not converted.is_empty():
                songs.append(converted)
    limit = _limit_of(config)
    return songs[:limit] if len(songs) > limit else songs


def pick_song(
    songs: Any,
    *,
    keyword: Any = "",
    artist: Any = "",
    strategy: Any = "top",
) -> SongInfo | None:
    """从候选里挑一首：歌手匹配优先，strategy="first" 时再让完全同名优先。"""
    items = [
        song
        for song in (songs or [])
        if isinstance(song, SongInfo) and not song.is_empty()
    ]
    if not items:
        return None
    artist_text = normalise_keyword(artist, max_chars=MAX_ARTIST_CHARS)
    name_text = normalise_keyword(keyword)
    artist_scores = [artist_match_score(song.artists, artist_text) for song in items]
    mode = str(strategy or "top").strip().lower()
    if mode == "first":
        name_scores = [name_match_score(song.name, name_text) for song in items]
        order = min(
            range(len(items)),
            key=lambda index: (-artist_scores[index], -name_scores[index], index),
        )
    else:
        order = min(
            range(len(items)),
            key=lambda index: (-artist_scores[index], index),
        )
    return items[order]


async def resolve_song(
    cfg: Any,
    transport: Any,
    keyword: Any,
    artist: Any = "",
    *,
    provider: Any = None,
) -> SongInfo | None:
    """搜索并选出唯一一首歌；无结果或失败返回 None。"""
    config = ensure_runtime_config(cfg)
    key = normalise_keyword(keyword)
    artist_text = normalise_keyword(artist, max_chars=MAX_ARTIST_CHARS)
    if not key and not artist_text:
        return None
    songs = await search_songs(config, transport, key, artist_text, provider=provider)
    if not songs:
        return None
    return pick_song(
        songs, keyword=key, artist=artist_text, strategy=config.pick_strategy
    )
