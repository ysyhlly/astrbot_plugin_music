"""DefaultRenderer 安全性：任何异常都返回 None，绝不上抛。"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest

import core.renderer as renderer_module
from core.renderer import (
    RENDER_MODES,
    DefaultRenderer,
    RenderOptions,
    Renderer,
    normalize_mode,
)


@pytest.fixture(autouse=True)
def _no_local_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认把「本地 Pillow 策略」置为不可用。

    本文件多数用例断言的是 star 委派路径；若环境里恰好有真实 AstrBot
    （tests/conftest.py 会注入 ASTRBOT_REF），本地策略会先成功返回，
    让这些断言失去确定性。需要验证本地路径的用例自行覆盖本 fixture。
    """
    monkeypatch.setattr(renderer_module, "local_render_strategy", lambda: None)


class FakeStar:
    """可编程的假 Star 对象：分别控制 html_render / text_to_image 的行为。"""

    def __init__(
        self,
        *,
        html_result: Any = "https://img.example.com/card.png",
        html_error: Exception | None = None,
        text_result: Any = "https://img.example.com/text.png",
        text_error: Exception | None = None,
    ) -> None:
        self.html_result = html_result
        self.html_error = html_error
        self.text_result = text_result
        self.text_error = text_error
        self.html_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    async def html_render(
        self,
        tmpl: str,
        data: dict[str, Any],
        return_url: bool = True,
        options: dict[str, Any] | None = None,
    ) -> Any:
        self.html_calls.append(
            {"tmpl": tmpl, "data": data, "return_url": return_url, "options": options}
        )
        if self.html_error is not None:
            raise self.html_error
        return self.html_result

    async def text_to_image(self, text: str, return_url: bool = True) -> Any:
        self.text_calls.append({"text": text, "return_url": return_url})
        if self.text_error is not None:
            raise self.text_error
        return self.text_result


def test_default_renderer_satisfies_renderer_protocol() -> None:
    assert isinstance(DefaultRenderer(FakeStar()), Renderer)
    assert isinstance(DefaultRenderer(None), Renderer)


def test_render_options_defaults_and_normalisation() -> None:
    options = RenderOptions()
    assert (options.width, options.theme, options.font_size) == (900, "light", 22)
    assert options.line_spacing == 1.6
    assert options.max_lines == 0
    assert options.endpoint == ""
    assert options.mode == "auto"
    assert options.timeout == 15.0
    assert options.return_url is True

    weird = RenderOptions(
        width="abc",  # type: ignore[arg-type]
        theme="neon",  # type: ignore[arg-type]
        font_size=None,  # type: ignore[arg-type]
        line_spacing="x",  # type: ignore[arg-type]
        max_lines=-5,
        mode="CLOUD",  # type: ignore[arg-type]
        timeout="nope",  # type: ignore[arg-type]
        return_url="no",  # type: ignore[arg-type]
        extra="nope",  # type: ignore[arg-type]
    )
    assert weird.width == 900
    assert weird.theme == "light"
    assert weird.font_size == 22
    assert weird.line_spacing == 1.6
    assert weird.max_lines == 0
    assert weird.mode == "auto"
    assert weird.timeout == 15.0
    assert weird.return_url is False
    assert weird.extra == {}


def test_render_options_from_mapping_ignores_unknown_keys() -> None:
    options = RenderOptions.from_mapping({"width": 500, "unknown": 1, "mode": "local"})
    assert options.width == 500
    assert options.mode == "local"
    assert RenderOptions.from_mapping(None).width == 900
    assert RenderOptions.from_mapping({"width": 500}, width=640).width == 640


def test_normalize_mode() -> None:
    assert normalize_mode("network") == "network"
    assert normalize_mode(" LOCAL ") == "local"
    assert normalize_mode("weird") == "auto"
    assert normalize_mode(None) == "auto"
    assert normalize_mode(1) == "auto"
    assert set(RENDER_MODES) == {"network", "local", "auto"}


@pytest.mark.asyncio
async def test_render_html_returns_none_when_star_raises() -> None:
    star = FakeStar(html_error=RuntimeError("boom"))
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div>{{ name }}</div>", {"name": "x"}, RenderOptions(mode="network")
    )
    assert result is None
    assert star.html_calls, "应当尝试过调用 star.html_render"


