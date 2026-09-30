"""core/t2i 渲染层：模板自包含性、HTML 转义、三级降级链（全程无网络）。

用 FakeRenderer 记录 render_html / render_markdown 的调用参数，断言：

- 两个内置模板都能被 autoescape=True 的 Jinja2 环境渲染成功；
- 降级链 HTML -> Markdown -> None 的每一步；
- 用户可控文本（歌名 / 昵称 / 评论正文）不会以未转义形式出现在 HTML 里；
- t2i 包不 import astrbot，也不自带 HTTP 客户端（HTTP 只可能发生在 Renderer 实现里）。
"""

from __future__ import annotations

import ast
import asyncio
from unittest.mock import AsyncMock
from pathlib import Path
from typing import Any

import pytest

import core.renderer as renderer_module
from core.config import RuntimeConfig
from core.models import CommentItem, CommentPage, Lyric, LyricLine, SongInfo
from core.renderer import DefaultRenderer, RenderOptions, Renderer
from core.t2i import (
    COMMENTS_TEMPLATE,
    LYRICS_TEMPLATE,
    build_comments_data,
    build_lyrics_data,
    call_renderer,
    get_environment,
    render_comments,
    render_lyrics,
    render_template,
    select_lyrics_template,
)
from core.t2i.templates import is_template_renderable

TITLE = "晴天"
ARTIST = "周杰伦"
ALBUM = "叶惠美"
COVER = "https://p1.music.126.net/cover.jpg"


@pytest.fixture(autouse=True)
def _no_local_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    """把本地 Pillow 策略置为不可用，让渲染断言走确定性的 star 委派路径。

    有真实 AstrBot 时本地策略会先成功返回，使
    「local -> star.text_to_image」这类断言不确定；真正的本地路径由
    tests/test_renderer_default.py 专门覆盖。
    """
    monkeypatch.setattr(renderer_module, "local_render_strategy", lambda: None)

HTML_RESULT = "https://img.example.com/lyrics.png"
MD_RESULT = "https://img.example.com/lyrics-md.png"


# --------------------------------------------------------------------- 素材


def make_song(**overrides: Any) -> SongInfo:
    values: dict[str, Any] = {
        "id": "186016",
        "name": TITLE,
        "artists": [ARTIST],
        "album": ALBUM,
        "duration_ms": 225_000,
        "cover_url": COVER,
        "source": "netease",
        "provider_key": "netease",
        "url": "https://music.163.com/#/song?id=186016",
    }
    values.update(overrides)
    return SongInfo(**values)


def make_lyric(count: int = 3, *, translation: bool = False) -> Lyric:
    return Lyric(
        lines=[
            LyricLine(
                text=f"第{index}行歌词",
                translation=f"line {index}" if translation else "",
            )
            for index in range(1, count + 1)
        ]
    )


def make_page(count: int = 2, *, replies: int = 0) -> CommentPage:
    return CommentPage(
        items=[
            CommentItem(
                user=f"用户{index}",
                content=f"评论正文{index}",
                liked=12_345 * index,
                avatar_url=f"https://p1.music.126.net/avatar{index}.jpg",
                time=f"2024-01-0{index}",
                replies=[
                    CommentItem(user=f"回复者{index}", content=f"回复内容{index}")
                    for _ in range(replies)
                ],
            )
            for index in range(1, count + 1)
        ],
        total=count,
    )


class FakeRenderer:
    """记录调用参数的假渲染器（满足 Renderer 协议，默认不发任何请求）。"""

    def __init__(
        self,
        *,
        html_result: Any = HTML_RESULT,
        md_result: Any = MD_RESULT,
        html_error: Exception | None = None,
        md_error: Exception | None = None,
    ) -> None:
        self.html_result = html_result
        self.md_result = md_result
        self.html_error = html_error
        self.md_error = md_error
        self.html_calls: list[dict[str, Any]] = []
        self.md_calls: list[dict[str, Any]] = []

    async def render_html(
        self,
        template: str,
        data: dict[str, Any] | None = None,
        options: RenderOptions | None = None,
    ) -> str | None:
        self.html_calls.append(
            {"template": template, "data": data, "options": options}
        )
        if self.html_error is not None:
            raise self.html_error
        return self.html_result

    async def render_markdown(
        self,
        md: str,
        options: RenderOptions | None = None,
    ) -> str | None:
        self.md_calls.append({"md": md, "options": options})
        if self.md_error is not None:
            raise self.md_error
        return self.md_result


