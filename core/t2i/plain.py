"""歌词与评论的纯文本 / Markdown 构造层（t2i 降级与最终文本兜底共用）。

设计要点（冻结）：

- 只做「数据 -> 文本」的纯函数转换：不联网、不 import astrbot、不 import jinja2。
- 产出的多行文本有两个用途：
  1. 作为 HTML 模板数据里的 ``fallback_text``，供 DefaultRenderer 在 mode="local" 时
     用本地 Markdown 渲染（AstrBot 的本地策略只支持 Markdown，不支持 HTML）；
  2. 作为 render_html / render_markdown 全部失败后的最终纯文本兜底消息。
- 因此格式同时兼顾「Markdown 图片渲染」与「聊天窗口直接阅读」：不使用 # / ** 等
  Markdown 标记（在聊天窗口里会原样显示）。
- 所有截断都按运行时配置执行：lyrics_max_lines、comments_count、comments_max_chars、
  comments_reply_count；max_lines 与 max_chars 上的 0 表示不限制。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..config import ensure_runtime_config
from ..models import format_duration

__all__ = [
    "EMPTY_COMMENT_TEXT",
    "EMPTY_LYRIC_TEXT",
    "SOURCE_LABELS",
    "build_comments_text",
    "build_lyrics_text",
    "comment_entries",
    "format_count",
    "format_likes",
    "lyric_lines",
    "song_artist",
    "song_duration",
    "song_heading",
    "song_meta",
    "song_source",
    "truncate_text",
]

EMPTY_LYRIC_TEXT = "（暂无歌词）"
"""歌词为空时的占位文本。"""

EMPTY_COMMENT_TEXT = "（暂无评论）"
"""没有可展示的评论时的占位文本。"""

SOURCE_LABELS: dict[str, str] = {"netease": "网易云音乐"}
"""来源 key -> 展示名；未知 key 原样展示。"""

_ANONYMOUS_USER = "匿名用户"
"""昵称为空时的占位。"""

_WAN = 10_000
"""万的数值（点赞数 / 评论数展示用）。"""

_YI = 100_000_000
"""亿的数值。"""


# --------------------------------------------------------------------- 取值辅助


def _field(source: Any, name: str, default: Any = "") -> Any:
    """兼容 dataclass 与 Mapping 的取值：缺失 / None 一律给默认值。"""
    if source is None:
        return default
    if isinstance(source, Mapping):
        value = source.get(name, default)
    else:
        value = getattr(source, name, default)
    return default if value is None else value


def _as_int(value: Any, default: int = 0) -> int:
    """安全转 int（bool / 非法字符串给默认值，float 截断）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return default if value != value else int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _text(value: Any) -> str:
    """安全转字符串并去掉首尾空白。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _compact(value: float, unit: str) -> str:
    """把数值压成一位小数并去掉多余的 ".0"（1.0万 -> 1万）。"""
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}{unit}"


# --------------------------------------------------------------------- 格式化


def format_count(count: Any) -> str:
    """计数人类可读化：999 -> "999"；12345 -> "1.2万"；123456789 -> "1.2亿"。"""
    value = max(0, _as_int(count, 0))
    if value >= _YI:
        return _compact(value / _YI, "亿")
    if value >= _WAN:
        return _compact(value / _WAN, "万")
    return str(value)


def format_likes(count: Any) -> str:
    """点赞数格式化（规则同 format_count：1.2万 / 1.2亿，负数按 0）。"""
    return format_count(count)


def truncate_text(text: Any, max_chars: Any = 0) -> str:
    """按字符数截断正文，超出部分加省略号。

    保留前 max_chars 个字符，其后追加省略号（所以结果最长为 max_chars + 1 个字符）；
    max_chars <= 0 表示不限制。输入非字符串时先安全转成字符串。
    """
    value = _text(text)
    limit = max(0, _as_int(max_chars, 0))
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


# --------------------------------------------------------------------- 歌曲信息


def song_artist(song: Any) -> str:
    """歌手文本 "A / B"（没有 artist_text 属性时退回 artists 列表）。"""
    text = _text(_field(song, "artist_text", ""))
    if text:
        return text
    artists = _field(song, "artists", [])
    if isinstance(artists, str):
        return artists.strip()
    if isinstance(artists, (list, tuple)):
        return " / ".join(part for part in (_text(item) for item in artists) if part)
    return ""


def song_heading(song: Any) -> str:
    """标题行 "歌名 - 歌手"（歌手为空时只有歌名，两者都空时为空串）。"""
    name = _text(_field(song, "name", ""))
    artist = song_artist(song)
    if name and artist:
        return f"{name} - {artist}"
    return name or artist


def song_duration(song: Any) -> str:
    """时长文本 "3:45"；未知时长返回空串（不展示 "--:--"）。"""
    text = _text(_field(song, "duration_text", ""))
    if text and text != "--:--":
        return text
    text = format_duration(_field(song, "duration_ms", 0))
    return "" if text == "--:--" else text


def song_source(song: Any) -> str:
    """来源展示名（netease -> 网易云音乐；未知 key 原样展示）。"""
    key = _text(_field(song, "provider_key", "")) or _text(_field(song, "source", ""))
    return SOURCE_LABELS.get(key, key)


def song_meta(song: Any) -> str:
    """一行元信息："专辑：x ｜ 时长：3:45 ｜ 网易云音乐"（缺失项自动跳过）。"""
    parts: list[str] = []
    album = _text(_field(song, "album", ""))
    if album:
        parts.append(f"专辑：{album}")
    duration = song_duration(song)
    if duration:
        parts.append(f"时长：{duration}")
    source = song_source(song)
    if source:
        parts.append(source)
    return " ｜ ".join(parts)


# --------------------------------------------------------------------- 歌词


def _trim_blank(lines: list[dict[str, str]]) -> list[dict[str, str]]:
    """去掉首尾的空行，保留中间的空行（当作段落间隔）。"""
    start, end = 0, len(lines)
    while start < end and not (lines[start]["text"] or lines[start]["translation"]):
        start += 1
    while end > start and not (lines[end - 1]["text"] or lines[end - 1]["translation"]):
        end -= 1
    return lines[start:end]


def lyric_lines(lyric: Any) -> list[dict[str, str]]:
    """把 Lyric 归一成 [{"text", "translation"}]；没有有效歌词时返回 []。

    优先使用逐行结构 lyric.lines；没有有效行时按 lyric.text（其次 lyric.translated）
    按行拆分。首尾空行去掉，中间空行保留为间隔。
    """
    normalized: list[dict[str, str]] = []
    raw_lines = _field(lyric, "lines", None)
    if isinstance(raw_lines, (list, tuple)):
        normalized = [
            {
                "text": _text(_field(item, "text", "")),
                "translation": _text(_field(item, "translation", "")),
            }
            for item in raw_lines
        ]
    if not any(line["text"] or line["translation"] for line in normalized):
        source_text = _text(_field(lyric, "text", "")) or _text(
            _field(lyric, "translated", "")
        )
        normalized = [
            {"text": part.strip(), "translation": ""}
            for part in source_text.splitlines()
            if part.strip()
        ]
    return _trim_blank(normalized)


def build_lyrics_text(song: Any, lyric: Any, max_lines: Any = 0) -> str:
    """构造歌词 Markdown / 纯文本（t2i 降级文本与最终兜底消息共用）。

    结构::

        歌名 - 歌手
        专辑：x ｜ 时长：3:45 ｜ 网易云音乐

        第一行歌词
          第一行翻译
        第二行歌词
        （已省略 150 行，共 200 行）

    max_lines <= 0 表示不限制；超过时只保留前 max_lines 行并追加省略说明。
    没有任何歌词时输出占位文本（EMPTY_LYRIC_TEXT）。
    """
    limit = max(0, _as_int(max_lines, 0))
    lines = lyric_lines(lyric)
    parts: list[str] = []
    heading = song_heading(song)
    if heading:
        parts.append(heading)
    meta = song_meta(song)
    if meta:
        parts.append(meta)
    parts.append("")
    if not lines:
        parts.append(EMPTY_LYRIC_TEXT)
        return "\n".join(parts).strip()
    visible = lines if limit == 0 or limit >= len(lines) else lines[:limit]
    for line in visible:
        if line["text"]:
            parts.append(line["text"])
        if line["translation"]:
            parts.append(f"  {line['translation']}")
    if limit and limit < len(lines):
        parts.append(f"（已省略 {len(lines) - limit} 行，共 {len(lines)} 行）")
    return "\n".join(parts).strip()


# --------------------------------------------------------------------- 评论


def comment_entries(page: Any, cfg: Any = None) -> list[dict[str, Any]]:
    """把 CommentPage 归一成模板与文本共用的评论条目（已按配置截断）。

    - 条数受 comments_count 限制（0 表示一条都不展示）；
    - 正文受 comments_max_chars 限制（0 表示不限制），超出加省略号；
    - comments_show_reply 关闭时回复列表恒为空，开启时每条最多
      comments_reply_count 条回复（回复正文同样受字数限制）。
    """
    config = ensure_runtime_config(cfg)
    raw_items = _field(page, "items", None)
    if not isinstance(raw_items, (list, tuple)):
        return []
    limit = max(0, _as_int(config.comments_count, 0))
    if limit == 0:
        return []
    max_chars = max(0, _as_int(config.comments_max_chars, 0))
    reply_limit = (
        max(0, _as_int(config.comments_reply_count, 0))
        if config.comments_show_reply
        else 0
    )
    entries: list[dict[str, Any]] = []
    for item in list(raw_items)[:limit]:
        liked = _as_int(_field(item, "liked", 0), 0)
        replies: list[dict[str, str]] = []
        raw_replies = _field(item, "replies", None)
        if reply_limit and isinstance(raw_replies, (list, tuple)):
            for reply in list(raw_replies)[:reply_limit]:
                replies.append(
                    {
                        "user": _text(_field(reply, "user", "")) or _ANONYMOUS_USER,
                        "content": truncate_text(_field(reply, "content", ""), max_chars),
                    }
                )
        entries.append(
            {
                "user": _text(_field(item, "user", "")) or _ANONYMOUS_USER,
                "content": truncate_text(_field(item, "content", ""), max_chars),
                "liked": liked,
                "liked_text": format_likes(liked),
                "avatar_url": _text(_field(item, "avatar_url", "")),
                "time": _text(_field(item, "time", "")),
                "replies": replies,
            }
        )
    return entries


def build_comments_text(song: Any, page: Any, cfg: Any = None) -> str:
    """构造评论 Markdown / 纯文本（t2i 降级文本与最终兜底消息共用）。

    结构::

        歌名 - 歌手
        专辑：x ｜ 时长：3:45 ｜ 网易云音乐

        网易云评论（共 1.2万 条，显示 10 条）

        1. 昵称（赞 1.2万）
           评论正文……
           └ 回复者：回复内容

    没有任何可展示的评论（comments_count 为 0 或列表为空）时返回空串，
    调用方据此跳过发送。
    """
    config = ensure_runtime_config(cfg)
    entries = comment_entries(page, config)
    if not entries:
        return ""
    total = max(0, _as_int(_field(page, "total", 0), 0)) or len(entries)
    summary = f"网易云评论（共 {format_count(total)} 条"
    summary += f"，显示 {len(entries)} 条）" if len(entries) < total else "）"
    parts: list[str] = []
    heading = song_heading(song)
    if heading:
        parts.append(heading)
    meta = song_meta(song)
    if meta:
        parts.append(meta)
    parts.extend(["", summary, ""])
    for index, entry in enumerate(entries, start=1):
        header = f"{index}. {entry['user']}"
        if config.comments_show_likes:
            header += f"（赞 {entry['liked_text']}）"
        parts.append(header)
        for content_line in entry["content"].splitlines() or [""]:
            parts.append(f"   {content_line}")
        for reply in entry["replies"]:
            parts.append(f"   └ {reply['user']}：{reply['content']}")
        parts.append("")
    return "\n".join(parts).strip()
