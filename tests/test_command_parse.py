"""main.py：指令解析、权限、编排（run_music_request）、注册与中文 ImportError。

main.py 是 AstrBot 插件模块（相对 import 依赖包上下文），所以这里把插件根目录的
**父目录**加入 sys.path，用 `astrbot_plugin_music.main` 这个名字导入它——
与 AstrBot 的加载方式一致。需要 astrbot 的用例通过 require_astrbot() 自动跳过。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:  # pragma: no cover - 首次导入时生效
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

# 关键：被测模块是 astrbot_plugin_music.main，它 import 的是 astrbot_plugin_music.core.*。
# 测试必须使用**同一份模块实例**的类型（否则 isinstance 判定会失败，配置会静默退回默认值）。
from astrbot_plugin_music.core.config import RuntimeConfig
from astrbot_plugin_music.core.models import (
    CommentItem,
    CommentPage,
    Lyric,
    LyricLine,
    SongInfo,
)
from astrbot_plugin_music.core.ratelimit import RateLimiter
from tests.conftest import require_astrbot

MODULE_NAME = "astrbot_plugin_music.main"
CARD_MODULE = "astrbot_plugin_music.core.cards"
IMAGE_URL = "https://img.example.com/lyrics.png"
COMMENT_IMAGE_URL = "https://img.example.com/comments.png"
LYRIC = Lyric(text="故事的小黄花", lines=[LyricLine(text="故事的小黄花")])
PAGE = CommentPage(
    items=[CommentItem(user="小明", content="青春啊", liked=8)],
    total=1,
    has_more=False,
)
SONG = SongInfo(
    id="186016",
    name="晴天",
    artists=["周杰伦"],
    album="叶惠美",
    duration_ms=225_000,
    cover_url="https://p1.music.126.net/cover.jpg",
)


@pytest.fixture(scope="module")
def plugin_main():
    require_astrbot()
    return importlib.import_module(MODULE_NAME)


@pytest.fixture(scope="module")
def fake_components():
    class Music:
        def __init__(self, **kwargs: Any) -> None:
            self.id = kwargs.get("id")
            self.url = kwargs.get("url", "")
            self.audio = kwargs.get("audio", "")
            self.title = kwargs.get("title", "")
            self.content = kwargs.get("content", "")
            self.image = kwargs.get("image", "")

    class Share:
        def __init__(self, url="", title="", content="", image="", **_):
            self.url, self.title, self.content, self.image = url, title, content, image

    class Plain:
        def __init__(self, text="", **_):
            self.text = text

    class Record:
        def __init__(self, file="", url="", **_):
            self.file, self.url = file, url

    class At:
        def __init__(self, qq="", name="", **_):
            self.qq, self.name = qq, name

    return SimpleNamespace(Music=Music, Share=Share, Record=Record, Plain=Plain, At=At)


def make_cfg(raw: Any = None, **overrides: Any) -> RuntimeConfig:
    data: dict = dict(raw) if isinstance(raw, dict) else {}
    data.update(overrides)
    return RuntimeConfig.from_mapping(data)


class FakeProvider:
    """同时满足 MusicProvider 协议与 NeteaseProvider 的额外能力。"""

    key = "fake"
    display_name = "假网易云"

    def __init__(
        self,
        *,
        songs: list[SongInfo] | None = None,
        lyric: Any = LYRIC,
        page: Any = PAGE,
        audio: str = "",
        search_error: Exception | None = None,
    ) -> None:
        self.songs = [SONG] if songs is None else list(songs)
        self.lyric = lyric
        self.page = page
        self.audio = audio
        self.search_error = search_error
        self.search_calls: list[Any] = []
        self.lyrics_calls = 0
        self.comments_calls = 0
        self.audio_calls = 0

    async def search(self, query, transport):
        self.search_calls.append(query)
        if self.search_error is not None:
            raise self.search_error
        return list(self.songs)

    async def lyrics(self, song, transport):
        self.lyrics_calls += 1
        return self.lyric

    async def comments(self, song, transport, limit=20, offset=0, *, sort=None):
        self.comments_calls += 1
        return self.page

    async def audio_url(self, song, transport, *, level="standard"):
        self.audio_calls += 1
        return self.audio

    def card_payload(self, song):
        return {
            "kind": "music",
            "type": "163",
            "id": getattr(song, "id", ""),
            "title": getattr(song, "name", ""),
            "content": "周杰伦 · 叶惠美 · 网易云音乐",
            "image": getattr(song, "cover_url", ""),
            "url": getattr(song, "url", ""),
            "audio": "",
        }


class FakeRenderer:
    def __init__(self, *, html: str = IMAGE_URL, markdown: str = COMMENT_IMAGE_URL) -> None:
        self.html = html
        self.markdown = markdown
        self.calls = 0

    async def render_html(self, template, data=None, options=None):
        self.calls += 1
        return self.html

    async def render_markdown(self, md, options=None):
        self.calls += 1
        return self.markdown


class FakeEvent:
    """鸭子类型事件（只实现 handler 用到的方法）。"""

    def __init__(
        self,
        text: str,
        *,
        group_id: str = "group-1",
        user_id: str = "user-1",
        umo: str = "aiocqhttp:GroupMessage:group-1",
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._user_id = user_id
        self.results: list[tuple[str, Any]] = []
        self.stopped = False

    def get_message_str(self) -> str:
        return self.message_str

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._user_id

    def plain_result(self, text: str):
        self.results.append(("text", text))
        return ("text", text)

    def chain_result(self, chain):
        self.results.append(("chain", list(chain)))
        return ("chain", list(chain))

    def image_result(self, url: str):
        self.results.append(("image", url))
        return ("image", url)

    def stop_event(self) -> None:
        self.stopped = True


# ------------------------------------------------------------------ 指令解析


def test_parse_keyword_only(plugin_main):
    request = plugin_main.parse_command_text("/点歌 晴天")
    assert request.is_valid
    assert (request.keyword, request.artist) == ("晴天", "")


def test_parse_keyword_and_artist(plugin_main):
    request = plugin_main.parse_command_text("/点歌 晴天 周杰伦")
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")


def test_parse_artist_with_spaces_is_kept_whole(plugin_main):
    request = plugin_main.parse_command_text("/点歌 晴天 周 杰 伦")
    assert request.keyword == "晴天"
    assert request.artist == "周 杰 伦"


def test_parse_without_prefix(plugin_main):
    request = plugin_main.parse_command_text("点歌 晴天 周杰伦")
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")
    assert request.command == "点歌"


def test_parse_fullwidth_prefix(plugin_main):
    request = plugin_main.parse_command_text("／点歌 晴天")
    assert request.keyword == "晴天"


def test_parse_exclamation_prefix(plugin_main):
    request = plugin_main.parse_command_text("！点歌 晴天")
    assert request.keyword == "晴天"


def test_parse_collapses_whitespace(plugin_main):
    request = plugin_main.parse_command_text("/点歌    晴天     周杰伦")
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")


def test_parse_aliases(plugin_main):
    for text in ("/听歌 晴天", "听歌 晴天", "/music 晴天", "/MUSIC 晴天"):
        request = plugin_main.parse_command_text(text)
        assert request.is_valid, text
        assert request.keyword == "晴天", text


def test_parse_custom_alias_from_config(plugin_main):
    request = plugin_main.parse_command_text("/dj 晴天 周杰伦", aliases=["dj"])
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")


def test_parse_missing_keyword_returns_chinese_hint(plugin_main):
    for text in ("/点歌", "点歌", "/点歌   ", "点歌 "):
        request = plugin_main.parse_command_text(text)
        assert request.is_valid is False, text
        assert request.error == "missing_keyword"
        assert "歌名" in request.message


def test_parse_empty_and_garbage_input(plugin_main):
    for value in (None, "", 0, [], {}, True):
        request = plugin_main.parse_command_text(value)
        assert request.is_valid is False
        assert request.message


def test_parse_unknown_command_without_prefix(plugin_main):
    request = plugin_main.parse_command_text("天气 晴天")
    assert request.is_valid is False
    assert request.error == "not_command"


def test_parse_truncates_overlong_input(plugin_main):
    request = plugin_main.parse_command_text("/点歌 " + "长" * 200 + " " + "手" * 100)
    assert len(request.keyword) <= plugin_main.MAX_KEYWORD_CHARS
    assert len(request.artist) <= plugin_main.MAX_ARTIST_CHARS


def test_parse_dash_is_not_treated_as_artist_separator(plugin_main):
    request = plugin_main.parse_command_text("/点歌 Love-Story")
    assert request.keyword == "Love-Story"
    assert request.artist == ""


def test_parse_request_to_query(plugin_main):
    request = plugin_main.parse_command_text("/点歌 晴天 周杰伦")
    query = request.to_query(limit=7)
    assert (query.keyword, query.artist, query.limit) == ("晴天", "周杰伦", 7)


ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")
"""零宽字符与 BOM：聊天里看不见，但（修复前）会被当成合法歌名（B1）。"""


def test_parse_invisible_only_keyword_is_missing(plugin_main):
    for char in ZERO_WIDTH_CHARS:
        request = plugin_main.parse_command_text(f"/点歌 {char}")
        assert request.is_valid is False, repr(char)
        assert request.error == "missing_keyword", repr(char)
        assert request.keyword == "", repr(char)
        assert "歌名" in request.message, repr(char)


def test_parse_mixed_invisible_only_keyword_is_missing(plugin_main):
    for blob in ("\u200b\u200c\u200d", "\ufeff\u200b", "\u200b\u3000", "\u2060\u2060", "\u200b \u200c"):
        request = plugin_main.parse_command_text(f"/点歌 {blob}")
        assert request.is_valid is False, repr(blob)
        assert request.error in {"empty", "missing_keyword"}, repr(blob)
        assert request.keyword == "", repr(blob)
        # 不可见字符绝不能出现在歌名里被送给搜索
        for char in ZERO_WIDTH_CHARS:
            assert char not in request.keyword, repr(blob)


def test_parse_invisible_only_text_without_command(plugin_main):
    for char in ZERO_WIDTH_CHARS:
        request = plugin_main.parse_command_text(char)
        assert request.is_valid is False, repr(char)
        assert request.error == "empty", repr(char)


def test_parse_invisible_inside_command_word(plugin_main):
    request = plugin_main.parse_command_text("/点\u200b歌 晴天")
    assert request.is_valid
    assert request.command == "点歌"
    assert request.keyword == "晴天"


def test_parse_bom_before_wake_prefix(plugin_main):
    request = plugin_main.parse_command_text("\ufeff/点歌 晴天 周杰伦")
    assert request.is_valid
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")


def test_parse_invisible_inside_keyword_keeps_visible_part(plugin_main):
    assert plugin_main.parse_command_text("/点歌 晴\u200b天").keyword == "晴天"
    spliced = plugin_main.parse_command_text("/点歌 晴\u200b天 周\u200c杰\u200d伦")
    assert spliced.keyword == "晴天"
    assert spliced.artist == "周杰伦"


def test_parse_invisible_in_artist_is_removed(plugin_main):
    request = plugin_main.parse_command_text("/点歌 晴天 周杰伦\ufeff")
    assert (request.keyword, request.artist) == ("晴天", "周杰伦")
    only_invisible = plugin_main.parse_command_text("/点歌 晴天 \u200b")
    assert only_invisible.keyword == "晴天"
    assert only_invisible.artist == ""


def test_parse_normal_names_unaffected_by_invisible_filter(plugin_main):
    """回归保护：emoji / CJK / 全角空格 / 路径样式字符串行为不变。"""
    emoji = plugin_main.parse_command_text("/点歌 🎵<b>晴天</b> 周杰伦")
    assert emoji.is_valid and emoji.keyword == "🎵<b>晴天</b>" and emoji.artist == "周杰伦"
    assert plugin_main.parse_command_text("/点歌 ../../etc/passwd").is_valid
    assert plugin_main.parse_command_text("/点歌 Love-Story").keyword == "Love-Story"
    spaced = plugin_main.parse_command_text("/点歌 　　晴天　　周杰伦")  # 全角空格
    assert (spaced.keyword, spaced.artist) == ("晴天", "周杰伦")
    assert plugin_main.parse_command_text("/点歌 晴天 周 杰 伦").artist == "周 杰 伦"


# ------------------------------------------------------------------ 权限


def test_check_access_default_allows(plugin_main):
    assert plugin_main.check_access(make_cfg({}), group_id="g", user_id="u") == ""


def test_check_access_disabled(plugin_main):
    assert plugin_main.check_access(make_cfg({"enabled": False}), group_id="g", user_id="u") == "disabled"


def test_check_access_group_lists(plugin_main):
    blacklist = make_cfg({"group_blacklist": ["g1"]})
    assert plugin_main.check_access(blacklist, group_id="g1") == "group"
    assert plugin_main.check_access(blacklist, group_id="g2") == ""

    whitelist = make_cfg({"group_whitelist": ["g1"]})
    assert plugin_main.check_access(whitelist, group_id="g1") == ""
    assert plugin_main.check_access(whitelist, group_id="g2") == "group"


def test_check_access_user_blacklist(plugin_main):
    cfg = make_cfg({"user_blacklist": ["u1"]})
    assert plugin_main.check_access(cfg, user_id="u1") == "user"
    assert plugin_main.check_access(cfg, user_id="u2") == ""


# ------------------------------------------------------------------ 编排


async def test_run_music_request_full_path(plugin_main, fake_components):
    provider = FakeProvider()
    outcome = await plugin_main.run_music_request(
        make_cfg({"send_delay": 0}),
        keyword="晴天",
        artist="周杰伦",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    assert outcome.ok is True and outcome.status == "ok"
    assert [kind for kind, _ in outcome.messages] == ["chain", "image", "image"]
    assert outcome.song is not None and outcome.song.id == "186016"
    assert provider.search_calls and provider.search_calls[0].limit == 5
    chain = outcome.messages[0][1]
    assert type(chain[0]).__name__ == "Music"
    assert chain[0]._type == "163"
    assert outcome.card is not None and outcome.card.degraded is False


async def test_run_music_request_without_renderer_uses_text_fallbacks(plugin_main, fake_components):
    provider = FakeProvider()
    outcome = await plugin_main.run_music_request(
        make_cfg({"lyrics_fallback_text": True, "comments_fallback_text": True}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=None,
        factory=fake_components,
    )
    kinds = [kind for kind, _ in outcome.messages]
    assert kinds == ["chain", "text", "text"]
    assert "故事的小黄花" in outcome.messages[1][1]
    assert "小明" in outcome.messages[2][1]


async def test_run_music_request_not_found(plugin_main, fake_components):
    provider = FakeProvider(songs=[])
    outcome = await plugin_main.run_music_request(
        make_cfg({}), keyword="不存在", provider=provider, transport=object(), factory=fake_components
    )
    assert outcome.ok is False and outcome.status == "not_found"
    assert len(outcome.messages) == 1
    kind, text = outcome.messages[0]
    assert kind == "text" and "没有找到" in text and "不存在" in text
    assert provider.lyrics_calls == 0 and provider.comments_calls == 0


async def test_run_music_request_search_error_is_not_fatal(plugin_main, fake_components):
    provider = FakeProvider(search_error=RuntimeError("boom"))
    outcome = await plugin_main.run_music_request(
        make_cfg({}), keyword="晴天", provider=provider, transport=object(), factory=fake_components
    )
    assert outcome.status == "not_found"
    assert outcome.messages and outcome.messages[0][0] == "text"


async def test_run_music_request_rate_limited(plugin_main, fake_components):
    limiter = RateLimiter(cooldown_seconds=60, daily_limit=0)
    provider = FakeProvider()
    first = await plugin_main.run_music_request(
        make_cfg({}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        limiter=limiter,
        rate_key="umo:user",
        factory=fake_components,
    )
    assert first.ok is True

    second = await plugin_main.run_music_request(
        make_cfg({}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        limiter=limiter,
        rate_key="umo:user",
        factory=fake_components,
    )
    assert second.ok is False and second.status == "rate_limited"
    assert second.messages[0][0] == "text"
    assert "秒" in second.messages[0][1]
    assert provider.search_calls and len(provider.search_calls) == 1


async def test_run_music_request_daily_limit(plugin_main, fake_components):
    limiter = RateLimiter(cooldown_seconds=0, daily_limit=1)
    provider = FakeProvider()
    kwargs = dict(keyword="晴天", provider=provider, transport=object(), factory=fake_components)
    assert (await plugin_main.run_music_request(make_cfg({}), limiter=limiter, rate_key="k", **kwargs)).ok
    denied = await plugin_main.run_music_request(make_cfg({}), limiter=limiter, rate_key="k", **kwargs)
    assert denied.status == "rate_limited"
    assert "上限 1 次" in denied.messages[0][1]


async def test_run_music_request_missing_provider(plugin_main, fake_components, monkeypatch):
    monkeypatch.setattr(plugin_main, "resolve_provider", lambda config: None)
    outcome = await plugin_main.run_music_request(
        make_cfg({}), keyword="晴天", transport=object(), factory=fake_components
    )
    assert outcome.status == "no_provider"
    assert "provider" in outcome.messages[0][1]


async def test_run_music_request_respects_lyrics_and_comments_switches(plugin_main, fake_components):
    provider = FakeProvider()
    outcome = await plugin_main.run_music_request(
        make_cfg({"lyrics_enable": False, "comments_enable": False}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    assert [kind for kind, _ in outcome.messages] == ["chain"]
    assert provider.lyrics_calls == 0
    assert provider.comments_calls == 0


async def test_run_music_request_never_silent_when_everything_disabled(plugin_main, fake_components):
    provider = FakeProvider()
    outcome = await plugin_main.run_music_request(
        make_cfg({"card_enable": False, "lyrics_enable": False, "comments_enable": False}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    assert outcome.ok is True
    assert len(outcome.messages) == 1
    kind, text = outcome.messages[0]
    assert kind == "text"
    assert "晴天" in text and "186016" in text


async def test_run_music_request_custom_card_uses_provider_audio(plugin_main, fake_components):
    provider = FakeProvider(audio="https://m8.music.126.net/song.mp3")
    outcome = await plugin_main.run_music_request(
        make_cfg({"card_type": "custom"}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    chain = outcome.messages[0][1]
    assert chain[0]._type == "custom"
    assert chain[0].audio.endswith(".mp3")
    assert provider.audio_calls == 1


async def test_run_music_request_custom_without_audio_degrades(plugin_main, fake_components):
    provider = FakeProvider(audio="")
    outcome = await plugin_main.run_music_request(
        make_cfg({"card_type": "custom", "card_fallback": "text"}),
        keyword="晴天",
        provider=provider,
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    kind, payload = outcome.messages[0]
    # 退化后的纯文本以 Plain 组件形式挂在链上（respond stage 只认组件）
    assert kind == "chain"
    assert [type(item).__name__ for item in payload] == ["Plain"]
    assert "晴天" in payload[0].text
    assert outcome.card is not None and outcome.card.degraded is True
    assert outcome.card.text_payload == ""  # 不要在链之外再发一遍


# ------------------------------------------------------------------ prepare_messages


def test_prepare_messages_without_at(plugin_main, fake_components):
    outcome = plugin_main.MusicRequestOutcome(messages=[("text", "hi")])
    assert plugin_main.prepare_messages(outcome, factory=fake_components) == [("text", "hi")]


def test_prepare_messages_adds_at_to_first_chain(plugin_main, fake_components):
    Music = fake_components.Music
    outcome = plugin_main.MusicRequestOutcome(
        messages=[("chain", [Music(id=1, title="t")]), ("image", IMAGE_URL)]
    )
    prepared = plugin_main.prepare_messages(
        outcome, reply_with_at=True, user_id="12345", factory=fake_components
    )
    kind, chain = prepared[0]
    assert kind == "chain"
    assert type(chain[0]).__name__ == "At"
    assert chain[0].qq == "12345"
    assert prepared[1] == ("image", IMAGE_URL)


def test_prepare_messages_wraps_text_as_chain(plugin_main, fake_components):
    outcome = plugin_main.MusicRequestOutcome(messages=[("text", "你好")])
    prepared = plugin_main.prepare_messages(
        outcome, reply_with_at=True, user_id="12345", factory=fake_components
    )
    kind, chain = prepared[0]
    assert kind == "chain"
    assert [type(item).__name__ for item in chain] == ["At", "Plain"]
    assert chain[1].text == "你好"


def test_prepare_messages_skips_at_when_no_component_factory(plugin_main, monkeypatch):
    import astrbot_plugin_music.core.cards.music_card as music_card

    monkeypatch.setattr(music_card, "COMPONENT_FACTORY", None)
    outcome = plugin_main.MusicRequestOutcome(messages=[("text", "你好")])
    assert plugin_main.prepare_messages(outcome, reply_with_at=True, user_id="1") == [("text", "你好")]


def test_prepare_messages_ignores_falsy_payloads(plugin_main):
    outcome = plugin_main.MusicRequestOutcome(messages=[("text", ""), ("image", "")])
    assert plugin_main.prepare_messages(outcome) == []


# ------------------------------------------------------------------ handler


async def _run_handler(plugin_main, event, config, monkeypatch, *, provider=None, renderer=None):
    provider = provider or FakeProvider()
    renderer = renderer or FakeRenderer()
    monkeypatch.setattr(plugin_main, "resolve_provider", lambda config: provider)
    monkeypatch.setattr(plugin_main, "DefaultRenderer", lambda star: renderer)
    plugin = plugin_main.MusicPlugin(context=None, config=config)
    results = [item async for item in plugin.cmd_song_request(event)]
    return plugin, results, provider


async def test_handler_sends_card_then_images(plugin_main, fake_components, monkeypatch):
    event = FakeEvent("/点歌 晴天 周杰伦")
    _, results, provider = await _run_handler(
        plugin_main,
        event,
        {"send_delay": 0, "card_type": "163"},
        monkeypatch,
        renderer=FakeRenderer(html=IMAGE_URL, markdown=COMMENT_IMAGE_URL),
    )
    assert [item[0] for item in results] == ["chain", "image", "image"]
    assert provider.search_calls
    assert event.results == results


async def test_handler_replies_usage_when_keyword_missing(plugin_main, fake_components, monkeypatch):
    event = FakeEvent("/点歌")
    _, results, provider = await _run_handler(plugin_main, event, {"send_delay": 0}, monkeypatch)
    assert [item[0] for item in results] == ["text"]
    assert "歌名" in results[0][1]
    assert provider.search_calls == []


async def test_handler_stays_silent_when_group_not_allowed(plugin_main, fake_components, monkeypatch):
    event = FakeEvent("/点歌 晴天", group_id="group-9")
    _, results, provider = await _run_handler(
        plugin_main, event, {"send_delay": 0, "group_whitelist": ["group-1"]}, monkeypatch
    )
    assert results == []
    assert event.stopped is True
    assert provider.search_calls == []


async def test_handler_stays_silent_when_disabled(plugin_main, fake_components, monkeypatch):
    event = FakeEvent("/点歌 晴天")
    _, results, provider = await _run_handler(plugin_main, event, {"enabled": False}, monkeypatch)
    assert results == []
    assert event.stopped is True
    assert provider.search_calls == []


async def test_handler_rate_limit_uses_umo_and_user_id(plugin_main, fake_components, monkeypatch):
    monkeypatch.setattr(plugin_main, "resolve_provider", lambda config: FakeProvider())
    monkeypatch.setattr(plugin_main, "DefaultRenderer", lambda star: FakeRenderer())
    plugin = plugin_main.MusicPlugin(context=None, config={"cooldown_seconds": 60})
    first = [item async for item in plugin.cmd_song_request(FakeEvent("/点歌 晴天"))]
    assert first
    second = [item async for item in plugin.cmd_song_request(FakeEvent("/点歌 晴天"))]
    assert [item[0] for item in second] == ["text"]
    assert "秒" in second[0][1]
    # 另一个用户不受影响
    other = [item async for item in plugin.cmd_song_request(FakeEvent("/点歌 晴天", user_id="user-2"))]
    assert [item[0] for item in other] == ["chain", "image", "image"]


# ------------------------------------------------------------------ 注册 / 导入保护


def test_plugin_class_is_star_subclass(plugin_main):
    from astrbot.api.star import Star

    assert issubclass(plugin_main.MusicPlugin, Star)
    assert plugin_main.MusicPlugin.__module__ == MODULE_NAME


def test_command_handler_registered(plugin_main):
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        f"{MODULE_NAME}_cmd_song_request"
    )
    assert handler is not None, "handler 没有注册到 star_handlers_registry"
    commands = [item for item in handler.event_filters if isinstance(item, CommandFilter)]
    assert commands, "handler 上缺少 CommandFilter"
    names = set()
    for item in commands:
        names.add(item.command_name)
        names.update(item.alias)
    assert "点歌" in names
    assert {"听歌", "music"} <= names


def test_register_metadata_recorded(plugin_main):
    from astrbot.core.star.star import star_map

    metadata = star_map.get(MODULE_NAME)
    assert metadata is not None
    assert metadata.name == "astrbot_plugin_music"
    assert metadata.author == "ysyhlly"


def test_import_error_mentions_astrbot_and_plugins_dir(monkeypatch):
    """astrbot 不可用时必须给出中文 ImportError，而不是裸 ImportError。"""
    # 模拟「AstrBot 不可用」：清空 astrbot 模块树，再让 import astrbot 直接失败
    for name in [n for n in list(sys.modules) if n == "astrbot" or n.startswith("astrbot.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "astrbot", None)
    monkeypatch.delitem(sys.modules, MODULE_NAME, raising=False)
    with pytest.raises(ImportError) as excinfo:
        importlib.import_module(MODULE_NAME)
    message = str(excinfo.value)
    assert "AstrBot" in message
    assert "data/plugins" in message
    assert re.search(r"[\u4e00-\u9fff]", message), "提示必须是中文"


def test_main_module_has_no_hardcoded_absolute_path():
    source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\\\", source), "插件源码里不允许写死 Windows 绝对路径"
    assert "_astrbot_ref" not in source


def test_metadata_yaml_matches_contract():
    text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert "name: astrbot_plugin_music" in text
    assert "author: ysyhlly" in text
    assert "https://github.com/ysyhlly/astrbot_plugin_music" in text
    assert 'astrbot_version: ">=4.16,<5"' in text
    assert "display_name:" in text and "short_desc:" in text and "tags:" in text

    import yaml

    data = yaml.safe_load(text)
    for key in ("name", "display_name", "short_desc", "desc", "version", "author", "repo", "astrbot_version", "tags"):
        assert key in data, key
    assert data["desc"].strip()