class FakeStar:
    """假 Star：只实现本地 text_to_image，html_render 被调用即视为测试失败。"""

    def __init__(self, *, text_result: Any = "local.png") -> None:
        self.text_result = text_result
        self.text_calls: list[dict[str, Any]] = []

    async def text_to_image(self, text: str, return_url: bool = True) -> Any:
        self.text_calls.append({"text": text, "return_url": return_url})
        return self.text_result

    async def html_render(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("本地渲染模式不应调用 html_render")


# --------------------------------------------------------------------- 模板


def test_fake_renderer_satisfies_renderer_protocol() -> None:
    assert isinstance(FakeRenderer(), Renderer)


def test_environment_escapes_by_default() -> None:
    assert get_environment().autoescape is True


@pytest.mark.parametrize(
    ("name", "template"),
    [("lyrics", LYRICS_TEMPLATE), ("comments", COMMENTS_TEMPLATE)],
)
def test_templates_are_self_contained(name: str, template: str) -> None:
    assert "<!DOCTYPE html>" in template
    assert "<style>" in template
    for forbidden in ("<link", "<script", "@import", "http://", "https://", "url("):
        assert forbidden not in template, f"{name} 模板不应包含 {forbidden}"


@pytest.mark.parametrize(
    ("name", "template", "data_factory"),
    [
        (
            "lyrics",
            LYRICS_TEMPLATE,
            lambda: build_lyrics_data(make_song(), make_lyric(2, translation=True), {}),
        ),
        (
            "comments",
            COMMENTS_TEMPLATE,
            lambda: build_comments_data(make_song(), make_page(2), {}),
        ),
    ],
)
def test_builtin_templates_render_complete_data(
    name: str, template: str, data_factory: Any
) -> None:
    html = render_template(template, data_factory(), strict=True)
    assert isinstance(html, str) and html.strip()
    assert TITLE in html
    assert ARTIST in html


def test_lyrics_template_renders_meta_and_lines() -> None:
    data = build_lyrics_data(make_song(), make_lyric(2, translation=True), {})
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert f"专辑：{ALBUM}" in html
    assert "时长：03:45" in html
    assert "网易云音乐" in html
    assert COVER in html
    assert "第1行歌词" in html
    assert "line 1" in html
    assert '<span class="translation translation-strong">' in html
    assert "共 2 行歌词" in html


def test_lyrics_template_uses_placeholder_without_cover() -> None:
    data = build_lyrics_data(make_song(cover_url=""), make_lyric(1), {})
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert "cover-placeholder" in html
    assert "cover.jpg" not in html


def test_lyrics_template_without_lyrics_shows_placeholder() -> None:
    data = build_lyrics_data(make_song(), Lyric(), {})
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert "（暂无歌词）" in html


def test_lyrics_template_theme_font_and_spacing() -> None:
    cfg = {
        "lyrics_theme": "dark",
        "lyrics_font_size": 30,
        "lyrics_line_spacing": 2.0,
        "lyrics_width": 640,
    }
    data = build_lyrics_data(make_song(), make_lyric(2), cfg)
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert 'class="theme-dark"' in html
    assert "font-size: 30px" in html
    assert "line-height: 2.0" in html
    assert "width: 640px" in html


def test_lyrics_template_can_hide_meta_and_translation_highlight() -> None:
    cfg = {"lyrics_show_meta": False, "lyrics_highlight_translation": False}
    data = build_lyrics_data(make_song(), make_lyric(2, translation=True), cfg)
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert '<h1 class="title">' not in html
    assert '<span class="translation translation-strong">' not in html
    assert 'class="translation"' in html
    assert "line 1" in html


@pytest.mark.parametrize(
    ("max_lines", "visible", "truncated"),
    [
        (0, 4, False),
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
        (4, 4, False),
        (99, 4, False),
    ],
)
def test_lyrics_template_max_lines(max_lines: int, visible: int, truncated: bool) -> None:
    data = build_lyrics_data(make_song(), make_lyric(4), {"lyrics_max_lines": max_lines})
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    for index in range(1, visible + 1):
        assert f"第{index}行歌词" in html
    for index in range(visible + 1, 5):
        assert f"第{index}行歌词" not in html
    if truncated:
        assert f"仅显示前 {visible} 行，已省略 {4 - visible} 行" in html
    else:
        assert "已省略" not in html


def test_comments_template_renders_total_and_items() -> None:
    page = CommentPage(items=make_page(2).items, total=100_000, has_more=True)
    data = build_comments_data(make_song(), page, {})
    html = render_template(COMMENTS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert "评论 10万 条" in html
    assert "显示 2 条" in html
    assert "用户1" in html
    assert "评论正文2" in html
    assert "赞 1.2万" in html
    assert "赞 2.5万" in html
    assert "仅显示前 2 条评论" in html
    assert "avatar1.jpg" in html


def test_comments_template_placeholder_avatar_and_empty_page() -> None:
    page = CommentPage(items=[CommentItem(user="小明", content="好听", liked=3)])
    html = render_template(
        COMMENTS_TEMPLATE, build_comments_data(make_song(), page, {}), strict=True
    )
    assert isinstance(html, str)
    assert "avatar-placeholder" in html
    assert ">小<" in html

    empty = render_template(
        COMMENTS_TEMPLATE, build_comments_data(make_song(), CommentPage(), {}), strict=True
    )
    assert isinstance(empty, str)
    assert "（暂无评论）" in empty


def test_comments_template_respects_display_flags() -> None:
    cfg = {
        "comments_show_avatar": False,
        "comments_show_likes": False,
        "comments_show_reply": False,
    }
    data = build_comments_data(make_song(), make_page(1, replies=2), cfg)
    html = render_template(COMMENTS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert 'class="avatar' not in html
    assert "赞 " not in html
    assert "回复内容1" not in html

    shown = build_comments_data(
        make_song(),
        make_page(1, replies=2),
        {"comments_show_reply": True, "comments_reply_count": 1},
    )
    html_replies = render_template(COMMENTS_TEMPLATE, shown, strict=True)
    assert isinstance(html_replies, str)
    assert "回复内容1" in html_replies
    assert "回复内容2" not in html_replies


# --------------------------------------------------------------------- 转义


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '"><script>alert(1)</script>',
        "<iframe src='https://evil.example.com'></iframe>",
    ],
)
def test_lyrics_template_escapes_user_text(payload: str) -> None:
    data = build_lyrics_data(make_song(name=payload, album=payload), make_lyric(1), {})
    html = render_template(LYRICS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert payload not in html
    assert "<script>" not in html
    assert "<iframe" not in html
    assert "&lt;" in html


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
    ],
)
def test_comments_template_escapes_user_text(payload: str) -> None:
    page = CommentPage(
        items=[
            CommentItem(
                user=payload,
                content=payload,
                liked=1,
                replies=[CommentItem(user=payload, content=payload)],
            )
        ]
    )
    data = build_comments_data(
        make_song(), page, {"comments_show_reply": True, "comments_reply_count": 1}
    )
    html = render_template(COMMENTS_TEMPLATE, data, strict=True)
    assert isinstance(html, str)
    assert payload not in html
    assert "<script>" not in html
    assert 'onerror="alert' not in html
    assert "&lt;" in html


