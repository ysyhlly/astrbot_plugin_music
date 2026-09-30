"""渲染器协议与安全默认实现。

契约（冻结）：
- RenderOptions(width=900, theme="light", font_size=22, line_spacing=1.6,
  max_lines=0, endpoint="", mode="auto", timeout=15.0, return_url=True)
- Renderer: async render_html(template, data, options) / async render_markdown(md, options)
  两者都返回 str | None，失败返回 None。
- DefaultRenderer(star)：用 star.html_render(...) / star.text_to_image(...) 渲染，
  遵守 options.mode（network/local/auto），任何异常都记日志并返回 None，绝不抛出。

mode 语义：
- "network"：只走网络渲染（优先 options.endpoint 直连 t2i 服务，其次 star.html_render）；
- "local"：只走本地渲染（star.text_to_image，用模板数据降级出的文本）；
- "auto"：先试网络，失败后用本地兜底。
"""

from __future__ import annotations

import inspect
import logging
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODE",
    "RENDER_MODES",
    "SCREENSHOT_OPTIONS",
    "DefaultRenderer",
    "RenderOptions",
    "Renderer",
    "local_render_strategy",
    "normalize_mode",
]


def local_render_strategy() -> Any:
    """返回 AstrBot 的本地（Pillow）渲染策略，拿不到时返回 None。

    单独抽成模块级函数是为了可注入：测试可用 monkeypatch 替换它，
    从而在「有真实 astrbot」与「没有 astrbot」两种环境下都能稳定断言。
    """
    try:
        from astrbot.core import html_renderer  # 延迟 import
    except Exception:
        return None
    return getattr(html_renderer, "local_strategy", None)

RENDER_MODES: tuple[str, ...] = ("network", "local", "auto")
"""支持的渲染模式。"""

DEFAULT_MODE = "auto"

SCREENSHOT_OPTIONS: dict[str, Any] = {"full_page": True, "type": "jpeg", "quality": 80}
"""透传给 t2i 服务的 Playwright screenshot 选项默认值。"""

_MAX_FALLBACK_CHARS = 8000


def normalize_mode(mode: Any) -> str:
    """把任意值规范成 network/local/auto，非法值回退 auto。"""
    text = str(mode or "").strip().lower()
    return text if text in RENDER_MODES else DEFAULT_MODE


def _int_or(value: Any, default: int) -> int:
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


