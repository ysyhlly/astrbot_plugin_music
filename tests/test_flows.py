"""core/lyrics_flow.py + core/comments_flow.py：编排、开关、降级与 viewport 注入。

全部使用 FakeProvider / FakeRenderer / FakeTransport（鸭子类型），无任何网络；
并断言两个编排模块既不 import aiohttp 也不自己发 HTTP 请求（HTTP 只在 provider /
renderer 里发生）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.comments_flow import (
    EMPTY_COMMENT_MESSAGE,
    FETCH_FAILED_MESSAGE,
    comments_offset,
    run_comments_flow,
    supports_sort_argument,
)
from core.config import RuntimeConfig
from core.lyrics_flow import (
    VIEWPORT_HEIGHT,
    ViewportRenderer,
    resolve_renderer,
    run_lyrics_flow,
    with_viewport,
)
from core.models import CommentItem, CommentPage, Lyric, LyricLine, SongInfo
from core.renderer import RenderOptions
from core.t2i import build_comments_text, build_lyrics_text
import core.lyrics_flow as lyrics_flow_module

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HTML_RESULT = "https://img.example.com/card.png"
MD_RESULT = "https://img.example.com/card-md.png"

LYRIC = Lyric(
    text="故事的小黄花\n从出生那年就飘着",
    lines=[LyricLine(text="故事的小黄花"), LyricLine(text="从出生那年就飘着")],
)
PAGE = CommentPage(
    items=[
        CommentItem(user="小明", content="青春啊", liked=1234, time="2020-01-01"),
        CommentItem(user="小红", content="循环一整年", liked=99, time="2020-02-02"),
    ],
    total=2,
    has_more=False,
)


def make_song(**overrides) -> SongInfo:
    values = {
        "id": "186016",
        "name": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "duration_ms": 225_000,
        "cover_url": "https://p1.music.126.net/cover.jpg",
    }
    values.update(overrides)
    return SongInfo(**values)


def make_cfg(raw=None, **overrides) -> RuntimeConfig:
    data: dict = dict(raw) if isinstance(raw, dict) else {}
    data.update(overrides)
    return RuntimeConfig.from_mapping(data)


class FakeTransport:
    """鸭子类型 transport：任何真实 HTTP 调用都会让用例失败。"""

    def get(self, *args, **kwargs):  # pragma: no cover - 编排层不该调用
        raise AssertionError("编排层不允许直接发 HTTP 请求")

    def post(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("编排层不允许直接发 HTTP 请求")

    def request(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("编排层不允许直接发 HTTP 请求")


class FakeProvider:
    """支持 sort 关键字的 provider（＝ core/netease/provider.py 的签名）。"""

    key = "fake"
    display_name = "假网易云"

    def __init__(
        self,
        *,
        lyric=None,
        page=None,
        audio="",
        raise_lyrics=False,
        raise_comments=False,
        lyric_delay=0.0,
    ) -> None:
        self.lyric = lyric
        self.page = page
        self.audio = audio
        self.raise_lyrics = raise_lyrics
        self.raise_comments = raise_comments
        self.requests = {"lyrics": 0, "comments": 0, "audio": 0}
        self.comment_args: list[dict] = []

    async def lyrics(self, song, transport):
        self.requests["lyrics"] += 1
        if self.raise_lyrics:
            raise RuntimeError("boom")
        return self.lyric

    async def comments(self, song, transport, limit=20, offset=0, *, sort=None):
        self.requests["comments"] += 1
        self.comment_args.append({"limit": limit, "offset": offset, "sort": sort})
        if self.raise_comments:
            raise RuntimeError("boom")
        return self.page

    async def audio_url(self, song, transport, *, level="standard"):
        self.requests["audio"] += 1
        return self.audio

    def card_payload(self, song):
        return {}


class LegacyProvider(FakeProvider):
    """老签名：comments 不接受 sort（协议本身就没有这个参数）。"""

    async def comments(self, song, transport, limit=20, offset=0):
        self.requests["comments"] += 1
        self.comment_args.append({"limit": limit, "offset": offset, "sort": None})
        return self.page


class FakeRenderer:
    """记录每次渲染拿到的 RenderOptions。"""

    def __init__(
        self,
        *,
        html=HTML_RESULT,
        markdown=MD_RESULT,
        reject_viewport=False,
        fail_html=False,
        fail_all=False,
    ) -> None:
        self.html = html
        self.markdown = markdown
        self.reject_viewport = reject_viewport
        self.fail_html = fail_html
        self.fail_all = fail_all
        self.html_calls: list[RenderOptions] = []
        self.markdown_calls: list[RenderOptions] = []

    @staticmethod
    def _has_viewport(options) -> bool:
        try:
            return "viewport_width" in (options.extra or {})
        except AttributeError:  # pragma: no cover
            return False

    async def render_html(self, template, data=None, options=None):
        self.html_calls.append(options)
        if self.fail_all or self.fail_html:
            return None
        if self.reject_viewport and self._has_viewport(options):
            return None
        return self.html

    async def render_markdown(self, md, options=None):
        self.markdown_calls.append(options)
        if self.fail_all:
            return None
        if self.reject_viewport and self._has_viewport(options):
            return None
        return self.markdown


@pytest.fixture(autouse=True)
def _clear_rejected_endpoints(monkeypatch):
    """viewport 拒绝缓存是模块级状态：每个用例都从干净状态开始。"""
    monkeypatch.setattr(lyrics_flow_module, "_REJECTED_ENDPOINTS", set())
    yield


# ------------------------------------------------------------------ viewport


def test_with_viewport_injects_extra_without_mutating_source():
    base = RenderOptions(width=900, mode="auto", endpoint="https://t2i.example.com")
    injected = with_viewport(base, 900)
    assert base.extra == {}
    assert injected.extra["viewport_width"] == 900
    assert injected.extra["viewport_height"] == VIEWPORT_HEIGHT
    assert injected.screenshot_options()["viewport_width"] == 900
    assert injected.screenshot_options()["full_page"] is True
    assert injected.width == base.width and injected.endpoint == base.endpoint


def test_with_viewport_clamps_and_survives_garbage():
    assert with_viewport(RenderOptions(), 999999).extra["viewport_width"] == 4096
    assert with_viewport(RenderOptions(), "abc").extra["viewport_width"] == 800
    assert with_viewport(None, 800).extra["viewport_height"] == VIEWPORT_HEIGHT


async def test_lyrics_render_receives_viewport_width_from_config():
    cfg = make_cfg({"lyrics_width": 1234})
    renderer = FakeRenderer()
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer)
    assert result.has_image and result.image == HTML_RESULT
    assert renderer.html_calls, "渲染器应被调用"
    options = renderer.html_calls[0]
    assert options.extra["viewport_width"] == 1234
    assert options.extra["viewport_height"] == VIEWPORT_HEIGHT
    assert options.screenshot_options()["viewport_width"] == 1234


async def test_comments_render_receives_viewport_width_from_config():
    cfg = make_cfg({"comments_width": 777})
    renderer = FakeRenderer()
    result = await run_comments_flow(cfg, make_song(), FakeProvider(page=PAGE), FakeTransport(), renderer)
    assert result.has_image
    assert renderer.html_calls[0].extra["viewport_width"] == 777


async def test_viewport_rejected_falls_back_to_plain_request():
    cfg = make_cfg({"lyrics_width": 900})
    renderer = FakeRenderer(reject_viewport=True)
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer)
    assert result.has_image, "去掉 viewport 重试后应当成功出图"
    assert len(renderer.html_calls) == 2, "带 viewport 失败后应重试一次"
    assert "viewport_width" in renderer.html_calls[0].extra
    assert "viewport_width" not in (renderer.html_calls[1].extra or {})
    # 记住这个端点不接受 viewport，下次不再白试
    assert ("", "auto") in lyrics_flow_module._REJECTED_ENDPOINTS


async def test_rejected_endpoint_is_not_probed_again():
    cfg = make_cfg({})
    lyrics_flow_module._REJECTED_ENDPOINTS.add(("", "auto"))
    renderer = FakeRenderer(reject_viewport=True)
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer)
    assert result.has_image
    assert len(renderer.html_calls) == 1
    assert "viewport_width" not in (renderer.html_calls[0].extra or {})


async def test_viewport_renderer_swallows_delegate_exceptions():
    class Boom:
        async def render_html(self, *args, **kwargs):
            raise RuntimeError("boom")

        async def render_markdown(self, *args, **kwargs):
            raise RuntimeError("boom")

    wrapped = ViewportRenderer(Boom(), width=900)
    assert await wrapped.render_html("tpl", {}, RenderOptions()) is None
    assert await wrapped.render_markdown("md", RenderOptions()) is None


def test_resolve_renderer_returns_none_without_renderer():
    assert resolve_renderer(make_cfg({}), None) is None
    wrapped = resolve_renderer(make_cfg({"lyrics_width": 640}), FakeRenderer())
    assert isinstance(wrapped, ViewportRenderer)
    assert wrapped.width == 640


# ------------------------------------------------------------------ 歌词流程


async def test_lyrics_disabled_skips_everything():
    cfg = make_cfg({"lyrics_enable": False})
    provider = FakeProvider(lyric=LYRIC)
    renderer = FakeRenderer()
    assert await run_lyrics_flow(cfg, make_song(), provider, FakeTransport(), renderer) is None
    assert provider.requests["lyrics"] == 0
    assert renderer.html_calls == []


async def test_lyrics_render_failure_falls_back_to_text():
    cfg = make_cfg({"lyrics_fallback_text": True})
    renderer = FakeRenderer(fail_all=True)
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer)
    assert result.status == "render_failed"
    assert result.image == ""
    assert result.text == build_lyrics_text(make_song(), LYRIC, cfg.lyrics_max_lines)
    assert "故事的小黄花" in result.text


async def test_lyrics_render_failure_without_fallback_text_is_silent():
    cfg = make_cfg({"lyrics_fallback_text": False})
    result = await run_lyrics_flow(
        cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), FakeRenderer(fail_all=True)
    )
    assert result.status == "render_failed"
    assert result.text == "" and result.image == ""


async def test_lyrics_t2i_disabled_uses_plain_text():
    cfg = make_cfg({"lyrics_t2i": False})
    renderer = FakeRenderer()
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer)
    assert result.image == ""
    assert "故事的小黄花" in result.text
    assert renderer.html_calls == []


async def test_lyrics_without_renderer_uses_plain_text():
    cfg = make_cfg({})
    result = await run_lyrics_flow(cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), None)
    assert result.image == "" and result.text


async def test_lyrics_empty_reports_empty_state():
    cfg = make_cfg({})
    provider = FakeProvider(lyric=Lyric())
    result = await run_lyrics_flow(cfg, make_song(), provider, FakeTransport(), FakeRenderer())
    assert result.status == "empty"
    assert "没有" in result.text or "暂时" in result.text
    assert result.image == ""


async def test_lyrics_fetch_failure_is_not_fatal():
    cfg = make_cfg({})
    provider = FakeProvider(raise_lyrics=True)
    result = await run_lyrics_flow(cfg, make_song(), provider, FakeTransport(), FakeRenderer())
    assert result.status == "empty"
    assert provider.requests["lyrics"] == 1


async def test_lyrics_without_provider_is_not_fatal():
    result = await run_lyrics_flow(make_cfg({}), make_song(), None, FakeTransport(), FakeRenderer())
    assert result.status == "empty"


async def test_lyrics_prefetched_lyric_is_not_refetched():
    provider = FakeProvider(lyric=None)
    result = await run_lyrics_flow(
        make_cfg({}), make_song(), provider, FakeTransport(), FakeRenderer(), lyric=LYRIC
    )
    assert result.has_image
    assert provider.requests["lyrics"] == 0


# ------------------------------------------------------------------ 评论流程


async def test_comments_disabled_skips_everything():
    cfg = make_cfg({"comments_enable": False})
    provider = FakeProvider(page=PAGE)
    renderer = FakeRenderer()
    assert await run_comments_flow(cfg, make_song(), provider, FakeTransport(), renderer) is None
    assert provider.requests["comments"] == 0
    assert renderer.html_calls == []


async def test_comments_fetch_failure_has_its_own_message():
    cfg = make_cfg({})
    result = await run_comments_flow(cfg, make_song(), FakeProvider(page=None), FakeTransport(), FakeRenderer())
    assert result.status == "fetch_failed"
    assert result.text == FETCH_FAILED_MESSAGE
    assert "失败" in result.text
    assert "还没有评论" not in result.text


async def test_comments_empty_page_has_its_own_message():
    cfg = make_cfg({})
    result = await run_comments_flow(
        cfg, make_song(), FakeProvider(page=CommentPage(items=[], total=0)), FakeTransport(), FakeRenderer()
    )
    assert result.status == "empty"
    assert result.text == EMPTY_COMMENT_MESSAGE
    assert "还没有评论" in result.text
    assert result.text != FETCH_FAILED_MESSAGE
    assert result.total == 0 and result.shown == 0


async def test_comments_render_failure_falls_back_to_text():
    cfg = make_cfg({"comments_fallback_text": True})
    renderer = FakeRenderer(fail_all=True)
    result = await run_comments_flow(cfg, make_song(), FakeProvider(page=PAGE), FakeTransport(), renderer)
    assert result.status == "render_failed"
    assert result.image == ""
    assert result.text == build_comments_text(make_song(), PAGE, cfg)
    assert "小明" in result.text


async def test_comments_render_failure_without_fallback_is_silent():
    cfg = make_cfg({"comments_fallback_text": False})
    result = await run_comments_flow(
        cfg, make_song(), FakeProvider(page=PAGE), FakeTransport(), FakeRenderer(fail_all=True)
    )
    assert result.status == "render_failed" and result.text == ""


async def test_comments_count_zero_is_disabled():
    cfg = make_cfg({"comments_count": 0})
    provider = FakeProvider(page=PAGE)
    result = await run_comments_flow(cfg, make_song(), provider, FakeTransport(), FakeRenderer())
    assert result.status == "disabled"
    assert provider.requests["comments"] == 0


async def test_comments_passes_sort_and_paging():
    cfg = make_cfg({"comments_count": 5, "comments_page": 3, "comments_sort": "new"})
    provider = FakeProvider(page=PAGE)
    await run_comments_flow(cfg, make_song(), provider, FakeTransport(), FakeRenderer())
    assert provider.comment_args == [{"limit": 5, "offset": 10, "sort": "new"}]
    assert comments_offset(cfg) == 10


async def test_comments_without_sort_support_still_works():
    cfg = make_cfg({"comments_count": 3, "comments_sort": "hot"})
    provider = LegacyProvider(page=PAGE)
    assert supports_sort_argument(provider.comments) is False
    result = await run_comments_flow(cfg, make_song(), provider, FakeTransport(), FakeRenderer())
    assert result.has_image
    assert provider.comment_args == [{"limit": 3, "offset": 0, "sort": None}]


async def test_comments_render_failure_without_renderer():
    result = await run_comments_flow(make_cfg({}), make_song(), FakeProvider(page=PAGE), FakeTransport(), None)
    assert result.status == "render_failed" and result.text


async def test_comments_prefetched_page_is_not_refetched():
    provider = FakeProvider(page=None)
    result = await run_comments_flow(
        make_cfg({}), make_song(), provider, FakeTransport(), FakeRenderer(), page=PAGE
    )
    assert result.has_image
    assert provider.requests["comments"] == 0


def test_supports_sort_argument_detects_var_keyword():
    class WithKwargs:
        async def comments(self, song, transport, **kwargs):
            ...

    class Plain:
        async def comments(self, song, transport, limit=20, offset=0):
            ...

    assert supports_sort_argument(WithKwargs().comments) is True
    assert supports_sort_argument(Plain().comments) is False
    assert supports_sort_argument(object()) is False


# ------------------------------------------------------------------ 真实端点（本地 mock）


class _EndpointRecorder:
    """本地 t2i mock 端点：记录请求体，可选拒绝 viewport 参数。"""

    def __init__(self, *, reject_viewport: bool = False) -> None:
        self.reject_viewport = reject_viewport
        self.options: list[dict] = []
        self.paths: list[str] = []

    async def __aenter__(self):
        from aiohttp import web

        async def handle(request):
            payload = await request.json()
            options = payload.get("options") or {}
            self.options.append(dict(options))
            self.paths.append(request.path)
            if self.reject_viewport and "viewport_width" in options:
                return web.json_response({"error": "extra fields not permitted"}, status=400)
            return web.json_response({"data": {"id": "img-1"}})

        self._app = web.Application()
        self._app.router.add_post("/text2img/generate", handle)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info):
        await self._runner.cleanup()
        return False


async def test_real_endpoint_receives_viewport_and_returns_image():
    from core.renderer import DefaultRenderer

    async with _EndpointRecorder() as endpoint:
        cfg = make_cfg(
            {
                "lyrics_width": 1024,
                "lyrics_render_mode": "network",
                "lyrics_t2i_endpoint": endpoint.base_url,
            }
        )
        renderer = DefaultRenderer(star=None)
        result = await run_lyrics_flow(
            cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer
        )
    assert result.has_image
    assert result.image == f"{endpoint.base_url}/text2img/img-1"
    assert endpoint.paths == ["/text2img/generate"]
    assert endpoint.options[0]["viewport_width"] == 1024
    assert endpoint.options[0]["viewport_height"] == VIEWPORT_HEIGHT
    assert endpoint.options[0]["full_page"] is True
    assert endpoint.options[0]["quality"] == 80


async def test_real_endpoint_rejecting_viewport_is_retried_without_it():
    from core.renderer import DefaultRenderer

    async with _EndpointRecorder(reject_viewport=True) as endpoint:
        cfg = make_cfg(
            {
                "lyrics_width": 900,
                "lyrics_render_mode": "network",
                "lyrics_t2i_endpoint": endpoint.base_url,
            }
        )
        renderer = DefaultRenderer(star=None)
        result = await run_lyrics_flow(
            cfg, make_song(), FakeProvider(lyric=LYRIC), FakeTransport(), renderer
        )
    assert result.has_image, "去掉 viewport 重试后应当出图"
    assert len(endpoint.options) == 2
    assert "viewport_width" in endpoint.options[0]
    assert "viewport_width" not in endpoint.options[1]


# ------------------------------------------------------------------ 共享辅助：session / logging


class _Closable:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_transport_session_closes_created_transport(monkeypatch):
    import core.session as session_module

    created = _Closable()
    monkeypatch.setattr(session_module, "build_transport", lambda cfg: created)
    async with session_module.transport_session(make_cfg({})) as transport:
        assert transport is created
        assert created.closed == 0
    assert created.closed == 1


async def test_transport_session_closes_on_exception(monkeypatch):
    import core.session as session_module

    created = _Closable()
    monkeypatch.setattr(session_module, "build_transport", lambda cfg: created)
    with pytest.raises(RuntimeError):
        async with session_module.transport_session(make_cfg({})):
            raise RuntimeError("boom")
    assert created.closed == 1, "异常路径也必须关闭 transport"


async def test_transport_session_does_not_close_injected_transport():
    import core.session as session_module

    injected = _Closable()
    async with session_module.transport_session(make_cfg({}), transport=injected) as transport:
        assert transport is injected
    assert injected.closed == 0, "外部注入的 transport 生命周期归调用方"


async def test_transport_session_builds_and_closes_real_transport():
    import core.session as session_module

    async with session_module.transport_session(make_cfg({"netease_mode": "official_direct"})) as transport:
        assert transport.mode == "official_direct"
        assert str(transport.base_url).startswith("https://")
        # session 是惰性创建的（不联网就不会真正建连接）
        assert transport.session is None
        transport._ensure_session()
        assert transport.session is not None
        assert transport.closed is False
    assert transport.closed is True
    assert transport.session is None, "退出上下文必须关闭 session"


async def test_transport_session_swallows_close_errors(monkeypatch):
    import core.session as session_module

    class Boom:
        async def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(session_module, "build_transport", lambda cfg: Boom())
    async with session_module.transport_session(make_cfg({})) as transport:
        assert transport is not None


def test_redact_masks_secrets():
    import core.logging_utils as logging_utils

    text = logging_utils.redact("Cookie: MUSIC_U=abc123; token=xyz; other=1")
    assert "abc123" not in text and "xyz" not in text
    assert "***" in text
    assert "abc.def" not in logging_utils.redact("Authorization: Bearer abc.def")
    assert logging_utils.redact(None) == ""


def test_redact_mapping_recurses():
    import core.logging_utils as logging_utils

    data = logging_utils.redact_mapping(
        {"cookie": "MUSIC_U=abc", "nested": {"token": "t"}, "name": "晴天"}
    )
    assert data["cookie"] == "***"
    assert data["nested"]["token"] == "***"
    assert data["name"] == "晴天"


def test_debug_enabled_reads_nested_config():
    import core.logging_utils as logging_utils

    assert logging_utils.debug_enabled({"send": {"debug_log": True}}) is True
    assert logging_utils.debug_enabled(make_cfg({"debug_log": True})) is True
    assert logging_utils.debug_enabled(make_cfg({})) is False
    assert logging_utils.debug_enabled(True) is True
    assert logging_utils.debug_enabled(None) is False


def test_log_debug_respects_switch_and_redacts_args(caplog):
    import logging as std_logging

    import core.logging_utils as logging_utils

    logger = std_logging.getLogger("astrbot_plugin_music.test")
    logger.setLevel(std_logging.DEBUG)
    with caplog.at_level(std_logging.DEBUG, logger="astrbot_plugin_music.test"):
        # debug_log 关闭：什么都不写
        logging_utils.log_debug(make_cfg({"debug_log": False}), "cookie=%s", "MUSIC_U=secret", logger=logger)
        # 打开：先按 %-格式拼装再脱敏（顺序反了会吃掉 %s，logging 会抛 TypeError）
        logging_utils.log_debug(make_cfg({"debug_log": True}), "cookie=%s", "MUSIC_U=secret", logger=logger)
        logging_utils.log_debug(make_cfg({"debug_log": True}), "结果=%s", "晴天", logger=logger)
    texts = [record.getMessage() for record in caplog.records if record.name == "astrbot_plugin_music.test"]
    assert texts == ["cookie=***", "结果=晴天"]


def test_describe_config_does_not_leak_cookie():
    import core.logging_utils as logging_utils

    text = logging_utils.describe_config(make_cfg({"cookie": "MUSIC_U=supersecret", "debug_log": True}))
    assert "supersecret" not in text
    assert logging_utils.config_value({"send": {"debug_log": True}}, "debug_log") is True
    assert logging_utils.config_value({}, "missing", "fallback") == "fallback"


# ------------------------------------------------------------------ 静态约束


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_flow_modules_do_not_import_http_clients():
    for name in ("lyrics_flow", "comments_flow"):
        path = PLUGIN_ROOT / "core" / f"{name}.py"
        imports = _module_level_imports(path)
        assert not (imports & {"aiohttp", "requests", "httpx", "urllib", "http", "socket"}), (
            f"{name}.py 不应该直接依赖 HTTP 客户端"
        )
        source = path.read_text(encoding="utf-8")
        assert "aiohttp" not in source
        assert "ClientSession" not in source