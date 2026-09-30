"""点歌插件数据模型（字段名冻结，所有模块共用）。

字段名由契约层冻结，其他任务不得改名：
- LyricLine(text, translation)
- Lyric(text, translated, lines, is_empty())
- CommentItem(user, content, liked, avatar_url, time, replies)
- CommentPage(items, total, has_more)
- SongInfo(id, name, artists, album, duration_ms, cover_url, source, url,
           audio_url, provider_key)
- MusicQuery(keyword, artist, limit)

所有构造函数都容错：外部 API 字段缺失 / 类型异常时退化为空值，绝不抛异常，
并提供 to_dict() 供 Jinja2 模板直接使用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "CommentItem",
    "CommentPage",
    "Lyric",
    "LyricLine",
    "MusicQuery",
    "SongInfo",
    "format_duration",
]

_LIST_SPLIT_PATTERN = re.compile(r"[,，、;；|]")


def _as_str(value: Any, default: str = "") -> str:
    """把任意值安全地转成字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_int(value: Any, default: int = 0) -> int:
    """把任意值安全地转成整数（"3" / 3.0 都可以，失败给默认值）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            return default
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """把任意值安全地转成布尔。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off", ""}:
            return False
    return default


def _as_str_list(value: Any) -> list[str]:
    """把任意值安全地转成字符串列表（支持 list/tuple/set/逗号分隔字符串/字典列表）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in _LIST_SPLIT_PATTERN.split(value) if part.strip()]
    if isinstance(value, Mapping):
        items: Iterable[Any] = value.keys()
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            text = _as_str(item.get("name")) or _as_str(item.get("text"))
        elif isinstance(item, (list, tuple, set, frozenset, Mapping)):
            text = ""
        else:
            text = _as_str(item)
        text = text.strip()
        if text:
            result.append(text)
    return result


def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非 None 的键值。"""
    for key in keys:
        if key in data:
            value = data[key]
            if value is not None:
                return value
    return default


def format_duration(duration_ms: Any) -> str:
    """毫秒 -> "mm:ss"；无效值返回 "--:--"。"""
    ms = _as_int(duration_ms, 0)
    if ms <= 0:
        return "--:--"
    total_seconds = ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass
class LyricLine:
    """一行歌词：原文 + 可选翻译。"""

    text: str = ""
    translation: str = ""

    def __post_init__(self) -> None:
        self.text = _as_str(self.text)
        self.translation = _as_str(self.translation)

    def is_empty(self) -> bool:
        """原文与翻译都为空视为空行。"""
        return not (self.text.strip() or self.translation.strip())

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "translation": self.translation}

    @classmethod
    def from_mapping(cls, data: Any) -> LyricLine:
        """从映射 / 字符串构造一行歌词。"""
        if isinstance(data, Mapping):
            return cls(
                text=_as_str(_first(data, "text", "line", "lyric", default="")),
                translation=_as_str(
                    _first(data, "translation", "tlyric", "trans", default="")
                ),
            )
        if isinstance(data, str):
            return cls(text=data)
        return cls()


@dataclass
class Lyric:
    """一首歌的歌词：纯文本、纯翻译文本与逐行结构化歌词。"""

    text: str = ""
    translated: str = ""
    lines: list[LyricLine] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.text = _as_str(self.text)
        self.translated = _as_str(self.translated)
        if self.lines is None:
            self.lines = []
        elif not isinstance(self.lines, list):
            self.lines = list(self.lines)
        normalised: list[LyricLine] = []
        for line in self.lines:
            if isinstance(line, LyricLine):
                normalised.append(line)
            else:
                normalised.append(LyricLine.from_mapping(line))
        self.lines = normalised

    def is_empty(self) -> bool:
        """没有任何可用歌词（无文本、无翻译、无有效行）时为 True。"""
        if self.text.strip() or self.translated.strip():
            return False
        return all(line.is_empty() for line in self.lines)

    def line_count(self) -> int:
        """有效歌词行数（不含纯空行）。"""
        return sum(1 for line in self.lines if not line.is_empty())

    def plain_text(self) -> str:
        """无结构纯文本：优先 text，其次由 lines 拼接。"""
        if self.text.strip():
            return self.text
        return "\n".join(line.text for line in self.lines if line.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "translated": self.translated,
            "lines": [line.to_dict() for line in self.lines],
            "line_count": self.line_count(),
            "has_translation": bool(self.translated.strip())
            or any(line.translation.strip() for line in self.lines),
        }

    @classmethod
    def from_mapping(cls, data: Any) -> Lyric:
        """从映射构造歌词（兼容 lyric/tlyric、text/translated 命名）。"""
        if not isinstance(data, Mapping):
            return cls()
        raw_lines = _first(data, "lines", default=None)
        lines: list[LyricLine] = []
        if isinstance(raw_lines, (list, tuple)):
            lines = [LyricLine.from_mapping(item) for item in raw_lines]
        return cls(
            text=_as_str(_first(data, "text", "lyric", default="")),
            translated=_as_str(_first(data, "translated", "tlyric", "trans", default="")),
            lines=lines,
        )


