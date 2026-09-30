"""Regression checks against real AstrBot and Netease rendering contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageChops, ImageDraw

from core.lyrics_flow import run_lyrics_flow
from core.models import SongInfo
from core.netease.provider import NeteaseProvider
from core.renderer import DefaultRenderer, RenderOptions

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_external_network(astrbot_ref, monkeypatch):
    from astrbot.core import html_renderer
    from astrbot.core.utils.t2i.local_strategy import ImageBlock

    error = AssertionError("Regression tests must never access external networks")
    monkeypatch.setattr(html_renderer.network_strategy, "render", AsyncMock(side_effect=error))
    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", AsyncMock(side_effect=error))
    monkeypatch.setattr(ImageBlock, "load", AsyncMock(side_effect=error))


@pytest.fixture
def real_star(astrbot_ref):
    from astrbot.core.star.base import Star

    class RealStar(Star):
        def _get_context_config(self):
            return {}

    return object.__new__(RealStar)


async def test_production_lyric_is_drawn_literally_without_loading_urls(real_star, monkeypatch):
    from astrbot.core import html_renderer
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    from astrbot.core.utils.t2i.local_strategy import ImageBlock

    marker = "![probe](http://127.0.0.1:6185/api/example)"
    provider = NeteaseProvider({"lyrics_render_mode": "local"})
    monkeypatch.setattr(provider, "_fetch", AsyncMock(return_value={
        "code": 200, "lrc": {"lyric": f"[00:01.00]{marker}"},
    }))
    image_load = AsyncMock(side_effect=AssertionError("External content must not load images"))
    network = AsyncMock(side_effect=AssertionError("Local mode must not use network T2I"))
    monkeypatch.setattr(ImageBlock, "load", image_load)
    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", network)
    drawn: list[str] = []
    original_draw = ImageDraw.ImageDraw.text

    def record_draw(self, xy, text, **kwargs):
        drawn.append(text)
        return original_draw(self, xy, text, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_draw)
    result = await run_lyrics_flow(
        {"lyrics_render_mode": "local", "lyrics_width": 1600},
        SongInfo(id="1", name="sample"), provider, object(), DefaultRenderer(real_star),
    )
    try:
        assert result.has_image
        assert marker in drawn
        assert Path(result.image).parent == Path(get_astrbot_temp_path())
        image_load.assert_not_called()
        network.assert_not_called()
    finally:
        Path(result.image).unlink(missing_ok=True)


async def test_legacy_markdown_renderer_receives_safe_text_and_chat_keeps_original(real_star, monkeypatch):
    from astrbot.core import html_renderer
    from astrbot.core.utils.t2i.local_strategy import ImageBlock

    marker = "![probe](http://127.0.0.1:6185/api/example)"
    provider = NeteaseProvider({"lyrics_render_mode": "local"})
    monkeypatch.setattr(provider, "_fetch", AsyncMock(return_value={
        "code": 200, "lrc": {"lyric": f"[00:01.00]{marker}"},
    }))
    image_load = AsyncMock(side_effect=AssertionError("Markdown image injection"))
    monkeypatch.setattr(ImageBlock, "load", image_load)

    class LegacyRenderer:
        async def render_html(self, template, data, options):
            return None

        async def render_markdown(self, md, options):
            return await html_renderer.local_strategy.render(md)

    result = await run_lyrics_flow(
        {"lyrics_render_mode": "local"}, SongInfo(id="1", name="sample"),
        provider, object(), LegacyRenderer(),
    )
    try:
        assert result.has_image
        image_load.assert_not_called()
    finally:
        Path(result.image).unlink(missing_ok=True)

    class FailedRenderer(LegacyRenderer):
        async def render_markdown(self, md, options):
            return None

    fallback = await run_lyrics_flow(
        {"lyrics_render_mode": "local"}, SongInfo(id="1", name="sample"),
        provider, object(), FailedRenderer(),
    )
    assert marker in fallback.text
    assert "&#" not in fallback.text


async def test_local_failure_never_calls_network(real_star, monkeypatch):
    from astrbot.core import html_renderer

    network_html = AsyncMock()
    network_md = AsyncMock()
    star_text = AsyncMock()
    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", network_html)
    monkeypatch.setattr(html_renderer.network_strategy, "render", network_md)
    monkeypatch.setattr(real_star, "text_to_image", star_text)
    monkeypatch.setattr(DefaultRenderer, "_render_via_local_text", AsyncMock(return_value=None))
    renderer = DefaultRenderer(real_star)
    assert await renderer.render_html("<p/>", {"fallback_text": "text"}, RenderOptions(mode="local")) is None
    network_html.assert_not_called()
    network_md.assert_not_called()
    star_text.assert_not_called()


async def test_network_markdown_failure_never_uses_local(real_star, monkeypatch):
    from astrbot.core import html_renderer

    network = AsyncMock(side_effect=RuntimeError("unavailable"))
    local = AsyncMock(return_value="/tmp/local.png")
    monkeypatch.setattr(html_renderer.network_strategy, "render", network)
    monkeypatch.setattr(html_renderer.local_strategy, "render", local)
    assert await DefaultRenderer(real_star).render_markdown("# text", RenderOptions(mode="network")) is None
    network.assert_awaited_once()
    local.assert_not_called()


async def test_auto_html_failure_goes_directly_to_local_text(real_star, monkeypatch):
    from astrbot.core import html_renderer

    network_html = AsyncMock(side_effect=RuntimeError("unavailable"))
    network_md = AsyncMock()
    local = AsyncMock(return_value="/tmp/local.png")
    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", network_html)
    monkeypatch.setattr(html_renderer.network_strategy, "render", network_md)
    monkeypatch.setattr(DefaultRenderer, "_render_via_local_text", local)
    assert await DefaultRenderer(real_star).render_html("<p/>", {"fallback_text": "text"}, RenderOptions(mode="auto")) == "/tmp/local.png"
    network_html.assert_awaited_once()
    network_md.assert_not_called()
    local.assert_awaited_once()


async def test_local_width_theme_font_size_and_line_spacing(real_star):
    settings = [
        RenderOptions(mode="local", width=400, font_size=20, line_spacing=1, theme="light"),
        RenderOptions(mode="local", width=1200, font_size=20, line_spacing=2, theme="dark"),
        RenderOptions(mode="local", width=1200, font_size=40, line_spacing=2, theme="dark"),
    ]
    dimensions = []
    glyph_heights = []
    for opts in settings:
        path = await DefaultRenderer(real_star).render_text("first line\nsecond line\nthird line", opts)
        try:
            with Image.open(path) as image:
                dimensions.append(image.size)
                assert image.width == opts.width
                corner = image.getpixel((0, 0))
                assert max(corner) < 40 if opts.theme == "dark" else min(corner) > 240
                first_line = image.crop((28, 28, image.width - 28, 28 + round(opts.font_size * opts.line_spacing)))
                background = Image.new("RGB", first_line.size, corner)
                mask = ImageChops.difference(first_line, background).convert("L").point(
                    lambda value: 255 if value > 30 else 0
                )
                bounds = mask.getbbox()
                glyph_heights.append(bounds[3] - bounds[1])
        finally:
            Path(path).unlink(missing_ok=True)
    assert dimensions[0][1] < dimensions[1][1] < dimensions[2][1]
    assert abs(glyph_heights[0] - glyph_heights[1]) <= 2
    assert max(glyph_heights[:2]) < glyph_heights[2]


async def test_network_plain_text_template_escapes_html(real_star, monkeypatch):
    from astrbot.core import html_renderer
    from jinja2 import Environment

    rendered = []

    async def render(template, data, return_url, options):
        rendered.append(Environment(autoescape=False).from_string(template).render(data))
        return "https://example.invalid/plain.png"

    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", render)
    result = await DefaultRenderer(real_star).render_text(
        '<img src="http://127.0.0.1/probe" /> {{ unsafe }}', RenderOptions(mode="network")
    )
    assert result == "https://example.invalid/plain.png"
    assert "<img" not in rendered[0]
    assert "&lt;img" in rendered[0]
    assert "{{ unsafe }}" in rendered[0]


async def test_network_markdown_timeout_and_cancellation_propagation(real_star, monkeypatch):
    from astrbot.core import html_renderer

    cancelled = asyncio.Event()

    async def block(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(html_renderer.network_strategy, "render", block)
    renderer = DefaultRenderer(real_star)
    assert await renderer.render_markdown("# text", RenderOptions(mode="network", timeout=0.5)) is None
    assert cancelled.is_set()
    task = asyncio.create_task(renderer.render_markdown("# text", RenderOptions(mode="network")))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_custom_html_contract_is_preserved(real_star, monkeypatch):
    from astrbot.core import html_renderer

    render = AsyncMock(return_value="https://example.invalid/custom.png")
    monkeypatch.setattr(html_renderer.network_strategy, "render_custom_template", render)
    template = "<main>{{ title }}</main>"
    payload = {"title": "custom"}
    assert await DefaultRenderer(real_star).render_html(template, payload, RenderOptions(mode="network")) == "https://example.invalid/custom.png"
    assert render.call_args.args[:2] == (template, payload)