def _float_or(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def _bool_or(value: Any, default: bool) -> bool:
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


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


@dataclass
class RenderOptions:
    """渲染参数（模板可直接用 to_dict() 作为 Jinja2 数据的一部分）。"""

    width: int = 900
    theme: str = "light"
    font_size: int = 22
    line_spacing: float = 1.6
    max_lines: int = 0
    endpoint: str = ""
    mode: str = "auto"
    timeout: float = 15.0
    return_url: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.width = _clamp(_int_or(self.width, 900), 100, 8192)
        theme = str(self.theme or "").strip().lower()
        self.theme = theme if theme in {"light", "dark"} else "light"
        self.font_size = _clamp(_int_or(self.font_size, 22), 8, 200)
        self.line_spacing = min(6.0, max(0.5, _float_or(self.line_spacing, 1.6)))
        self.max_lines = max(0, _int_or(self.max_lines, 0))
        self.endpoint = str(self.endpoint or "").strip()
        self.mode = normalize_mode(self.mode)
        self.timeout = max(0.5, _float_or(self.timeout, 15.0))
        self.return_url = _bool_or(self.return_url, True)
        self.extra = dict(self.extra) if isinstance(self.extra, Mapping) else {}

    def to_dict(self) -> dict[str, Any]:
        """转成普通 dict（含 extra 的副本）。"""
        return asdict(self)

    def screenshot_options(self) -> dict[str, Any]:
        """t2i 服务的截图选项（默认值 + extra 覆盖）。"""
        options = dict(SCREENSHOT_OPTIONS)
        options.update(self.extra)
        return options

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> RenderOptions:
        """从映射构造（未知键忽略，非法类型自动纠正，永不抛异常）。"""
        values: dict[str, Any] = {}
        if isinstance(data, Mapping):
            for name in _KNOWN_FIELDS:
                if name in data and data[name] is not None:
                    values[name] = data[name]
        for name, value in overrides.items():
            if name in _KNOWN_FIELDS and value is not None:
                values[name] = value
        return cls(**values)


_KNOWN_FIELDS = (
    "width",
    "theme",
    "font_size",
    "line_spacing",
    "max_lines",
    "endpoint",
    "mode",
    "timeout",
    "return_url",
    "extra",
)


@runtime_checkable
class Renderer(Protocol):
    """渲染器协议：两种渲染入口，失败一律返回 None。"""

    async def render_html(
        self,
        template: str,
        data: Mapping[str, Any] | None = None,
        options: RenderOptions | None = None,
    ) -> str | None:
        """渲染 Jinja2/HTML 模板，返回图片 URL 或本地路径。"""
        ...

    async def render_markdown(
        self,
        md: str,
        options: RenderOptions | None = None,
    ) -> str | None:
        """渲染 Markdown 文本，返回图片 URL 或本地路径。"""
        ...


class DefaultRenderer:
    """基于 Star.html_render / Star.text_to_image 的安全渲染器。

    star 可以是任意鸭子类型对象（测试里用假对象即可）；为 None 或缺方法时
    相应渲染路径直接返回 None。
    """

    def __init__(self, star: Any = None, logger_: logging.Logger | None = None) -> None:
        self.star = star
        self.logger = logger_ or logger

    async def render_html(
        self,
        template: str,
        data: Mapping[str, Any] | None = None,
        options: RenderOptions | None = None,
    ) -> str | None:
        """按 options.mode 渲染 HTML 模板；任何异常都吞掉并返回 None。"""
        try:
            opts = _as_options(options)
            if not isinstance(template, str) or not template.strip():
                self.logger.warning("render_html 收到空模板，跳过渲染。")
                return None
            payload = _as_payload(data)
            mode = normalize_mode(opts.mode)
            if mode in {"network", "auto"}:
                result = await self._render_via_endpoint(template, payload, opts)
                if not result:
                    result = await self._render_via_star_html(template, payload, opts)
                if result:
                    return result
                if mode == "network":
                    return None
            text = _fallback_text(payload)
            if not text:
                if mode == "local":
                    self.logger.warning("本地渲染缺少可降级的文本内容，跳过渲染。")
                return None
            # mode="local" 时优先走真正的本地 Pillow 渲染。
            # 注意：star.text_to_image 在 AstrBot 里最终会调到**网络**策略
            # (HtmlRenderer.render_t2i -> network_strategy.render)，所以它并不是
            # 本地渲染——实测网络一次 3~6s，而本地 Pillow 约 0.2s。local 模式若只依赖
            # star.text_to_image，就等于没生效（渲染依然走网络、依旧慢）。
            if mode == "local":
                local = await self._render_via_local_strategy(text)
                if local:
                    return local
            return await self._render_via_star_markdown(text, opts)
        except Exception as exc:  # 双保险：绝不上抛
            self.logger.error("render_html 渲染失败：%s", exc, exc_info=True)
            return None

    async def render_markdown(
        self,
        md: str,
        options: RenderOptions | None = None,
    ) -> str | None:
        """用 Star.text_to_image 渲染 Markdown；任何异常都吞掉并返回 None。"""
        try:
            opts = _as_options(options)
            if not isinstance(md, str) or not md.strip():
                self.logger.warning("render_markdown 收到空文本，跳过渲染。")
                return None
            if normalize_mode(opts.mode) == "local":
                local = await self._render_via_local_strategy(md)
                if local:
                    return local
            return await self._render_via_star_markdown(md, opts)
        except Exception as exc:  # 双保险：绝不上抛
            self.logger.error("render_markdown 渲染失败：%s", exc, exc_info=True)
            return None

    # ------------------------------------------------------------------ 内部

    async def _render_via_star_html(
        self,
        template: str,
        payload: dict[str, Any],
        opts: RenderOptions,
    ) -> str | None:
        star = self.star
        render = getattr(star, "html_render", None) if star is not None else None
        if not callable(render):
            self.logger.debug("star 不支持 html_render，跳过网络 HTML 渲染。")
            return None
        options = opts.screenshot_options()
        try:
            result = render(
                template,
                payload,
                return_url=opts.return_url,
                options=options,
            )
            result = await result if inspect.isawaitable(result) else result
        except TypeError as exc:
            # 兼容旧签名（没有 options 关键字参数）
            self.logger.debug("html_render 签名不兼容（%s），改用最小参数重试。", exc)
            try:
                result = render(template, payload, return_url=opts.return_url)
                result = await result if inspect.isawaitable(result) else result
            except Exception as retry_exc:
                self.logger.error("star.html_render 渲染失败：%s", retry_exc)
                return None
        except Exception as exc:
            self.logger.error("star.html_render 渲染失败：%s", exc)
            return None
        return _clean_result(result)

    async def _render_via_star_markdown(self, text: str, opts: RenderOptions) -> str | None:
        star = self.star
        convert = getattr(star, "text_to_image", None) if star is not None else None
        if not callable(convert):
            self.logger.debug("star 不支持 text_to_image，跳过本地/文本渲染。")
            return None
        try:
            result = convert(text, return_url=opts.return_url)
            result = await result if inspect.isawaitable(result) else result
        except TypeError as exc:
            self.logger.debug("text_to_image 签名不兼容（%s），改用最小参数重试。", exc)
            try:
                result = convert(text)
                result = await result if inspect.isawaitable(result) else result
            except Exception as retry_exc:
                self.logger.error("star.text_to_image 渲染失败：%s", retry_exc)
                return None
        except Exception as exc:
            self.logger.error("star.text_to_image 渲染失败：%s", exc)
            return None
        return _clean_result(result)

    async def _render_via_local_strategy(self, text: str) -> str | None:
        """用 AstrBot 的本地 Pillow 策略渲染 Markdown（不联网，实测约 0.2s）。

        这是 local 模式真正的本地实现：ImportError/属性缺失一律返回 None，
        让调用方回退到 star.text_to_image。绝不抛出。
        """
        strategy = local_render_strategy()
        if strategy is None:
            self.logger.debug("无本地渲染策略，跳过本地 Pillow 渲染。")
            return None
        render = getattr(strategy, "render", None)
        if not callable(render):
            self.logger.debug("本地策略不支持 render，跳过。")
            return None
        try:
            result = render(text, return_url=False)
            result = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            self.logger.warning("本地 Pillow 渲染失败，回退网络/文本路径：%s", exc)
            return None
        return _clean_result(result)

    async def _render_via_endpoint(
        self,
        template: str,
        payload: dict[str, Any],
        opts: RenderOptions,
    ) -> str | None:
        """直连自建 t2i 端点（{endpoint}/text2img/generate），与 AstrBot 协议一致。"""
        endpoint = opts.endpoint
        if not endpoint:
            return None
        base = endpoint.rstrip("/")
        if not base.endswith("text2img"):
            base = f"{base}/text2img"
        post_data = {
            "tmpl": template,
            "json": bool(opts.return_url),
            "tmpldata": payload,
            "options": opts.screenshot_options(),
        }
        data: Any = None
        raw: bytes = b""
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=max(0.5, float(opts.timeout)))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/generate", json=post_data) as resp:
                    if resp.status != 200:
                        self.logger.error(
                            "t2i 端点 %s 返回 HTTP %s。", base, resp.status
                        )
                        return None
                    if opts.return_url:
                        data = await resp.json(content_type=None)
                    else:
                        raw = await resp.read()
                        data = None
        except Exception as exc:
            self.logger.error("t2i 端点 %s 渲染失败：%s", base, exc)
            return None
        if opts.return_url:
            image_id = ""
            if isinstance(data, Mapping):
                inner = data.get("data")
                if isinstance(inner, Mapping):
                    image_id = str(inner.get("id") or "")
                if not image_id:
                    image_id = str(data.get("id") or "")
            if not image_id:
                self.logger.error("t2i 端点 %s 返回内容缺少图片 id。", base)
                return None
            return f"{base}/{image_id}"
        if not raw:
            self.logger.error("t2i 端点 %s 返回空图片数据。", base)
            return None
        return _save_temp_image(raw, self.logger)