@dataclass
class CommentItem:
    """一条网易云评论（可含楼中楼回复）。"""

    user: str = ""
    content: str = ""
    liked: int = 0
    avatar_url: str = ""
    time: str = ""
    replies: list[CommentItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.user = _as_str(self.user)
        self.content = _as_str(self.content)
        self.liked = _as_int(self.liked, 0)
        self.avatar_url = _as_str(self.avatar_url)
        self.time = _as_str(self.time)
        if self.replies is None:
            self.replies = []
        elif not isinstance(self.replies, list):
            self.replies = list(self.replies)
        self.replies = [
            reply if isinstance(reply, CommentItem) else CommentItem.from_mapping(reply)
            for reply in self.replies
        ]

    def is_empty(self) -> bool:
        return not (self.user.strip() or self.content.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "content": self.content,
            "liked": self.liked,
            "avatar_url": self.avatar_url,
            "time": self.time,
            "replies": [reply.to_dict() for reply in self.replies],
        }

    @classmethod
    def from_mapping(cls, data: Any) -> CommentItem:
        """从常见评论 API 字段构造（兼容昵称/头像/点赞数量命名差异）。"""
        if not isinstance(data, Mapping):
            return cls()
        user_raw = _first(data, "user", "nickname", "nickName", default=None)
        if isinstance(user_raw, Mapping):
            user = _as_str(
                _first(
                    user_raw,
                    "nickname",
                    "nickName",
                    "name",
                    "userName",
                    default="",
                )
            )
            avatar = _as_str(
                _first(user_raw, "avatar_url", "avatarUrl", "avatar", default="")
            )
        else:
            user = _as_str(user_raw)
            avatar = ""
        avatar = avatar or _as_str(
            _first(data, "avatar_url", "avatarUrl", "avatar", default="")
        )
        replies_raw = _first(data, "replies", "beReplied", default=None)
        replies: list[CommentItem] = []
        if isinstance(replies_raw, (list, tuple)):
            replies = [cls.from_mapping(item) for item in replies_raw]
        return cls(
            user=user,
            content=_as_str(_first(data, "content", "text", default="")),
            liked=_as_int(
                _first(data, "liked", "likedCount", "likeCount", "like", default=0), 0
            ),
            avatar_url=avatar,
            time=_as_str(
                _first(data, "time", "timeStr", "createTime", "ctime", default="")
            ),
            replies=replies,
        )


@dataclass
class CommentPage:
    """一页评论。"""

    items: list[CommentItem] = field(default_factory=list)
    total: int = 0
    has_more: bool = False

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []
        elif not isinstance(self.items, list):
            self.items = list(self.items)
        self.items = [
            item if isinstance(item, CommentItem) else CommentItem.from_mapping(item)
            for item in self.items
        ]
        self.total = _as_int(self.total, 0)
        self.has_more = _as_bool(self.has_more, False)

    def is_empty(self) -> bool:
        return not self.items

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "has_more": self.has_more,
        }

    @classmethod
    def from_mapping(cls, data: Any) -> CommentPage:
        """从映射构造（兼容 comments/hotComments、more/total 命名差异）。"""
        if not isinstance(data, Mapping):
            return cls()
        raw_items = _first(
            data, "items", "comments", "hotComments", "list", default=None
        )
        items: list[CommentItem] = []
        if isinstance(raw_items, (list, tuple)):
            items = [CommentItem.from_mapping(item) for item in raw_items]
        return cls(
            items=items,
            total=_as_int(_first(data, "total", "totalCount", default=0), 0),
            has_more=_as_bool(_first(data, "has_more", "more", "hasMore", default=False)),
        )