def test_templates_escape_exactly_once() -> None:
    """数据保持原样，转义只由模板环境发生一次（不会双重转义）。"""
    song = make_song(name="A&B <C>", album="D&E")
    html = render_template(LYRICS_TEMPLATE, build_lyrics_data(song, make_lyric(1), {}), strict=True)
    assert isinstance(html, str)
    assert "A&amp;B &lt;C&gt;" in html
    assert "&amp;amp;" not in html
    assert "<C>" not in html


def test_rendered_html_has_balanced_block_tags() -> None:
    """渲染结果里成对标签必须闭合（headless 浏览器渲染前的低成本自检）。"""
    lyrics = render_template(
        LYRICS_TEMPLATE, build_lyrics_data(make_song(), make_lyric(3), {}), strict=True
    )
    comments = render_template(
        COMMENTS_TEMPLATE, build_comments_data(make_song(), make_page(2), {}), strict=True
    )
    for html in (lyrics, comments):
        assert isinstance(html, str)
        assert html.count("<div") == html.count("</div>")
        assert html.count("<span") == html.count("</span>")
        assert html.rstrip().endswith("</html>")


def test_render_template_reports_errors() -> None:
    assert render_template("{{ name }}", {"name": "x"}) is not None
    assert render_template("", {"name": "x"}) is None
    assert render_template("{{ name ", {"name": "x"}) is None
    assert render_template(None, {}) is None  # type: ignore[arg-type]
    assert is_template_renderable("{{ name }}", {"name": "x"}) is True
    assert is_template_renderable("{{ name ", {}) is False
    with pytest.raises(Exception):
        render_template("{{ name ", {}, strict=True)


def test_select_lyrics_template() -> None:
    data = build_lyrics_data(make_song(), make_lyric(1), {})
    assert select_lyrics_template(RuntimeConfig.from_mapping({}), data) == LYRICS_TEMPLATE
    custom = "<div>{{ title }}</div>"
    cfg = RuntimeConfig.from_mapping({"lyrics_template": custom})
    assert select_lyrics_template(cfg, data) == custom
    broken = RuntimeConfig.from_mapping({"lyrics_template": "{{ title "})
    assert select_lyrics_template(broken, data) == LYRICS_TEMPLATE