def _as_options(options: RenderOptions | Mapping[str, Any] | None) -> RenderOptions:
    if isinstance(options, RenderOptions):
        return options
    if isinstance(options, Mapping):
        return RenderOptions.from_mapping(options)
    return RenderOptions()


def _as_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    if data is None:
        return {}
    return {"data": data}


def _clean_result(result: Any) -> str | None:
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        text = str(item.get("text") or item.get("content") or "").strip()
        translation = str(item.get("translation") or "").strip()
        if text and translation:
            return f"{text}  {translation}"
        if text:
            return text
        user = str(item.get("user") or "").strip()
        if user:
            return f"{user}: {translation or text}".strip()
    return ""


def _fallback_text(payload: Mapping[str, Any]) -> str:
    """从模板数据里抽出一段可降级渲染的 Markdown 文本。"""
    for key in ("fallback_text", "markdown", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_FALLBACK_CHARS]
    parts: list[str] = []
    for key in ("title", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"## {value.strip()}")
            break
    for key in ("subtitle", "artist_text", "album", "meta", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key in ("lines", "items", "comments"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                text = _item_text(item)
                if text:
                    parts.append(text)
    return "\n\n".join(parts)[:_MAX_FALLBACK_CHARS].strip()


def _save_temp_image(raw: bytes, log: logging.Logger) -> str | None:
    """把图片字节写入临时文件并返回路径。"""
    try:
        directory = Path(tempfile.gettempdir()) / "astrbot_plugin_music_t2i"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"t2i_{uuid.uuid4().hex}.jpg"
        path.write_bytes(raw)
        return str(path)
    except Exception as exc:  # pragma: no cover - 磁盘异常
        log.error("保存 t2i 图片失败：%s", exc)
        return None