@dataclass
class SongInfo:
    """一首歌的元信息（网易云为默认来源）。"""

    id: str = ""
    name: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    cover_url: str = ""
    source: str = "netease"
    url: str = ""
    audio_url: str = ""
    provider_key: str = "netease"

    def __post_init__(self) -> None:
        self.id = _as_str(self.id)
        self.name = _as_str(self.name)
        self.artists = _as_str_list(self.artists)
        self.album = _as_str(self.album)
        self.duration_ms = _as_int(self.duration_ms, 0)
        self.cover_url = _as_str(self.cover_url)
        self.source = _as_str(self.source) or "netease"
        self.url = _as_str(self.url)
        self.audio_url = _as_str(self.audio_url)
        self.provider_key = _as_str(self.provider_key) or self.source

    @property
    def artist_text(self) -> str:
        """歌手拼接文本（模板用）。"""
        return " / ".join(artist for artist in self.artists if artist)

    @property
    def duration_text(self) -> str:
        """时长文本 "mm:ss"（模板用）。"""
        return format_duration(self.duration_ms)

    @property
    def display_name(self) -> str:
        """ "歌名 - 歌手"（歌手为空时只有歌名）。"""
        return f"{self.name} - {self.artist_text}" if self.artist_text else self.name

    def is_empty(self) -> bool:
        return not (self.id or self.name)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artist_text"] = self.artist_text
        data["duration_text"] = self.duration_text
        data["display_name"] = self.display_name
        return data

    @classmethod
    def from_mapping(
        cls,
        data: Any,
        *,
        provider_key: str = "netease",
        source: str = "",
    ) -> SongInfo:
        """从搜索 / 详情接口的常见字段构造 SongInfo（容错，永不抛异常）。"""
        if not isinstance(data, Mapping):
            return cls(provider_key=provider_key, source=source or provider_key)
        album_raw = _first(data, "album", "al", "albumName", default=None)
        if isinstance(album_raw, Mapping):
            album = _as_str(_first(album_raw, "name", "albumName", default=""))
        else:
            album = _as_str(album_raw)
        artist_raw = _first(data, "artists", "ar", "artist", "singers", default=None)
        artists = _as_str_list(artist_raw)
        cover = _as_str(
            _first(
                data,
                "cover_url",
                "coverUrl",
                "picUrl",
                "coverImgUrl",
                "image",
                default=None,
            )
        )
        if not cover and isinstance(album_raw, Mapping):
            cover = _as_str(_first(album_raw, "picUrl", "pic_url", default=""))
        duration = _first(
            data, "duration_ms", "duration", "dt", "interval", default=0
        )
        song_id = _first(data, "id", "song_id", "songId", default="")
        return cls(
            id=_as_str(song_id),
            name=_as_str(_first(data, "name", "title", "songname", default="")),
            artists=artists,
            album=album,
            duration_ms=_as_int(duration, 0),
            cover_url=cover,
            source=source or provider_key,
            url=_as_str(_first(data, "url", "web_url", "link", default="")),
            audio_url=_as_str(
                _first(data, "audio_url", "audio", "audioUrl", default="")
            ),
            provider_key=provider_key,
        )


@dataclass
class MusicQuery:
    """一次点歌查询：关键词 + 可选歌手 + 结果条数。"""

    keyword: str = ""
    artist: str = ""
    limit: int = 5

    def __post_init__(self) -> None:
        self.keyword = _as_str(self.keyword).strip()
        self.artist = _as_str(self.artist).strip()
        self.limit = max(1, _as_int(self.limit, 5))

    @property
    def search_text(self) -> str:
        """拼接后的搜索关键词（"歌名 歌手"）。"""
        return f"{self.keyword} {self.artist}".strip()

    def is_empty(self) -> bool:
        return not self.keyword

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "artist": self.artist,
            "limit": self.limit,
            "search_text": self.search_text,
        }

    @classmethod
    def from_text(
        cls,
        raw: str,
        *,
        limit: int = 5,
        artist: str = "",
    ) -> MusicQuery:
        """把 "/点歌 歌名 歌手" 的原始文本拆成 keyword + artist。"""
        text = _as_str(raw).strip()
        if not text:
            return cls(limit=limit, artist=artist)
        if artist:
            return cls(keyword=text, artist=artist, limit=limit)
        for separator in (" - ", " – ", " — "):
            if separator in text:
                left, _, right = text.partition(separator)
                if left.strip() and right.strip():
                    return cls(keyword=left.strip(), artist=right.strip(), limit=limit)
        parts = [part for part in re.split(r"\s+", text) if part]
        if len(parts) == 1:
            for separator in ("-", "—", "–"):
                if separator in parts[0]:
                    left, _, right = parts[0].partition(separator)
                    if left.strip() and right.strip():
                        return cls(
                            keyword=left.strip(), artist=right.strip(), limit=limit
                        )
            return cls(keyword=parts[0], limit=limit)
        return cls(keyword=parts[0], artist=" ".join(parts[1:]), limit=limit)
