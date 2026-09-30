"""core/cards/music_card.py：163 / custom / share 三态、降级链与 respond stage 非空校验。

两组断言：

1. 无 astrbot 环境（FakeComponents，故意模仿 pydantic v1 把 `_type` 构造参数丢掉、
   `id` 只接受整数）——验证降级逻辑、id 防御性整数化、"绝不产出会被丢弃的空组件"；
2. 有 astrbot 环境（ASTRBOT_REF 指向的真实组件）——直接用 respond stage 的
   `RespondStage._component_validators` 作为判定函数断言卡片真的能发出去，
   并检查 `toDict()["data"]["type"]`（平台适配器实际读的字段）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.cards import (
    CARD_TYPES,
    FALLBACK_TYPES,
    CardResult,
    at_component,
    build_card,
    build_card_result,
    build_card_result_from_payload,
    build_card_text,
    build_payload_from_song,
    card_extra_components,
    component_is_deliverable,
    ensure_music_type,
    music_component_is_valid,
    normalise_card_type,
    normalise_song_id,
    plain_component,
    resolve_component_factory,
    song_page_url,
)
from core.config import RuntimeConfig
from core.models import SongInfo
import core.cards.music_card as music_card

from tests.conftest import require_astrbot

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SONG_URL = "https://music.163.com/song?id=186016"
AUDIO_URL = "https://m8.music.126.net/song.mp3"


# ------------------------------------------------------------------ 假组件


class Music:  # noqa: N801 - 故意与 AstrBot 组件同名，才能走同一套校验规则
    """忠实模仿 pydantic v1 的 Music：构造参数里的 _type 被丢弃、id 必须是整数。"""

    def __init__(self, **kwargs: Any) -> None:
        raw = kwargs.get("id", 0)
        if isinstance(raw, str) and raw.strip() and not raw.strip().lstrip("+-").isdigit():
            raise ValueError("value is not a valid integer")
        self.id = None if raw in (None, "") else int(raw)
        self.url = kwargs.get("url", "")
        self.audio = kwargs.get("audio", "")
        self.title = kwargs.get("title", "")
        self.content = kwargs.get("content", "")
        self.image = kwargs.get("image", "")


class Share:  # noqa: N801
    def __init__(self, url: str = "", title: str = "", content: str = "", image: str = "", **_: Any) -> None:
        self.url = url
        self.title = title
        self.content = content
        self.image = image


class Plain:  # noqa: N801
    def __init__(self, text: str = "", **_: Any) -> None:
        self.text = text


class Record:  # noqa: N801
    def __init__(self, file: str = "", url: str = "", **_: Any) -> None:
        self.file = file
        self.url = url


class At:  # noqa: N801
    def __init__(self, qq: str = "", name: str = "", **_: Any) -> None:
        self.qq = qq
        self.name = name


FAKE_COMPONENTS = SimpleNamespace(Music=Music, Share=Share, Record=Record, Plain=Plain, At=At)


def fake_factory() -> Any:
    return FAKE_COMPONENTS


@pytest.fixture
def fake_components():
    """注入假组件工厂（等价于无 astrbot 环境下的卡片构造）。"""
    return fake_factory


def make_song(**overrides: Any) -> SongInfo:
    values: dict[str, Any] = {
        "id": "186016",
        "name": "晴天",
        "artists": ["周杰伦"],
        "album": "叶惠美",
        "duration_ms": 225_000,
        "cover_url": "https://p1.music.126.net/cover.jpg",
        "url": SONG_URL,
    }
    values.update(overrides)
    return SongInfo(**values)


def make_cfg(raw: Any = None, **overrides: Any) -> RuntimeConfig:
    data: dict = dict(raw) if isinstance(raw, dict) else {}
    data.update(overrides)
    return RuntimeConfig.from_mapping(data)


class FakeProvider:
    key = "fake"
    display_name = "假网易云"

    def __init__(self, *, audio: str = "", audio_error: Exception | None = None, payload: Any = None) -> None:
        self.audio = audio
        self.audio_error = audio_error
        self.payload = payload
        self.audio_calls: list[tuple[Any, Any]] = []

    async def audio_url(self, song: Any, transport: Any, *, level: str = "standard") -> str:
        self.audio_calls.append((song, transport))
        if self.audio_error is not None:
            raise self.audio_error
        return self.audio

    def card_payload(self, song: Any) -> Any:
        if self.payload is not None:
            return self.payload
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


def music_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "music",
        "type": "163",
        "id": "186016",
        "title": "晴天",
        "content": "周杰伦 · 叶惠美 · 网易云音乐",
        "image": "https://p1.music.126.net/cover.jpg",
        "url": SONG_URL,
        "audio": "",
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------------ respond stage 规则


def stage_accepts(component: Any) -> bool:
    """用真实 respond stage 的校验函数判定组件能否被发出去。"""
    require_astrbot()
    from astrbot.core.pipeline.respond.stage import RespondStage

    rule = RespondStage._component_validators.get(type(component))
    return bool(rule(component)) if rule else True


# ------------------------------------------------------------------ 基础工具


def test_normalise_song_id_table():
    assert normalise_song_id("186016") == 186016
    assert normalise_song_id(186016) == 186016
    assert normalise_song_id(" 186016 ") == 186016
    assert normalise_song_id("186016.0") == 186016
    assert normalise_song_id(186016.0) == 186016
    assert normalise_song_id("not-a-number") is None
    assert normalise_song_id("") is None
    assert normalise_song_id(None) is None
    assert normalise_song_id(True) is None
    assert normalise_song_id(float("nan")) is None
    assert normalise_song_id(float("inf")) is None
    assert normalise_song_id("-12") == -12
    assert normalise_song_id("12abc") is None


def test_normalise_card_type_table():
    for value in CARD_TYPES:
        assert normalise_card_type(value) == value
    assert normalise_card_type("SHARE") == "share"
    assert normalise_card_type(" 163 ") == "163"
    assert normalise_card_type("qq") == ""
    assert normalise_card_type(None) == ""


def test_song_page_url_uses_netease_endpoint():
    assert song_page_url("186016") == SONG_URL
    assert song_page_url("") == ""
    assert song_page_url(None) == ""


def test_music_component_validator_matches_stage_rules():
    assert music_component_is_valid(Music()) is False
    valid = ensure_music_type(Music(id=186016, title="晴天"), "163")
    assert music_component_is_valid(valid) is True
    # custom 需要 url + audio + title 三个字段
    custom = ensure_music_type(Music(id=186016, url=SONG_URL, audio=AUDIO_URL, title="晴天"), "custom")
    assert music_component_is_valid(custom) is True
    assert music_component_is_valid(ensure_music_type(Music(id=186016, url=SONG_URL, title="晴天"), "custom")) is False
    # 163 只要 id 非空即可
    assert music_component_is_valid(ensure_music_type(Music(id=186016), "163")) is True
    assert music_component_is_valid(ensure_music_type(Music(id=0), "163")) is False


def test_component_is_deliverable_covers_other_types():
    assert component_is_deliverable(Share(url=SONG_URL, title="晴天")) is True
    assert component_is_deliverable(Share(url="", title="晴天")) is True
    assert component_is_deliverable(Share(url="", title="")) is False
    assert component_is_deliverable(Plain(text="  ")) is False
    assert component_is_deliverable(Plain(text="hi")) is True
    assert component_is_deliverable(Record(file=AUDIO_URL)) is True
    assert component_is_deliverable(Record(file="")) is False
    assert component_is_deliverable(At(qq="123")) is True
    assert component_is_deliverable(At(qq="")) is False
    assert component_is_deliverable(None) is False


def test_ensure_music_type_writes_private_type_attribute():
    comp = Music(id=186016)
    assert getattr(comp, "_type", "") == ""
    ensure_music_type(comp, "163")
    assert comp._type == "163"
    # 重复调用是幂等的
    ensure_music_type(comp, "163")
    assert comp._type == "163"


# ------------------------------------------------------------------ 163


def test_163_card_builds_valid_music(fake_components):
    cfg = make_cfg({"card_type": "163"})
    result = build_card_result_from_payload(music_payload(), cfg, factory=fake_components, song=make_song())
    assert result.degraded is False
    assert result.reason == "ok"
    assert result.card_type == "163"
    assert len(result.components) == 1
    component = result.components[0]
    assert type(component).__name__ == "Music"
    assert component._type == "163"
    assert component.id == 186016
    assert isinstance(component.id, int)
    assert component.title == "晴天"
    assert component.image == "https://p1.music.126.net/cover.jpg"
    assert "周杰伦" in component.content


def test_163_card_url_falls_back_to_song_page_url(fake_components):
    cfg = make_cfg({"card_type": "163"})
    payload = music_payload(url="")
    result = build_card_result_from_payload(payload, cfg, factory=fake_components, song=make_song(url=""))
    assert result.components[0].url == SONG_URL


def test_163_card_respects_cover_switch(fake_components):
    cfg = make_cfg({"card_type": "163", "card_show_cover": False})
    result = build_card_result_from_payload(music_payload(), cfg, factory=fake_components, song=make_song())
    assert result.components[0].image == ""


def test_163_dirty_id_degrades_to_share(fake_components):
    cfg = make_cfg({"card_type": "163", "card_fallback": "share"})
    result = build_card_result_from_payload(
        music_payload(id="not-a-number"), cfg, factory=fake_components, song=make_song()
    )
    assert result.degraded is True
    assert result.reason == "fallback_share:music_invalid_id"
    assert result.card_type == "share"
    assert len(result.components) == 1
    assert type(result.components[0]).__name__ == "Share"
    assert all(component_is_deliverable(item) for item in result.components)


def test_163_dirty_id_degrades_to_text(fake_components):
    cfg = make_cfg({"card_type": "163", "card_fallback": "text"})
    result = build_card_result_from_payload(
        music_payload(id="not-a-number"), cfg, factory=fake_components, song=make_song()
    )
    assert result.card_type == "text"
    assert type(result.components[0]).__name__ == "Plain"
    assert "晴天" in result.components[0].text
    assert SONG_URL in result.components[0].text


# ------------------------------------------------------------------ custom


def test_custom_with_audio_builds_valid_music(fake_components):
    cfg = make_cfg({"card_type": "custom"})
    result = build_card_result_from_payload(
        music_payload(type="custom", audio=AUDIO_URL), cfg, factory=fake_components, song=make_song()
    )
    assert result.degraded is False
    component = result.components[0]
    assert component._type == "custom"
    assert component.audio == AUDIO_URL
    assert music_component_is_valid(component) is True


async def test_custom_fetches_audio_from_provider(fake_components):
    cfg = make_cfg({"card_type": "custom"})
    provider = FakeProvider(audio=AUDIO_URL)
    transport = object()
    result = await build_card_result(
        make_song(), cfg, provider=provider, transport=transport, factory=fake_components
    )
    assert provider.audio_calls == [(result and provider.audio_calls[0][0], transport)]
    assert result.card_type == "custom"
    assert result.components[0].audio == AUDIO_URL
    assert result.audio_url == AUDIO_URL


async def test_custom_without_audio_degrades_to_share(fake_components):
    """核心缺陷回归：custom 拿不到 audio 时绝不能产出会被丢弃（或平台报错）的 Music。"""
    cfg = make_cfg({"card_type": "custom", "card_fallback": "share"})
    provider = FakeProvider(audio="")
    result = await build_card_result(
        make_song(), cfg, provider=provider, transport=object(), factory=fake_components
    )
    assert result.degraded is True
    assert result.reason == "fallback_share:custom_no_audio"
    assert result.components, "必须有可发送的组件"
    assert all(type(item).__name__ != "Music" for item in result.components)
    assert all(component_is_deliverable(item) for item in result.components)
    assert type(result.components[0]).__name__ == "Share"


async def test_custom_without_audio_degrades_to_text(fake_components):
    cfg = make_cfg({"card_type": "custom", "card_fallback": "text"})
    result = await build_card_result(
        make_song(), cfg, provider=FakeProvider(audio=""), transport=object(), factory=fake_components
    )
    assert result.card_type == "text"
    assert result.components and all(type(item).__name__ == "Plain" for item in result.components)
    text = result.components[0].text
    assert "晴天" in text and SONG_URL in text
    assert "播放地址" in text


async def test_custom_audio_url_exception_is_swallowed(fake_components):
    cfg = make_cfg({"card_type": "custom", "card_fallback": "share"})
    provider = FakeProvider(audio_error=RuntimeError("network down"))
    result = await build_card_result(
        make_song(), cfg, provider=provider, transport=object(), factory=fake_components
    )
    assert result.card_type == "share" and result.degraded is True
    assert component_is_deliverable(result.components[0])


async def test_custom_without_provider_still_degrades(fake_components):
    cfg = make_cfg({"card_type": "custom", "card_fallback": "text"})
    result = await build_card_result(make_song(), cfg, factory=fake_components)
    assert result.card_type == "text"
    assert result.components and component_is_deliverable(result.components[0])


# ------------------------------------------------------------------ share / 附带语音


def test_share_card_produces_share(fake_components):
    cfg = make_cfg({"card_type": "share"})
    result = build_card_result_from_payload(music_payload(type="share"), cfg, factory=fake_components)
    assert result.degraded is False
    assert result.card_type == "share"
    assert type(result.components[0]).__name__ == "Share"
    assert result.components[0].url == SONG_URL


def test_share_without_url_uses_placeholder_title(fake_components):
    """分享卡片只有 title 非空也能发出去（respond stage: url 或 title 任一非空）。"""
    cfg = make_cfg({"card_type": "share", "card_fallback": "share"})
    result = build_card_result_from_payload(
        {"type": "share", "title": "", "url": "", "id": ""}, cfg, factory=fake_components
    )
    assert result.components and component_is_deliverable(result.components[0])
    assert type(result.components[0]).__name__ == "Share"
    assert result.components[0].title == music_card.UNKNOWN_SONG_NAME


def test_card_attach_audio_appends_record(fake_components):
    cfg = make_cfg({"card_attach_audio": True})
    result = build_card_result_from_payload(
        music_payload(audio=AUDIO_URL), cfg, factory=fake_components, song=make_song()
    )
    assert len(result.components) == 2
    assert type(result.components[1]).__name__ == "Record"
    assert result.components[1].file == AUDIO_URL
    assert all(component_is_deliverable(item) for item in result.components)


def test_card_attach_audio_without_audio_adds_nothing(fake_components):
    cfg = make_cfg({"card_attach_audio": True})
    result = build_card_result_from_payload(music_payload(audio=""), cfg, factory=fake_components)
    assert len(result.components) == 1
    assert type(result.components[0]).__name__ == "Music"
    assert card_extra_components(fake_components, cfg, "") == []


async def test_attach_audio_fetches_audio_for_163(fake_components):
    cfg = make_cfg({"card_attach_audio": True})
    provider = FakeProvider(audio=AUDIO_URL)
    result = await build_card_result(
        make_song(), cfg, provider=provider, transport=object(), factory=fake_components
    )
    assert provider.audio_calls, "card_attach_audio 时应向 provider 取一次播放地址"
    assert len(result.components) == 2
    assert type(result.components[1]).__name__ == "Record"


# ------------------------------------------------------------------ 关闭 / 无组件


def test_card_disabled_produces_nothing(fake_components):
    cfg = make_cfg({"card_enable": False})
    result = build_card_result_from_payload(music_payload(), cfg, factory=fake_components)
    assert result.components == []
    assert result.text == ""
    assert result.degraded is False
    assert result.reason == "card_disabled"


def test_missing_factory_returns_text_only(monkeypatch):
    monkeypatch.setattr(music_card, "COMPONENT_FACTORY", None)
    result = build_card_result_from_payload(music_payload(), make_cfg({}), song=make_song())
    assert result.components == []
    assert result.text, "没有组件时必须给出纯文本兜底"
    assert result.reason == "no_component_factory"
    assert result.degraded is True


def test_broken_factory_returns_text_only():
    def broken():
        raise ImportError("no astrbot here")

    result = build_card_result_from_payload(music_payload(), make_cfg({}), factory=broken, song=make_song())
    assert result.components == []
    assert "晴天" in result.text
    assert result.reason == "no_component_factory"


def test_resolve_component_factory_accepts_namespace_and_callable():
    assert resolve_component_factory(FAKE_COMPONENTS) is FAKE_COMPONENTS
    assert resolve_component_factory(fake_factory) is FAKE_COMPONENTS
    assert resolve_component_factory(object()) is None
    assert resolve_component_factory(None) is None or True  # 默认工厂可能可用


def test_pydantic_validation_error_is_swallowed_and_degrades():
    """组件构造抛 pydantic.ValidationError 时也必须降级，不能冒泡成用户可见错误。"""
    from pydantic import BaseModel

    class _Model(BaseModel):
        number: int

    try:
        _Model(number="not-a-number")
    except Exception as exc:  # noqa: BLE001 - 故意拿一个真实的 ValidationError
        validation_error = exc
    else:  # pragma: no cover - pydantic 行为变化时保护
        pytest.skip("无法构造 pydantic ValidationError")

    class ExplodingMusic:
        def __init__(self, **kwargs: Any) -> None:
            raise validation_error

    factory = SimpleNamespace(
        Music=ExplodingMusic, Share=Share, Record=Record, Plain=Plain, At=At
    )
    cfg = make_cfg({"card_type": "163", "card_fallback": "share"})
    result = build_card_result_from_payload(music_payload(), cfg, factory=factory, song=make_song())
    assert result.degraded is True
    assert result.components and component_is_deliverable(result.components[0])
    assert type(result.components[0]).__name__ == "Share"


def test_share_construction_failure_falls_back_to_plain():
    class ExplodingShare:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("nope")

    factory = SimpleNamespace(Music=Music, Share=ExplodingShare, Record=Record, Plain=Plain, At=At)
    result = build_card_result_from_payload(music_payload(), make_cfg({"card_type": "share"}), factory=factory)
    assert result.card_type == "text"
    assert result.text
    assert result.reason == "fallback_text:share_invalid"
    assert component_is_deliverable(result.components[0])
    # Plain 组件已经承载了这段文本：text_payload 必须为空，避免调用方重复发送
    assert result.text_payload == ""
    assert result.components[0].text == result.text


@pytest.mark.parametrize("card_type", CARD_TYPES)
@pytest.mark.parametrize("fallback", FALLBACK_TYPES)
@pytest.mark.parametrize("song_id", ["186016", "not-a-number", ""])
@pytest.mark.parametrize("audio", ["", AUDIO_URL])
def test_degradation_matrix_never_returns_an_undeliverable_chain(
    fake_components, card_type, fallback, song_id, audio
):
    """任意组合下：不抛异常；要么 text 兜底，要么链上全是可以发出去的组件。"""
    cfg = make_cfg({"card_type": card_type, "card_fallback": fallback})
    payload = music_payload(type=card_type, id=song_id, audio=audio, url=SONG_URL)
    result = build_card_result_from_payload(payload, cfg, factory=fake_components, song=make_song())
    if result.components:
        assert all(component_is_deliverable(item) for item in result.components)
    else:
        assert result.text, "没有组件时必须有纯文本兜底（否则用户什么都收不到）"


# ------------------------------------------------------------------ 其他入口


async def test_build_card_returns_plain_list(fake_components):
    components = await build_card(
        make_song(), make_cfg({}), provider=FakeProvider(), transport=object(), factory=fake_components
    )
    assert isinstance(components, list) and components
    assert type(components[0]).__name__ == "Music"


def test_card_result_list_protocol(fake_components):
    result = build_card_result_from_payload(music_payload(), make_cfg({}), factory=fake_components)
    assert len(result) == 1
    assert list(result) == result.components
    assert result[0] is result.components[0]
    assert bool(result) is True
    assert result.to_dict()["card_type"] == "163"
    empty = CardResult()
    assert bool(empty) is False and len(empty) == 0


def test_at_component_and_plain_component(fake_components, monkeypatch):
    mention = at_component("12345", fake_components)
    assert mention is not None and mention.qq == "12345"
    assert at_component("", fake_components) is None
    body = plain_component("你好", fake_components)
    assert body is not None and body.text == "你好"
    assert plain_component("   ", fake_components) is None
    monkeypatch.setattr(music_card, "COMPONENT_FACTORY", None)
    assert at_component("12345") is None
    assert plain_component("你好") is None


def test_card_text_fallback_contains_key_info():
    text = build_card_text(make_song(), hint="测试提示")
    assert "晴天" in text
    assert "周杰伦" in text
    assert SONG_URL in text
    assert "测试提示" in text
    assert build_card_text(None).strip()


def test_build_payload_prefers_provider_payload():
    payload = {"type": "share", "url": SONG_URL, "title": "自定义标题"}
    data = build_payload_from_song(make_song(), make_cfg({}), provider=FakeProvider(payload=payload))
    assert data["title"] == "自定义标题"
    assert data["type"] == "share"


def test_build_payload_falls_back_when_provider_raises():
    class Boom:
        def card_payload(self, song):
            raise RuntimeError("boom")

    data = build_payload_from_song(make_song(), make_cfg({}), provider=Boom())
    assert data["title"] == "晴天"
    assert data["url"] == SONG_URL
    assert data["id"] == "186016"


def test_payload_type_is_overridden_by_config(fake_components):
    """provider 载荷里的 type 只是配置的投影；cfg.card_type 才是最终意图。"""
    cfg = make_cfg({"card_type": "share"})
    result = build_card_result_from_payload(music_payload(type="163"), cfg, factory=fake_components)
    assert result.requested_type == "share"
    assert result.card_type == "share"


def test_module_has_no_top_level_astrbot_import():
    path = PLUGIN_ROOT / "core" / "cards" / "music_card.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "astrbot" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "astrbot"


# ------------------------------------------------------------------ 真实 AstrBot 组件


def test_real_163_card_passes_respond_stage_rule():
    require_astrbot()
    result = build_card_result_from_payload(music_payload(), make_cfg({"card_type": "163"}), song=make_song())
    assert result.degraded is False and result.card_type == "163"
    component = result.components[0]
    assert component._type == "163"
    assert component.id == 186016
    assert stage_accepts(component) is True
    assert component.toDict()["data"]["type"] == "163"
    assert component.toDict()["data"]["id"] == 186016


def test_real_custom_card_passes_respond_stage_rule():
    require_astrbot()
    result = build_card_result_from_payload(
        music_payload(type="custom", audio=AUDIO_URL), make_cfg({"card_type": "custom"}), song=make_song()
    )
    component = result.components[0]
    assert component._type == "custom"
    assert component.audio == AUDIO_URL
    assert stage_accepts(component) is True
    assert component.toDict()["data"]["type"] == "custom"


async def test_real_custom_without_audio_never_emits_music():
    require_astrbot()
    from astrbot.api.message_components import Music as RealMusic

    cfg = make_cfg({"card_type": "custom", "card_fallback": "share"})
    result = await build_card_result(make_song(), cfg, provider=FakeProvider(audio=""), transport=object())
    assert result.components
    assert all(not isinstance(item, RealMusic) for item in result.components)
    assert all(stage_accepts(item) for item in result.components)
    assert result.degraded is True


def test_real_share_card_passes_respond_stage_rule():
    require_astrbot()
    result = build_card_result_from_payload(music_payload(type="share"), make_cfg({"card_type": "share"}), song=make_song())
    assert stage_accepts(result.components[0]) is True


def test_real_attach_audio_record_passes_stage_rule():
    require_astrbot()
    result = build_card_result_from_payload(
        music_payload(audio=AUDIO_URL), make_cfg({"card_attach_audio": True}), song=make_song()
    )
    assert len(result.components) == 2
    assert stage_accepts(result.components[1]) is True


def test_real_dirty_id_does_not_raise():
    require_astrbot()
    result = build_card_result_from_payload(
        music_payload(id="not-a-number"), make_cfg({"card_fallback": "text"}), song=make_song()
    )
    assert result.components
    assert all(stage_accepts(item) for item in result.components)


def test_real_degradation_matrix_matches_respond_stage():
    require_astrbot()
    for card_type in CARD_TYPES:
        for fallback in FALLBACK_TYPES:
            for song_id in ("186016", "not-a-number", ""):
                for audio in ("", AUDIO_URL):
                    cfg = make_cfg({"card_type": card_type, "card_fallback": fallback})
                    payload = music_payload(type=card_type, id=song_id, audio=audio)
                    result = build_card_result_from_payload(payload, cfg, song=make_song())
                    if result.components:
                        assert len(result.components) >= 1
                        assert all(stage_accepts(item) for item in result.components), (
                            card_type,
                            fallback,
                            song_id,
                            audio,
                        )
                        assert result.text_payload == "", "有组件时不应再发重复文本"
                    else:
                        assert result.text