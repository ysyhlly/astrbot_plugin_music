"""歌词卡片渲染入口：HTML 模板 -> 安全文本图 -> None 的降级链。

AstrBot 侧事实（t2i/renderer.py、network_strategy.py、local_strategy.py）：

- 自定义 HTML/Jinja2 模板只能走网络 t2i 端点（HtmlRenderer.render_custom_template
  -> NetworkRenderStrategy），本地策略只有 Pillow + Markdown 实现；
- 模板数据里要带 fallback_text，DefaultRenderer 在 mode="local" 时直接绘制
  纯文本；旧渲染器使用经过实体转义的 Markdown，歌词不能触发图片 URL 加载。

调用约定：

- renderer 是 core.renderer.Renderer 协议对象（鸭子类型，默认实现为
  DefaultRenderer）；本模块不 import astrbot。
- cfg 是 core.config.RuntimeConfig（也接受等价 Mapping / None，内部走
  ensure_runtime_config）。
- 歌词为空（没有正文也没有翻译）时直接返回 None，避免发一张空图；调用方据此
  提示「暂无歌词」。
- 全部渲染失败时返回 None，调用方按 cfg.lyrics_fallback_text 决定是否发送
  build_lyrics_text 生成的纯文本。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..config import RuntimeConfig, ensure_runtime_config
from ..renderer import RenderOptions, Renderer
from .plain import (
    EMPTY_LYRIC_TEXT,
    build_lyrics_text,
    lyric_lines,
    song_artist,
    song_duration,
    song_source,
)
from .templates import (
    LYRICS_TEMPLATE,
    call_renderer,
    escape_markdown_text,
    is_template_renderable,
)

logger = logging.getLogger(__name__)

__all__ = ["FOOTER_TEXT", "build_lyrics_data", "render_lyrics", "select_lyrics_template"]

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


def build_lyrics_data(
    song: Any,
    lyric: Any,
    cfg: RuntimeConfig | Mapping[str, Any] | None = None,
    *,
    fallback_text: str | None = None,
) -> dict[str, Any]:
    """组装 LYRICS_TEMPLATE 的数据（键名见 core/t2i/templates.py 模块文档）。

    fallback_text 缺省时用 build_lyrics_text(song, lyric, cfg.lyrics_max_lines) 现算，
    它既是 mode="local" 的本地纯文本素材，也是最终纯文本兜底消息。
    """
    config = ensure_runtime_config(cfg)
    lines = lyric_lines(lyric)
    text = (
        build_lyrics_text(song, lyric, config.lyrics_max_lines)
        if fallback_text is None
        else fallback_text
    )
    return {
        "title": _attr(song, "name"),
        "artist": song_artist(song),
        "album": _attr(song, "album"),
        "duration": song_duration(song),
        "cover_url": _attr(song, "cover_url"),
        "url": _attr(song, "url"),
        "source": song_source(song),
        "theme": config.lyrics_theme,
        "width": config.lyrics_width,
        "font_size": config.lyrics_font_size,
        "line_spacing": config.lyrics_line_spacing,
        "max_lines": config.lyrics_max_lines,
        "show_meta": bool(config.lyrics_show_meta),
        "highlight_translation": bool(config.lyrics_highlight_translation),
        "lines": lines,
        "total_lines": len(lines),
        "empty_text": EMPTY_LYRIC_TEXT,
        "footer": FOOTER_TEXT,
        "fallback_text": text,
    }


def select_lyrics_template(cfg: RuntimeConfig, data: Mapping[str, Any]) -> str:
    """选模板：cfg.lyrics_template 非空且能渲染时用用户模板，否则用内置模板。

    先用 autoescape=True 的环境做一次本地自检，用户模板写坏了也不至于没有歌词图。
    """
    custom = (getattr(cfg, "lyrics_template", "") or "").strip()
    if not custom:
        return LYRICS_TEMPLATE
    if is_template_renderable(custom, data):
        return custom
    logger.warning("自定义歌词模板无法渲染，已回退内置模板。")
    return LYRICS_TEMPLATE


async def render_lyrics(
    renderer: Renderer | None,
    song: Any,
    lyric: Any,
    cfg: RuntimeConfig | Mapping[str, Any] | None = None,
) -> str | None:
    """渲染歌词图：HTML -> 纯文本图（兼容旧 Markdown 渲染器）-> None。

    返回图片 URL / 本地路径；任一环节失败都记日志并进入下一级，全部失败返回 None。
    """
    config = ensure_runtime_config(cfg)
    lines = lyric_lines(lyric)
    if not lines:
        logger.info("歌词为空，跳过 t2i 渲染。")
        return None
    if renderer is None:
        logger.warning("没有可用的渲染器，跳过歌词渲染。")
        return None
    text = build_lyrics_text(song, lyric, config.lyrics_max_lines)
    data = build_lyrics_data(song, lyric, config, fallback_text=text)
    template = select_lyrics_template(config, data)
    options: RenderOptions = config.lyrics_render_options()

    result = await call_renderer(
        getattr(renderer, "render_html", None), template, data, options
    )
    if result:
        return result
    logger.info("歌词 HTML 渲染未成功，尝试安全文本图片。")

    result = await call_renderer(getattr(renderer, "render_text", None), text, options)
    if not result:
        result = await call_renderer(
            getattr(renderer, "render_markdown", None), escape_markdown_text(text), options
        )
    if result:
        return result
    logger.warning("歌词渲染全部失败（HTML 与文本图片均未返回结果）。")
    return None