# --------------------------------------------------------------------- 降级链


async def test_render_lyrics_returns_html_result() -> None:
    renderer = FakeRenderer()
    cfg = RuntimeConfig.from_mapping({})
    result = await render_lyrics(renderer, make_song(), make_lyric(3), cfg)
    assert result == HTML_RESULT
    assert len(renderer.html_calls) == 1
    assert renderer.md_calls == []
    call = renderer.html_calls[0]
    assert call["template"] == LYRICS_TEMPLATE
    assert call["data"]["title"] == TITLE
    assert call["data"]["max_lines"] == 200
    assert "第1行歌词" in call["data"]["fallback_text"]
    options = call["options"]
    assert isinstance(options, RenderOptions)
    assert options.width == 900
    assert options.mode == "auto"


async def test_render_lyrics_uses_custom_template_and_options() -> None:
    renderer = FakeRenderer()
    cfg = RuntimeConfig.from_mapping(
        {
            "lyrics_template": "<div>{{ title }}</div>",
            "lyrics_width": 640,
            "lyrics_render_mode": "network",
            "lyrics_t2i_endpoint": "http://127.0.0.1:6100",
            "lyrics_max_lines": 50,
        }
    )
    result = await render_lyrics(renderer, make_song(), make_lyric(3), cfg)
    assert result == HTML_RESULT
    call = renderer.html_calls[0]
    assert call["template"] == "<div>{{ title }}</div>"
    assert call["options"].width == 640
    assert call["options"].mode == "network"
    assert call["options"].endpoint == "http://127.0.0.1:6100"
    assert call["options"].max_lines == 50


async def test_render_lyrics_falls_back_to_markdown() -> None:
    renderer = FakeRenderer(html_result=None)
    result = await render_lyrics(renderer, make_song(), make_lyric(2), {})
    assert result == MD_RESULT
    assert len(renderer.html_calls) == 1
    assert len(renderer.md_calls) == 1
    md = renderer.md_calls[0]["md"]
    assert TITLE in md
    assert "第1行歌词" in md
    assert isinstance(renderer.md_calls[0]["options"], RenderOptions)


@pytest.mark.parametrize("html_result", [None, "", "   "])
async def test_render_lyrics_treats_blank_html_result_as_failure(html_result: Any) -> None:
    renderer = FakeRenderer(html_result=html_result)
    assert await render_lyrics(renderer, make_song(), make_lyric(1), {}) == MD_RESULT
    assert len(renderer.md_calls) == 1


async def test_render_lyrics_returns_none_when_both_fail() -> None:
    renderer = FakeRenderer(html_result=None, md_result=None)
    assert await render_lyrics(renderer, make_song(), make_lyric(2), {}) is None
    assert len(renderer.html_calls) == 1
    assert len(renderer.md_calls) == 1


async def test_render_lyrics_falls_back_when_renderer_raises() -> None:
    renderer = FakeRenderer(html_error=RuntimeError("network down"))
    assert await render_lyrics(renderer, make_song(), make_lyric(1), {}) == MD_RESULT
    assert len(renderer.md_calls) == 1


async def test_render_lyrics_returns_none_when_markdown_raises_too() -> None:
    renderer = FakeRenderer(
        html_error=RuntimeError("html"), md_error=RuntimeError("markdown")
    )
    assert await render_lyrics(renderer, make_song(), make_lyric(1), {}) is None


