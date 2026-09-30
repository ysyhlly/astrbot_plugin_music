"""网易云评论卡片渲染入口：HTML 模板 -> Markdown -> None 的三级降级。

与 lyrics_card 同构：

- 自定义 HTML/Jinja2 模板只能走网络 t2i 端点，本地策略只支持 Markdown，
  所以降级链固定为 HTML -> Markdown -> None；
- 数据里带 fallback_text，DefaultRenderer 在 mode="local" 时直接用它做本地
  Markdown 渲染；
- 本模块不 import astrbot，只依赖 core.renderer.Renderer 协议。

调用约定：

- cfg 是 core.config.RuntimeConfig（也接受等价 Mapping / None）；
- 没有可展示的评论（comments_count 为 0、列表为空）时返回 None，调用方据此跳过；
- 全部渲染失败时返回 None，调用方按 cfg.comments_fallback_text 决定是否发送
  build_comments_text 生成的纯文本。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..config import RuntimeConfig, ensure_runtime_config
from ..renderer import RenderOptions, Renderer
from .plain import (
    EMPTY_COMMENT_TEXT,
    build_comments_text,
    comment_entries,
    format_count,
    song_artist,
    song_duration,
    song_source,
)
from .templates import (
    COMMENTS_TEMPLATE,
    call_renderer,
    is_template_renderable,
)

logger = logging.getLogger(__name__)

__all__ = ["FOOTER_TEXT", "build_comments_data", "render_comments", "select_comments_template"]

FOOTER_TEXT = "由 AstrBot 点歌插件渲染"
"""卡片页脚文案。"""


def _attr(source: Any, name: str, default: str = "") -> str:
    """兼容 dataclass / Mapping 的字符串取值。"""
    if source is None:
        return default
    value = (
        source.get(name, default)
        if isinstance(source, Mapping)
        else getattr(source, name, default)
    )
    if value is None or isinstance(value, bool):
        return default
    return value.strip() if isinstance(value, str) else str(value).strip()


def _as_int(value: Any, default: int = 0) -> int:
    """安全取整数（非法值给默认值）。"""
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


def _page_field(page: Any, name: str, default: Any = None) -> Any:
    """从 CommentPage（或等价 Mapping）取字段。"""
    if page is None:
        return default
    if isinstance(page, Mapping):
        value = page.get(name, default)
    else:
        value = getattr(page, name, default)
    return default if value is None else value


def build_comments_data(
    song: Any,
    page: Any,
    cfg: RuntimeConfig | Mapping[str, Any] | None = None,
    *,
    fallback_text: str | None = None,
) -> dict[str, Any]:
    """组装 COMMENTS_TEMPLATE 的数据（键名见 core/t2i/templates.py 模块文档）。

    items 已经过 comment_entries 处理：受 comments_count / comments_max_chars /
    comments_show_reply / comments_reply_count 控制。
    """
    config = ensure_runtime_config(cfg)
    entries = comment_entries(page, config)
    text = (
        build_comments_text(song, page, config)
        if fallback_text is None
        else fallback_text
    )
    total = max(0, _as_int(_page_field(page, "total", 0), 0)) or len(entries)
    has_more = bool(_page_field(page, "has_more", False)) or total > len(entries)
    return {
        "title": _attr(song, "name"),
        "artist": song_artist(song),
        "album": _attr(song, "album"),
        "duration": song_duration(song),
        "cover_url": _attr(song, "cover_url"),
        "url": _attr(song, "url"),
        "source": song_source(song),
        "theme": config.comments_theme,
        "width": config.comments_width,
        "font_size": config.comments_font_size,
        "total": total,
        "total_text": format_count(total),
        "shown": len(entries),
        "has_more": has_more,
        "show_avatar": bool(config.comments_show_avatar),
        "show_likes": bool(config.comments_show_likes),
        "show_reply": bool(config.comments_show_reply),
        "items": entries,
        "empty_text": EMPTY_COMMENT_TEXT,
        "footer": FOOTER_TEXT,
        "fallback_text": text,
    }


def select_comments_template(cfg: RuntimeConfig, data: Mapping[str, Any]) -> str:
    """选模板：评论没有独立的自定义模板配置，固定使用内置 COMMENTS_TEMPLATE。

    cfg 与 data 参数保留是为了和 select_lyrics_template 同构，便于未来扩展
    （同时做一次内置模板自检，模板被改坏时能立刻在日志里看到）。
    """
    if not is_template_renderable(COMMENTS_TEMPLATE, data):
        logger.error("内置评论模板渲染自检未通过，仍按原样交给渲染器。")
    return COMMENTS_TEMPLATE


async def render_comments(
    renderer: Renderer | None,
    song: Any,
    page: Any,
    cfg: RuntimeConfig | Mapping[str, Any] | None = None,
) -> str | None:
    """渲染网易云评论图：HTML -> Markdown -> None。

    返回图片 URL / 本地路径；没有可展示的评论或渲染全失败时返回 None。
    """
    config = ensure_runtime_config(cfg)
    entries = comment_entries(page, config)
    if not entries:
        logger.info("没有可展示的评论，跳过 t2i 渲染。")
        return None
    if renderer is None:
        logger.warning("没有可用的渲染器，跳过评论渲染。")
        return None
    text = build_comments_text(song, page, config)
    data = build_comments_data(song, page, config, fallback_text=text)
    template = select_comments_template(config, data)
    options: RenderOptions = config.comments_render_options()

    result = await call_renderer(
        getattr(renderer, "render_html", None), template, data, options
    )
    if result:
        return result
    logger.info("评论 HTML 渲染未成功，降级为 Markdown 渲染。")

    result = await call_renderer(
        getattr(renderer, "render_markdown", None), text, options
    )
    if result:
        return result
    logger.warning("评论渲染全部失败（HTML 与 Markdown 均未返回结果）。")
    return None
