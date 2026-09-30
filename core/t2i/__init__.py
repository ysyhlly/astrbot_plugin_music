"""歌词与评论的文字转图片（t2i）渲染层。

对外接口（供 core/lyrics_flow.py、core/comments_flow.py 使用）：

- render_lyrics(renderer, song, lyric, cfg) -> str | None：歌词图；
- render_comments(renderer, song, page, cfg) -> str | None：评论图；
- build_lyrics_text(song, lyric, max_lines) -> str：歌词纯文本兜底消息；
- build_comments_text(song, page, cfg) -> str：评论纯文本兜底消息。

设计约束（冻结）：

- 只依赖标准库、jinja2 与 core 包内的 config / models / renderer，不 import astrbot，
  渲染一律走 core.renderer.Renderer 协议（鸭子类型）；
- AstrBot 的自定义 HTML 模板只能走网络 t2i 端点，本地策略只支持 Markdown，
  所以降级链固定为 HTML -> Markdown -> None；
- 模板渲染使用显式 autoescape=True 的 Jinja2 环境，用户可控文本
  （歌名 / 昵称 / 评论正文 / 图片 URL）必须转义。
"""

from __future__ import annotations

from .comments_card import (
    build_comments_data,
    render_comments,
    select_comments_template,
)
from .lyrics_card import build_lyrics_data, render_lyrics, select_lyrics_template
from .plain import (
    EMPTY_COMMENT_TEXT,
    EMPTY_LYRIC_TEXT,
    SOURCE_LABELS,
    build_comments_text,
    build_lyrics_text,
    comment_entries,
    format_count,
    format_likes,
    lyric_lines,
    song_artist,
    song_duration,
    song_heading,
    song_meta,
    song_source,
    truncate_text,
)
from .templates import (
    COMMENTS_TEMPLATE,
    LYRICS_TEMPLATE,
    call_renderer,
    get_environment,
    is_template_renderable,
    render_template,
)

__all__ = [
    "COMMENTS_TEMPLATE",
    "EMPTY_COMMENT_TEXT",
    "EMPTY_LYRIC_TEXT",
    "LYRICS_TEMPLATE",
    "SOURCE_LABELS",
    "build_comments_data",
    "build_comments_text",
    "build_lyrics_data",
    "build_lyrics_text",
    "call_renderer",
    "comment_entries",
    "format_count",
    "format_likes",
    "get_environment",
    "is_template_renderable",
    "lyric_lines",
    "render_comments",
    "render_lyrics",
    "render_template",
    "select_comments_template",
    "select_lyrics_template",
    "song_artist",
    "song_duration",
    "song_heading",
    "song_meta",
    "song_source",
    "truncate_text",
]