async def test_render_lyrics_propagates_cancelled_error() -> None:
    renderer = FakeRenderer(html_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await render_lyrics(renderer, make_song(), make_lyric(1), {})


async def test_render_lyrics_without_renderer_returns_none() -> None:
    assert await render_lyrics(None, make_song(), make_lyric(1), {}) is None


async def test_render_lyrics_with_empty_lyric_skips_rendering() -> None:
    renderer = FakeRenderer()
    assert await render_lyrics(renderer, make_song(), Lyric(), {}) is None
    assert renderer.html_calls == []
    assert renderer.md_calls == []


async def test_render_lyrics_tolerates_broken_renderer() -> None:
    class BrokenRenderer:
        async def render_html(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("bad")

        render_markdown = "not-callable"

    assert await render_lyrics(BrokenRenderer(), make_song(), make_lyric(1), {}) is None


async def test_render_lyrics_local_mode_uses_literal_fallback_text(monkeypatch: pytest.MonkeyPatch) -> None:
    local = AsyncMock(return_value="local-lyrics.png")
    monkeypatch.setattr(DefaultRenderer, "_render_via_local_text", local)
    star = FakeStar(text_result="local-lyrics.png")
    renderer = DefaultRenderer(star)
    cfg = RuntimeConfig.from_mapping({"lyrics_render_mode": "local"})
    result = await render_lyrics(renderer, make_song(), make_lyric(2), cfg)
    assert result == "local-lyrics.png"
    assert star.text_calls == []
    text = local.call_args.args[0]
    assert TITLE in text
    assert "第1行歌词" in text


async def test_render_lyrics_accepts_mapping_config() -> None:
    renderer = FakeRenderer()
    await render_lyrics(renderer, make_song(), make_lyric(1), {"lyrics_width": 320})
    assert renderer.html_calls[0]["options"].width == 320


# --------------------------------------------------------------------- 评论链


async def test_render_comments_returns_html_result() -> None:
    renderer = FakeRenderer(html_result="https://img.example.com/comments.png")
    result = await render_comments(renderer, make_song(), make_page(2), {})
    assert result == "https://img.example.com/comments.png"
    assert len(renderer.html_calls) == 1
    assert renderer.md_calls == []
    call = renderer.html_calls[0]
    assert call["template"] == COMMENTS_TEMPLATE
    assert call["data"]["shown"] == 2
    assert call["data"]["total_text"] == "2"
    assert "评论正文1" in call["data"]["fallback_text"]
    assert isinstance(call["options"], RenderOptions)


async def test_render_comments_falls_back_to_markdown() -> None:
    renderer = FakeRenderer(html_result=None)
    result = await render_comments(renderer, make_song(), make_page(2), {})
    assert result == MD_RESULT
    assert len(renderer.md_calls) == 1
    md = renderer.md_calls[0]["md"]
    assert TITLE in md
    assert "评论正文1" in md


async def test_render_comments_returns_none_when_both_fail() -> None:
    renderer = FakeRenderer(html_result=None, md_result=None)
    assert await render_comments(renderer, make_song(), make_page(1), {}) is None


async def test_render_comments_falls_back_when_renderer_raises() -> None:
    renderer = FakeRenderer(html_error=RuntimeError("boom"))
    assert await render_comments(renderer, make_song(), make_page(1), {}) == MD_RESULT


async def test_render_comments_returns_none_for_empty_page() -> None:
    renderer = FakeRenderer()
    assert await render_comments(renderer, make_song(), CommentPage(), {}) is None
    assert await render_comments(renderer, make_song(), make_page(2), {"comments_count": 0}) is None
    assert renderer.html_calls == []
    assert await render_comments(None, make_song(), make_page(1), {}) is None


async def test_render_comments_local_mode_uses_literal_fallback_text(monkeypatch: pytest.MonkeyPatch) -> None:
    local = AsyncMock(return_value="local-comments.png")
    monkeypatch.setattr(DefaultRenderer, "_render_via_local_text", local)
    star = FakeStar(text_result="local-comments.png")
    renderer = DefaultRenderer(star)
    result = await render_comments(
        renderer, make_song(), make_page(1), {"lyrics_render_mode": "local"}
    )
    assert result == "local-comments.png"
    assert "评论正文1" in local.call_args.args[0]
    assert star.text_calls == []


# --------------------------------------------------------------------- 其他


async def test_call_renderer_semantics() -> None:
    async def ok(*args: Any, **kwargs: Any) -> str:
        return "  result.png  "

    def sync_ok(value: str) -> str:
        return value

    async def blank() -> str:
        return "   "

    async def boom() -> str:
        raise RuntimeError("boom")

    assert await call_renderer(ok) == "result.png"
    assert await call_renderer(sync_ok, "x") == "x"
    assert await call_renderer(blank) is None
    assert await call_renderer(boom) is None
    assert await call_renderer(None) is None
    assert await call_renderer("not-callable") is None

    async def cancelled() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await call_renderer(cancelled)


def test_package_does_not_import_astrbot_or_http_clients() -> None:
    package = Path(__file__).resolve().parents[1] / "core" / "t2i"
    modules = sorted(package.glob("*.py"))
    assert modules, "core/t2i 包应当存在"
    forbidden = {"astrbot", "aiohttp", "requests", "urllib", "httpx"}
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            assert name.split(".")[0] not in forbidden, f"{path.name} 不应 import {name}"


def test_package_exports_public_api() -> None:
    import core.t2i as t2i

    for name in (
        "render_lyrics",
        "render_comments",
        "build_lyrics_text",
        "build_comments_text",
        "build_lyrics_data",
        "build_comments_data",
    ):
        assert callable(getattr(t2i, name)), f"core.t2i 缺少 {name}"
