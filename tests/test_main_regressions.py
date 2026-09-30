"""Regression checks for real command dispatch and production message staging."""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.conftest import PLUGIN_ROOT, require_astrbot

sys.path.insert(0, str(PLUGIN_ROOT.parent))


@pytest.fixture
def plugin_main(monkeypatch):
    require_astrbot()
    main = importlib.import_module("astrbot_plugin_music.main")
    handler = main.star_handlers_registry.get_handler_by_full_name(
        "astrbot_plugin_music.main_cmd_song_request"
    )
    command = next(f for f in handler.event_filters if isinstance(f, main.CommandFilter))
    monkeypatch.setattr(command, "command_name", command.command_name)
    monkeypatch.setattr(command, "alias", set(command.alias))
    monkeypatch.setattr(command, "_cmpl_cmd_names", None)
    return main


async def test_configured_aliases_reach_handler_and_reload_clears_old_names(plugin_main, monkeypatch):
    from tests.test_command_parse import FakeEvent, FakeProvider

    class Event(FakeEvent):
        is_at_or_wake_command = True

        def set_extra(self, key, value):
            pass

        def get_platform_name(self):
            return "webchat"

    monkeypatch.setattr(plugin_main, "resolve_provider", lambda cfg: FakeProvider())
    settings = {
        "command_aliases": ["dj", "music radio"], "cooldown_seconds": 0,
        "send_delay": 0, "lyrics_enable": False, "comments_enable": False,
    }
    plugin = plugin_main.MusicPlugin(None, settings)
    command = plugin._command_filter
    assert set(command.get_complete_command_names()) == {"点歌", "dj", "music radio"}
    for text in ("dj 晴天 周杰伦", "music radio 晴天 周杰伦"):
        event = Event(text)
        assert command.filter(event, {})
        responses = [item async for item in plugin.cmd_song_request(event)]
        assert len(responses) == 1
        assert responses[0][0] == "chain"
        assert "晴天" in responses[0][1][0].text
        assert "music.163.com" in responses[0][1][0].text
    assert not command.filter(Event("听歌 晴天"), {})

    replacement = plugin_main.MusicPlugin(None, {**settings, "command_aliases": ["radio"]})
    assert set(replacement._command_filter.get_complete_command_names()) == {"点歌", "radio"}
    assert not replacement._command_filter.filter(Event("dj 晴天"), {})
    assert replacement._command_filter.filter(Event("radio 晴天"), {})


async def test_framework_renamed_command_is_parsed_after_wake_prefix_removal(plugin_main, monkeypatch):
    from tests.test_command_parse import FakeEvent, FakeProvider

    monkeypatch.setattr(plugin_main, "resolve_provider", lambda cfg: FakeProvider())
    plugin = plugin_main.MusicPlugin(None, {
        "send_delay": 0, "lyrics_enable": False, "comments_enable": False,
    })
    plugin._command_filter.command_name = "radio"
    plugin._command_filter.alias = set()
    plugin._command_filter._cmpl_cmd_names = None
    results = [item async for item in plugin.cmd_song_request(FakeEvent("radio 晴天"))]
    assert results[0][0] == "chain"


@pytest.mark.parametrize("fallback", [False, True])
async def test_explicit_text_survives_global_and_feature_fallback_switches(plugin_main, fallback):
    from tests.test_command_parse import FakeProvider, FakeRenderer

    provider = FakeProvider()
    renderer = FakeRenderer()
    cfg = {
        "lyrics_t2i": False, "comments_t2i": False,
        "lyrics_fallback_text": fallback, "comments_fallback_text": fallback,
        "fallback_to_plain": fallback,
    }
    messages = []
    async for outcome, fresh in plugin_main.run_music_request_staged(
        cfg, keyword="晴天", provider=provider, renderer=renderer,
        platform_name="webchat",
    ):
        messages.extend(fresh)
    assert [kind for kind, payload in messages] == ["chain", "text", "text"]
    assert outcome.lyrics.status == "text"
    assert outcome.comments.status == "text"
    assert "故事的小黄花" in messages[1][1]
    assert "青春啊" in messages[2][1]
    assert renderer.calls == 0


async def test_platform_reaches_both_production_entrypoints(plugin_main):
    from tests.test_command_parse import FakeProvider

    settings = {"lyrics_enable": False, "comments_enable": False}
    result = await plugin_main.run_music_request(
        settings, keyword="晴天", provider=FakeProvider(), platform_name="telegram"
    )
    assert result.ok
    kind, chain = result.messages[0]
    assert kind == "chain"
    assert type(chain[0]).__name__ == "Plain"
    assert "music.163.com" in chain[0].text
