"""Regressions for platform delivery and disabled-card network requests."""

from __future__ import annotations

from typing import Any

import pytest

from core.cards import build_card, build_card_result, build_card_result_from_payload
from core.config import RuntimeConfig
from core.models import SongInfo
from core.netease.provider import NeteaseProvider
from tests.conftest import require_astrbot


SONG_URL = "https://music.163.com/song?id=186016"
AUDIO_URL = "https://example.test/song.mp3"


class RecordingTransport:
    mode = "official_direct"

    def __init__(self) -> None:
        self.requests: list[tuple[Any, Any]] = []

    async def request_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.requests.append((args, kwargs))
        return {"data": []}


@pytest.mark.parametrize("card_type", ["163", "custom", "share"])
async def test_disabled_cards_skip_payload_audio_and_factory(card_type: str) -> None:
    class UnusedProvider:
        def card_payload(self, song: Any) -> Any:
            pytest.fail("Disabled cards must not construct provider payloads")

        async def audio_url(self, song: Any, transport: Any) -> str:
            pytest.fail("Disabled cards must not query audio")

    def unused_factory() -> Any:
        pytest.fail("Disabled cards must not load message components")

    config = RuntimeConfig.from_mapping(
        {"card_enable": False, "card_type": card_type, "card_attach_audio": True}
    )
    song = SongInfo(id="186016", name="晴天")
    result = await build_card_result(
        song,
        config,
        provider=UnusedProvider(),
        transport=RecordingTransport(),
        factory=unused_factory,
    )
    assert result.reason == "card_disabled"
    assert result.components == []
    assert result.text_payload == ""
    assert result.requested_type == card_type
    assert await build_card(
        song,
        config,
        provider=UnusedProvider(),
        transport=RecordingTransport(),
        factory=unused_factory,
    ) == []


@pytest.mark.parametrize("card_type", ["163", "custom", "share"])
@pytest.mark.parametrize("attach_audio", [False, True])
async def test_real_webchat_receives_song_links_without_audio_requests(
    monkeypatch: pytest.MonkeyPatch, card_type: str, attach_audio: bool
) -> None:
    require_astrbot()
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import (
        AstrBotMessage,
        MessageMember,
        MessageType,
        PlatformMetadata,
    )
    from astrbot.core.platform.sources.webchat.webchat_event import (
        WebChatMessageEvent,
        webchat_queue_mgr,
    )
    from astrbot.core.utils.metrics import Metric

    queued: list[dict[str, Any]] = []

    async def capture_queue(request_id: str, payload: dict[str, Any]) -> bool:
        queued.append(payload)
        return True

    async def skip_metrics(**kwargs: Any) -> None:
        pass

    monkeypatch.setattr(webchat_queue_mgr, "put_back_queue", capture_queue)
    monkeypatch.setattr(Metric, "upload", skip_metrics)
    incoming = AstrBotMessage()
    incoming.type = MessageType.FRIEND_MESSAGE
    incoming.self_id = "bot"
    incoming.session_id = "session"
    incoming.message_id = "message"
    incoming.sender = MessageMember("user", "Reviewer")
    incoming.message = []
    incoming.message_str = "/点歌 晴天"
    incoming.raw_message = {}
    event = WebChatMessageEvent(
        incoming.message_str,
        incoming,
        PlatformMetadata(name="webchat", description="Test", id="custom-platform-id"),
        incoming.session_id,
    )
    config = RuntimeConfig.from_mapping(
        {"card_type": card_type, "card_attach_audio": attach_audio}
    )
    song = SongInfo(id="186016", name="晴天", artists=["周杰伦"], url=SONG_URL)
    transport = RecordingTransport()
    card = await build_card_result(
        song,
        config,
        provider=NeteaseProvider(config),
        transport=transport,
        platform_name=event.get_platform_name(),
    )
    assert transport.requests == []
    assert card.requested_type == card_type
    assert card.card_type == "text"
    assert card.degraded
    assert len(card.components) == 1
    assert isinstance(card.components[0], Plain)
    assert card.text_payload == ""

    await event.send(MessageChain(card.components))
    assert [payload["type"] for payload in queued] == ["plain"]
    assert "晴天" in queued[0]["data"]
    assert SONG_URL in queued[0]["data"]

    queued.clear()
    await WebChatMessageEvent._send(
        incoming.message_id,
        MessageChain(card.components),
        session_id=incoming.session_id,
        emit_complete=True,
    )
    assert [payload["type"] for payload in queued] == ["plain", "complete"]
    assert SONG_URL in queued[-1]["data"]


@pytest.mark.parametrize("platform_name", [None, "", "aiocqhttp"])
@pytest.mark.parametrize("card_type", ["163", "custom", "share"])
async def test_real_onebot_and_unspecified_platform_preserve_native_cards(
    platform_name: str | None, card_type: str
) -> None:
    require_astrbot()
    from astrbot.api.event import MessageChain
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    config = RuntimeConfig.from_mapping({"card_type": card_type})
    card = build_card_result_from_payload(
        {"id": "186016", "title": "晴天", "url": SONG_URL, "audio": AUDIO_URL},
        config,
        platform_name=platform_name,
    )
    sent = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(card.components))
    assert not card.degraded
    assert len(sent) == 1
    assert sent[0]["type"] == ("share" if card_type == "share" else "music")
    if card_type != "share":
        assert sent[0]["data"]["type"] == card_type


@pytest.mark.parametrize("platform_name", ["qq_official", "telegram", "discord", "custom_aiocqhttp"])
def test_only_registered_onebot_adapter_name_keeps_music(platform_name: str) -> None:
    require_astrbot()
    from astrbot.api.message_components import Plain

    card = build_card_result_from_payload(
        {"id": "186016", "title": "晴天", "url": SONG_URL},
        platform_name=platform_name,
    )
    assert len(card.components) == 1
    assert isinstance(card.components[0], Plain)
    assert SONG_URL in card.components[0].text