@pytest.mark.asyncio
async def test_render_html_returns_string_on_success() -> None:
    star = FakeStar(html_result="https://img.example.com/card.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div>{{ name }}</div>",
        {"name": "x"},
        RenderOptions(mode="network", return_url=True),
    )
    assert result == "https://img.example.com/card.png"
    call = star.html_calls[0]
    assert call["data"] == {"name": "x"}
    assert call["return_url"] is True
    assert call["options"]["full_page"] is True
    assert star.text_calls == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("boom"),
        ValueError("bad template"),
        TypeError("bad signature"),
        TimeoutError("slow"),
        OSError("network down"),
        Exception("bare"),
    ],
)
@pytest.mark.asyncio
async def test_every_exception_becomes_none(error: Exception) -> None:
    """任何渲染期异常都被吞掉并返回 None，绝不上抛。"""
    renderer = DefaultRenderer(FakeStar(html_error=error, text_error=error))
    assert (
        await renderer.render_html("<div/>", {"title": "t"}, RenderOptions(mode="network"))
        is None
    )
    assert await renderer.render_markdown("# t", RenderOptions(mode="local")) is None
    assert await renderer.render_html("<div/>", {}, RenderOptions(mode="auto")) is None


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed() -> None:
    """asyncio.CancelledError 属于 BaseException，必须继续向上传播。"""
    import asyncio

    renderer = DefaultRenderer(FakeStar(html_error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await renderer.render_html("<div/>", {}, RenderOptions(mode="network"))


@pytest.mark.asyncio
async def test_render_html_returns_none_for_empty_result() -> None:
    renderer = DefaultRenderer(FakeStar(html_result=""))
    assert (
        await renderer.render_html("<div/>", {}, RenderOptions(mode="network")) is None
    )
    renderer_none = DefaultRenderer(FakeStar(html_result=None))
    assert (
        await renderer_none.render_html("<div/>", {}, RenderOptions(mode="network")) is None
    )


@pytest.mark.asyncio
async def test_render_html_returns_none_for_empty_template() -> None:
    star = FakeStar()
    renderer = DefaultRenderer(star)
    assert await renderer.render_html("", {"a": 1}, RenderOptions(mode="network")) is None
    assert star.html_calls == []


@pytest.mark.asyncio
async def test_render_html_local_mode_uses_text_to_image() -> None:
    star = FakeStar(text_result="local.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div>{{ title }}</div>",
        {"title": "标题", "lines": [{"text": "第一行"}, {"text": "第二行"}]},
        RenderOptions(mode="local"),
    )
    assert result == "local.png"
    assert star.html_calls == []
    assert len(star.text_calls) == 1
    text = star.text_calls[0]["text"]
    assert "标题" in text and "第一行" in text and "第二行" in text


@pytest.mark.asyncio
async def test_render_html_local_mode_prefers_real_local_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode=local 且本地策略可用时，必须走本地 Pillow，而不是 star.text_to_image。

    这是性能修复的核心：star.text_to_image 在 AstrBot 里其实会调到**网络**策略
    （实测 3~6s），而本地 Pillow 约 0.2s。
    """
    calls: list[str] = []

    class FakeLocalStrategy:
        async def render(self, text: str, return_url: bool = True) -> str:
            calls.append(text)
            return "/tmp/local.png"

    monkeypatch.setattr(
        renderer_module, "local_render_strategy", lambda: FakeLocalStrategy()
    )
    star = FakeStar(text_result="network.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div/>", {"fallback_text": "本地文本"}, RenderOptions(mode="local")
    )
    assert result == "/tmp/local.png"
    assert calls == ["本地文本"]
    assert star.text_calls == [], "本地策略可用时不应回落到 star.text_to_image"


@pytest.mark.asyncio
async def test_render_html_local_mode_falls_back_when_local_strategy_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地策略抛异常时必须安全回退到 star 路径，绝不抛出。"""

    class BoomStrategy:
        async def render(self, text: str, return_url: bool = True) -> str:
            raise RuntimeError("pillow boom")

    monkeypatch.setattr(
        renderer_module, "local_render_strategy", lambda: BoomStrategy()
    )
    star = FakeStar(text_result="fallback.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div/>", {"fallback_text": "文本"}, RenderOptions(mode="local")
    )
    assert result == "fallback.png"
    assert len(star.text_calls) == 1


@pytest.mark.asyncio
async def test_render_html_local_mode_prefers_fallback_text() -> None:
    star = FakeStar(text_result="local.png")
    renderer = DefaultRenderer(star)
    await renderer.render_html(
        "<div/>", {"fallback_text": "纯文本兜底"}, RenderOptions(mode="local")
    )
    assert star.text_calls[0]["text"] == "纯文本兜底"


@pytest.mark.asyncio
async def test_render_html_auto_mode_falls_back_to_local() -> None:
    star = FakeStar(html_error=RuntimeError("network down"), text_result="local.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div>{{ title }}</div>", {"title": "标题"}, RenderOptions(mode="auto")
    )
    assert result == "local.png"
    assert star.html_calls and star.text_calls


@pytest.mark.asyncio
async def test_render_html_auto_mode_returns_none_without_fallback_text() -> None:
    star = FakeStar(html_error=RuntimeError("network down"), text_result="unused.png")
    renderer = DefaultRenderer(star)
    assert await renderer.render_html("<div/>", {}, RenderOptions(mode="auto")) is None
    assert star.text_calls == []


@pytest.mark.asyncio
async def test_render_html_network_mode_never_falls_back_locally() -> None:
    star = FakeStar(html_error=RuntimeError("network down"), text_result="local.png")
    renderer = DefaultRenderer(star)
    assert (
        await renderer.render_html("<div/>", {"title": "t"}, RenderOptions(mode="network"))
        is None
    )
    assert star.text_calls == []


@pytest.mark.asyncio
async def test_render_markdown_returns_none_when_star_raises() -> None:
    star = FakeStar(text_error=RuntimeError("boom"))
    renderer = DefaultRenderer(star)
    assert await renderer.render_markdown("# 标题", RenderOptions(mode="local")) is None


@pytest.mark.asyncio
async def test_render_markdown_returns_string_on_success() -> None:
    star = FakeStar(text_result="https://img.example.com/md.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_markdown("# 标题", RenderOptions(mode="local"))
    assert result == "https://img.example.com/md.png"
    assert star.text_calls[0]["text"] == "# 标题"


@pytest.mark.asyncio
async def test_render_markdown_empty_text_returns_none() -> None:
    star = FakeStar()
    renderer = DefaultRenderer(star)
    assert await renderer.render_markdown("   ", RenderOptions(mode="local")) is None
    assert star.text_calls == []


@pytest.mark.asyncio
async def test_renderer_without_star_returns_none() -> None:
    renderer = DefaultRenderer(None)
    assert await renderer.render_html("<div/>", {"title": "t"}) is None
    assert await renderer.render_markdown("text") is None

    class BrokenStar:
        async def html_render(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("不应被调用")

    assert await DefaultRenderer(BrokenStar()).render_markdown("text") is None


@pytest.mark.asyncio
async def test_renderer_logs_instead_of_raising(caplog: pytest.LogCaptureFixture) -> None:
    star = FakeStar(html_error=RuntimeError("boom"))
    renderer = DefaultRenderer(star)
    with caplog.at_level(logging.ERROR):
        assert await renderer.render_html("<div/>", {}, RenderOptions(mode="network")) is None
    assert any("boom" in record.message for record in caplog.records)


# --------------------------------------------------- 自建 t2i 端点（本地 mock）


async def _start_t2i_server(handler: Any) -> Any:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    app = web.Application()
    app.router.add_post("/text2img/generate", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_endpoint_mode_returns_url() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: Any) -> Any:
        from aiohttp import web

        seen["payload"] = await request.json()
        return web.json_response({"data": {"id": "img-1.png"}})

    server = await _start_t2i_server(handler)
    try:
        base = str(server.make_url("")).rstrip("/")
        renderer = DefaultRenderer(None)
        result = await renderer.render_html(
            "<div>{{ name }}</div>",
            {"name": "x"},
            RenderOptions(mode="network", endpoint=base, return_url=True),
        )
    finally:
        await server.close()

    assert result == f"{base}/text2img/img-1.png"
    payload = seen["payload"]
    assert payload["tmpl"] == "<div>{{ name }}</div>"
    assert payload["tmpldata"] == {"name": "x"}
    assert payload["json"] is True
    assert payload["options"]["full_page"] is True


@pytest.mark.asyncio
async def test_endpoint_mode_writes_local_file_when_return_url_false() -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n-fake-image"

    async def handler(request: Any) -> Any:
        from aiohttp import web

        return web.Response(body=png_bytes, content_type="image/jpeg")

    server = await _start_t2i_server(handler)
    try:
        base = str(server.make_url("")).rstrip("/")
        renderer = DefaultRenderer(None)
        result = await renderer.render_html(
            "<div/>",
            {"title": "t"},
            RenderOptions(mode="network", endpoint=base, return_url=False),
        )
    finally:
        await server.close()

    assert result is not None
    path = Path(result)
    assert path.exists()
    assert path.read_bytes() == png_bytes
    assert tempfile.gettempdir() in str(path)
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_endpoint_failure_falls_back_to_local_in_auto_mode() -> None:
    star = FakeStar(html_error=RuntimeError("no star network"), text_result="local.png")
    renderer = DefaultRenderer(star)
    result = await renderer.render_html(
        "<div/>",
        {"title": "t"},
        RenderOptions(mode="auto", endpoint="http://127.0.0.1:1", timeout=0.5),
    )
    assert result == "local.png"
    assert star.text_calls


@pytest.mark.asyncio
async def test_local_mode_ignores_configured_endpoint() -> None:
    hits = {"count": 0}

    async def handler(request: Any) -> Any:
        from aiohttp import web

        hits["count"] += 1
        return web.json_response({"data": {"id": "x.png"}})

    server = await _start_t2i_server(handler)
    try:
        base = str(server.make_url("")).rstrip("/")
        star = FakeStar(text_result="local.png")
        renderer = DefaultRenderer(star)
        result = await renderer.render_html(
            "<div/>",
            {"title": "t"},
            RenderOptions(mode="local", endpoint=base),
        )
    finally:
        await server.close()

    assert result == "local.png"
    assert hits["count"] == 0
