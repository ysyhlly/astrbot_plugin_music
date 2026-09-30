"""网易云评论编排：启用开关 → 取数 → t2i 渲染 → 纯文本兜底。

本模块**不直接碰 HTTP**：取数走 provider.comments(song, transport, limit, offset, sort)，
渲染走 core.renderer.Renderer 协议（ViewportRenderer 复用自 core/lyrics_flow）。

语义区分（重要，两者提示必须不同）
----------------------------------
- provider.comments 返回 **None** = 请求失败（网络/权限/Cookie 等）→ status="fetch_failed"，
  提示引导用户检查 Cookie 或 netease_api_base；
- 返回**空 CommentPage** = 请求成功但没有评论 → status="empty"，提示「这首歌还没有评论」。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import RuntimeConfig, ensure_runtime_config
from .logging_utils import get_logger, log_debug, log_info
from .lyrics_flow import ViewportRenderer
from .models import CommentPage
from .t2i import build_comments_text, render_comments

logger = get_logger("comments_flow")

__all__ = [
    "CommentsFlowResult",
    "comments_offset",
    "run_comments_flow",
    "supports_sort_argument",
]

EMPTY_COMMENT_MESSAGE = "这首歌还没有评论，来做第一个评论的人吧～"
FETCH_FAILED_MESSAGE = (
    "评论获取失败了，稍后再试试～\n"
    "（官方直连常见原因：缺少 Cookie，或该接口需要自建服务；"
    "可在插件配置里填写 netease_api_base 并切换 netease_mode=self_hosted_api）"
)


@dataclass
class CommentsFlowResult:
    """评论流程结果：image 与 text 至多发送一个。"""

    image: str = ""
    text: str = ""
    status: str = ""
    page: CommentPage | None = None
    total: int = 0
    shown: int = 0
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
            "total": self.total,
            "shown": self.shown,
            "text_length": len(self.text),
            "fetched": self.fetched,
        }


def comments_offset(config: RuntimeConfig) -> int:
    """按 comments_page / comments_count 计算分页偏移。"""
    page = max(1, int(getattr(config, "comments_page", 1) or 1))
    count = max(0, int(getattr(config, "comments_count", 0) or 0))
    return max(0, (page - 1) * count)


def supports_sort_argument(method: Any) -> bool:
    """provider.comments 是否支持额外的 sort 关键字（协议本身没有该参数）。"""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):  # pragma: no cover - 内建/C 实现
        return False
    if "sort" in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _clean_result(result: Any) -> str:
    if isinstance(result, str) and result.strip():
        return result.strip()
    return ""


def _as_page(value: Any) -> CommentPage | None:
    """把 provider 返回值规范成 CommentPage（失败给 None）。"""
    if value is None:
        return None
    if isinstance(value, CommentPage):
        return value
    if isinstance(value, Mapping):
        return CommentPage.from_mapping(value)
    return None


async def _fetch_comments(
    config: RuntimeConfig, song: Any, provider: Any, transport: Any
) -> CommentPage | None:
    """取评论：None 表示请求失败（与「成功但无评论」的空页区分开）。"""
    if provider is None or song is None:
        return None
    fetch = getattr(provider, "comments", None)
    if not callable(fetch):
        return None
    kwargs: dict[str, Any] = {
        "limit": config.comments_count,
        "offset": comments_offset(config),
    }
    if supports_sort_argument(fetch):
        kwargs["sort"] = config.comments_sort
    try:
        result = fetch(song, transport, **kwargs)
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("取评论失败：%r", exc)
        return None
    return _as_page(result)


async def _render_comment_image(
    config: RuntimeConfig, song: Any, page: CommentPage, renderer: Any
) -> str:
    wrapped = ViewportRenderer(renderer, width=config.comments_width)
    try:
        return _clean_result(await render_comments(wrapped, song, page, config))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("评论渲染失败：%r", exc)
        return ""


def _fallback_text(config: RuntimeConfig, song: Any, page: CommentPage) -> str:
    if not config.comments_fallback_text:
        return ""
    try:
        return build_comments_text(song, page, config) or ""
    except Exception as exc:  # pragma: no cover - 文本兜底异常
        logger.warning("生成评论纯文本失败：%r", exc)
        return ""


async def run_comments_flow(
    cfg: Any,
    song: Any = None,
    provider: Any = None,
    transport: Any = None,
    renderer: Any = None,
    *,
    page: Any = None,
) -> CommentsFlowResult | None:
    """评论流程：关闭时返回 None；否则返回「图片或纯文本」二选一的结果。

    - cfg.comments_enable=False：直接返回 None（**不发起任何取数调用**）；
    - cfg.comments_count=0：视为用户不要评论，status="disabled"，不取数；
    - provider 返回 None：status="fetch_failed" + 可读失败提示；
    - provider 返回空页：status="empty" + 「还没有评论」提示；
    - cfg.comments_t2i=False：status="text"，发送明确选择的纯文本；
    - 渲染失败：status="render_failed" + build_comments_text 纯文本兜底。
    """
    config = ensure_runtime_config(cfg)
    if not config.comments_enable:
        log_debug(config, "评论功能已关闭，跳过取评论与渲染。", logger=logger)
        return None
    if int(config.comments_count or 0) <= 0:
        log_debug(config, "comments_count=0，用户不需要评论，跳过。", logger=logger)
        return CommentsFlowResult(status="disabled")

    data = _as_page(page)
    fetched = data is not None
    if data is None:
        data = await _fetch_comments(config, song, provider, transport)
        fetched = data is not None

    if data is None:
        # 请求失败：与「没有评论」是两件事
        return CommentsFlowResult(
            status="fetch_failed",
            text=FETCH_FAILED_MESSAGE if config.comments_fallback_text else "",
            fetched=False,
        )
    if data.is_empty():
        return CommentsFlowResult(
            status="empty",
            text=EMPTY_COMMENT_MESSAGE if config.comments_fallback_text else "",
            page=data,
            total=data.total,
            shown=0,
            fetched=fetched,
        )

    if config.comments_t2i and renderer is not None:
        image = await _render_comment_image(config, song, data, renderer)
        if image:
            return CommentsFlowResult(
                image=image,
                status="ok",
                page=data,
                total=data.total,
                shown=len(data.items),
                fetched=fetched,
            )
        log_info(config, "评论图渲染未成功，走纯文本兜底。", logger=logger)
    else:
        log_debug(config, "comments_t2i 关闭或没有渲染器，直接使用纯文本评论。", logger=logger)

    return CommentsFlowResult(
        status="render_failed" if config.comments_t2i else "text",
        text=(
            _fallback_text(config, song, data) if config.comments_t2i
            else build_comments_text(song, data, config)
        ),
        page=data,
        total=data.total,
        shown=len(data.items),
        fetched=fetched,
    )
