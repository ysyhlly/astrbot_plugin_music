"""歌词编排：启用开关 → 取数 → t2i 渲染 → 纯文本兜底。

本模块**不直接碰 HTTP**：取数一律走 provider.lyrics(song, transport)（transport 由
main.py 的 transport_session 提供），渲染一律走 core.renderer.Renderer 协议。

viewport 注入（实测缺陷修复）
-----------------------------
官方 t2i 服务在请求没带 viewport 时回退 800x720，而 full_page=True 的语义是
「只扩不裁」（内容矮于视口时 scrollHeight 被夹到视口高度），于是出图底部会有大片
空白、cfg.lyrics_width 也完全不生效。core/t2i 的 render_lyrics 内部用
cfg.lyrics_render_options() 组装 RenderOptions，调用点无法直接塞 extra，所以这里用
ViewportRenderer 包一层：在交给渲染器之前，把 viewport_width / viewport_height
合并进 RenderOptions.extra（screenshot_options() 的实现是「默认值 + extra 覆盖」），
让宽度配置真正生效、高度精确贴合内容。

兼容性兜底：端点若拒绝未知键（pydantic extra=forbid / 老版本服务），带 viewport 的
请求会失败；此时自动去掉 viewport 再试一次，仍失败才进入 core/t2i 的 Markdown/纯文本
降级链。class ViewportRenderer 同时被 core/comments_flow.py 复用。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import RuntimeConfig, ensure_runtime_config
from .logging_utils import get_logger, log_debug, log_info
from .models import Lyric, SongInfo
from .renderer import RenderOptions, normalize_mode
from .t2i import build_lyrics_text, render_lyrics

logger = get_logger("lyrics_flow")

__all__ = [
    "VIEWPORT_HEIGHT",
    "LyricsFlowResult",
    "ViewportRenderer",
    "resolve_renderer",
    "run_lyrics_flow",
    "with_viewport",
]

VIEWPORT_HEIGHT = 100
"""注入给 t2i 服务的视口高度（小值 + full_page=True => 出图高度贴合内容）。"""

MAX_VIEWPORT_WIDTH = 4096
MIN_VIEWPORT_WIDTH = 200

_REJECTED_ENDPOINTS: set[tuple[str, str]] = set()
"""已知会拒绝 viewport 参数的 (endpoint, mode) 组合（跨事件复用，避免重复踩坑）。"""

_MAX_REJECTED_ENDPOINTS = 32


def _as_render_options(options: Any) -> RenderOptions:
    if isinstance(options, RenderOptions):
        return options
    if isinstance(options, Mapping):
        return RenderOptions.from_mapping(options)
    return RenderOptions()


def _clean_result(result: Any) -> str:
    if isinstance(result, str) and result.strip():
        return result.strip()
    return ""


def _viewport_key(options: RenderOptions) -> tuple[str, str]:
    return (str(options.endpoint or ""), str(options.mode or ""))


def with_viewport(options: Any, width: Any, height: Any = VIEWPORT_HEIGHT) -> RenderOptions:
    """复制一份 RenderOptions 并把 viewport 合并进 extra（不改原对象）。"""
    base = _as_render_options(options)
    try:
        extra = dict(base.extra)
    except Exception:  # pragma: no cover - 兜底
        extra = {}
    try:
        value = int(float(width))
    except (TypeError, ValueError):
        value = 800
    extra["viewport_width"] = max(MIN_VIEWPORT_WIDTH, min(MAX_VIEWPORT_WIDTH, value))
    try:
        height_value = int(float(height))
    except (TypeError, ValueError):
        height_value = VIEWPORT_HEIGHT
    extra["viewport_height"] = max(50, min(4096, height_value))
    return RenderOptions.from_mapping(base.to_dict(), extra=extra)


def _mark_rejected(options: RenderOptions) -> None:
    key = _viewport_key(options)
    if key in _REJECTED_ENDPOINTS:
        return
    if len(_REJECTED_ENDPOINTS) >= _MAX_REJECTED_ENDPOINTS:
        _REJECTED_ENDPOINTS.pop()
    _REJECTED_ENDPOINTS.add(key)
    logger.info("t2i 端点 %s 不接受 viewport 参数，后续请求将不再注入。", key[0] or "(内置)")


@dataclass
class ViewportRenderer:
    """给底层渲染器注入 viewport 的包装器（Renderer 协议鸭子类型）。

    - render_html：注入 viewport 后调用；失败时去掉 viewport 重试一次
      （端点拒绝未知键的场景），第二次结果作为最终结果；
    - render_markdown：同样处理（本地渲染不依赖 viewport，注入无副作用）；
    - 任何异常都吞掉返回 None，不破坏 core/t2i 的三级降级链。
    """

    renderer: Any
    width: int = 800
    height: int = VIEWPORT_HEIGHT
    retry_without_viewport: bool = True
    _rejected: bool = field(default=False, init=False, repr=False)

    async def render_html(
        self,
        template: str,
        data: Mapping[str, Any] | None = None,
        options: RenderOptions | None = None,
    ) -> str | None:
        return await self._render("render_html", (template, data), options)

    async def render_markdown(self, md: str, options: RenderOptions | None = None) -> str | None:
        return await self._render("render_markdown", (md,), options)

    async def render_text(self, text: str, options: RenderOptions | None = None) -> str | None:
        """Forward the optional plain-text renderer without interpreting Markdown."""
        return await self._render("render_text", (text,), options)

    # ------------------------------------------------------------------ 内部

    async def _render(self, name: str, args: tuple[Any, ...], options: Any) -> str | None:
        method = getattr(self.renderer, name, None)
        if not callable(method):
            return None
        base = _as_render_options(options)
        if self._skip_viewport(base):
            return await self._call(method, args, base)
        injected = with_viewport(base, self.width, self.height)
        result = await self._call(method, args, injected)
        if result:
            return result
        if not self.retry_without_viewport or normalize_mode(base.mode) == "local":
            return None
        log_info(
            self.renderer,
            "带 viewport 的 t2i 渲染未成功，去掉 viewport 重试一次。",
            logger=logger,
        )
        result = await self._call(method, args, base)
        if result:
            self._rejected = True
            _mark_rejected(base)
        return result

    def _skip_viewport(self, options: RenderOptions) -> bool:
        if self._rejected:
            return True
        return _viewport_key(options) in _REJECTED_ENDPOINTS

    async def _call(self, method: Any, args: tuple[Any, ...], options: RenderOptions) -> str | None:
        try:
            result = method(*args, options)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("渲染调用 %r 失败：%r", getattr(method, "__name__", method), exc)
            return None
        return _clean_result(result)


def resolve_renderer(cfg: Any, renderer: Any = None) -> Any | None:
    """按配置给渲染器套上 viewport 注入（renderer 为空时返回 None）。"""
    if renderer is None:
        return None
    config = ensure_runtime_config(cfg)
    return ViewportRenderer(renderer, width=config.lyrics_width)


# --------------------------------------------------------------------- 结果


@dataclass
class LyricsFlowResult:
    """歌词流程结果：image 与 text 至多发送一个。"""

    image: str = ""
    text: str = ""
    status: str = ""
    lyric: Lyric | None = None
    fetched: bool = False

    @property
    def has_image(self) -> bool:
        return bool(self.image)

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "has_image": self.has_image,
            "has_text": self.has_text,
            "text_length": len(self.text),
            "fetched": self.fetched,
        }


def _as_lyric(value: Any) -> Lyric | None:
    """把 provider 返回值规范成 Lyric（失败返回 None）。"""
    if value is None:
        return None
    if isinstance(value, Lyric):
        return value
    if isinstance(value, Mapping):
        return Lyric.from_mapping(value)
    return None


async def _fetch_lyric(song: Any, provider: Any, transport: Any) -> Lyric | None:
    """取歌词（provider 缺失/异常都返回 None，不抛异常）。"""
    if provider is None:
        return None
    fetch = getattr(provider, "lyrics", None)
    if not callable(fetch):
        return None
    try:
        result = fetch(song, transport)
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("取歌词失败：%r", exc)
        return None
    return _as_lyric(result)


async def _render_lyric_image(
    config: RuntimeConfig, song: Any, lyric: Lyric, renderer: Any
) -> str:
    """渲染歌词图（任何异常都返回空串，由调用方走纯文本兜底）。"""
    wrapped = resolve_renderer(config, renderer)
    if wrapped is None:
        return ""
    try:
        return _clean_result(await render_lyrics(wrapped, song, lyric, config))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("歌词渲染失败：%r", exc)
        return ""


def _empty_message(config: RuntimeConfig) -> str:
    """没有歌词时的提示（受 lyrics_fallback_text 控制）。"""
    if not config.lyrics_fallback_text:
        return ""
    return "这首歌暂时还没有歌词～"


async def run_lyrics_flow(
    cfg: Any,
    song: Any = None,
    provider: Any = None,
    transport: Any = None,
    renderer: Any = None,
    *,
    lyric: Any = None,
) -> LyricsFlowResult | None:
    """歌词流程：关闭时返回 None；否则返回「图片或纯文本」二选一的结果。

    - cfg.lyrics_enable=False：直接返回 None（**不发起任何取数调用**）；
    - 取不到歌词 / 歌词为空：status="empty"，按 lyrics_fallback_text 给提示；
    - cfg.lyrics_t2i=False：status="text"，发送明确选择的纯文本；
    - 渲染失败：status="render_failed"，按 lyrics_fallback_text 给纯文本。
    """
    config = ensure_runtime_config(cfg)
    if not config.lyrics_enable:
        log_debug(config, "歌词功能已关闭，跳过取歌词与渲染。", logger=logger)
        return None

    data = _as_lyric(lyric)
    fetched = data is not None
    if data is None:
        data = await _fetch_lyric(song, provider, transport)
        fetched = data is not None

    if data is None or data.is_empty():
        return LyricsFlowResult(status="empty", text=_empty_message(config), lyric=data, fetched=fetched)

    explicit_text = not config.lyrics_t2i
    if config.lyrics_t2i and renderer is not None:
        image = await _render_lyric_image(config, song, data, renderer)
        if image:
            return LyricsFlowResult(image=image, status="ok", lyric=data, fetched=fetched)
        log_info(config, "歌词图渲染未成功，走纯文本兜底。", logger=logger)
    else:
        log_debug(config, "lyrics_t2i 关闭或没有渲染器，直接使用纯文本歌词。", logger=logger)

    text = ""
    if explicit_text or config.lyrics_fallback_text:
        try:
            text = build_lyrics_text(song, data, config.lyrics_max_lines) or ""
        except Exception as exc:  # pragma: no cover - 文本兜底异常
            logger.warning("生成歌词纯文本失败：%r", exc)
            text = ""
    return LyricsFlowResult(
        status="text" if explicit_text else "render_failed", text=text, lyric=data, fetched=fetched
    )
