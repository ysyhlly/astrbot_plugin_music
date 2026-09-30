"""数据模型与 Provider 契约：构造、默认值、容错解析与注册表行为。"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any

import pytest

from core.models import (
    CommentItem,
    CommentPage,
    Lyric,
    LyricLine,
    MusicQuery,
    SongInfo,
    format_duration,
)
from core.provider import (
    PROVIDER_REGISTRY,
    MusicProvider,
    Transport,
    available_providers,
    get_provider,
    register_provider,
)

# --------------------------------------------------------------------- 模型


def test_song_info_field_names_are_frozen() -> None:
    names = [f.name for f in dataclasses.fields(SongInfo)]
    assert names == [
        "id",
        "name",
        "artists",
        "album",
        "duration_ms",
        "cover_url",
        "source",
        "url",
        "audio_url",
        "provider_key",
    ]


def test_song_info_construction_and_defaults() -> None:
    song = SongInfo(
        id="123",
        name="测试歌曲",
        artists=["歌手甲", "歌手乙"],
        album="专辑",
        duration_ms=225000,
        cover_url="https://example.com/cover.jpg",
        source="netease",
        url="https://music.163.com/song?id=123",
        audio_url="https://example.com/song.mp3",
        provider_key="netease",
    )
    assert song.id == "123"
    assert song.artist_text == "歌手甲 / 歌手乙"
    assert song.duration_text == "03:45"
    assert song.display_name == "测试歌曲 - 歌手甲 / 歌手乙"
    assert song.is_empty() is False
    assert SongInfo().is_empty() is True
    assert SongInfo(id=1, name="x").id == "1"  # 容错转换
    assert SongInfo(name="x").artists == []
    assert SongInfo(name="x").provider_key == "netease"


def test_song_info_to_dict_contains_template_helpers() -> None:
    data = SongInfo(id="1", name="n", artists=["a"]).to_dict()
    assert data["artists"] == ["a"]
    assert data["artist_text"] == "a"
    assert data["duration_text"] == "--:--"
    assert data["display_name"] == "n - a"


def test_song_info_from_mapping_handles_netease_shape() -> None:
    song = SongInfo.from_mapping(
        {
            "id": 347230,
            "name": "海阔天空",
            "ar": [{"name": "Beyond"}],
            "al": {"name": "乐与怒", "picUrl": "https://example.com/p.jpg"},
            "dt": 326000,
        }
    )
    assert song.id == "347230"
    assert song.name == "海阔天空"
    assert song.artists == ["Beyond"]
    assert song.album == "乐与怒"
    assert song.duration_ms == 326000
    assert song.cover_url == "https://example.com/p.jpg"
    assert song.provider_key == "netease"


def test_song_info_from_mapping_is_total() -> None:
    assert SongInfo.from_mapping(None).name == ""
    assert SongInfo.from_mapping("nope").name == ""
    assert SongInfo.from_mapping({}).duration_ms == 0


def test_format_duration() -> None:
    assert format_duration(0) == "--:--"
    assert format_duration(-5) == "--:--"
    assert format_duration(1000) == "00:01"
    assert format_duration(60000) == "01:00"
    assert format_duration(3661000) == "1:01:01"
    assert format_duration("bad") == "--:--"


def test_lyric_line_basics() -> None:
    line = LyricLine(text="歌词", translation="lyrics")
    assert line.text == "歌词"
    assert line.translation == "lyrics"
    assert line.is_empty() is False
    assert LyricLine().is_empty() is True
    assert LyricLine(text="   ").is_empty() is True
    assert LyricLine.from_mapping("只有原文").text == "只有原文"
    assert LyricLine.from_mapping({"text": "a", "translation": "b"}).to_dict() == {
        "text": "a",
        "translation": "b",
    }


def test_lyric_is_empty_matrix() -> None:
    assert Lyric().is_empty() is True
    assert Lyric(text="   ").is_empty() is True
    assert Lyric(text="原文").is_empty() is False
    assert Lyric(translated="translation only").is_empty() is False
    assert Lyric(lines=[LyricLine()]).is_empty() is True
    assert Lyric(lines=[LyricLine(text="x")]).is_empty() is False
    assert Lyric(lines=[LyricLine(translation="t")]).is_empty() is False


def test_lyric_lines_are_normalised() -> None:
    lyric = Lyric(
        lines=[LyricLine(text="a"), {"text": "b", "translation": "tb"}, "c"],  # type: ignore[list-item]
    )
    assert [line.text for line in lyric.lines] == ["a", "b", "c"]
    assert lyric.line_count() == 3
    assert lyric.plain_text() == "\n".join(["a", "b", "c"])


def test_lyric_plain_text_prefers_raw_text() -> None:
    assert Lyric(text="原文", lines=[LyricLine(text="a")]).plain_text() == "原文"


def test_lyric_to_dict_exposes_flags() -> None:
    data = Lyric(text="a", translated="ta", lines=[LyricLine(text="a", translation="ta")]).to_dict()
    assert data["has_translation"] is True
    assert data["line_count"] == 1
    assert data["lines"] == [{"text": "a", "translation": "ta"}]


def test_lyric_from_mapping_supports_netease_keys() -> None:
    lyric = Lyric.from_mapping(
        {
            "lyric": "原文",
            "tlyric": "翻译",
            "lines": [{"text": "a"}, {"text": "b", "translation": "tb"}],
        }
    )
    assert lyric.text == "原文"
    assert lyric.translated == "翻译"
    assert lyric.line_count() == 2
    assert Lyric.from_mapping(None).is_empty() is True


def test_comment_item_field_names_are_frozen() -> None:
    names = [f.name for f in dataclasses.fields(CommentItem)]
    assert names == ["user", "content", "liked", "avatar_url", "time", "replies"]


def test_comment_item_construction() -> None:
    reply = CommentItem(user="乙", content="回复")
    item = CommentItem(
        user="甲",
        content="好听",
        liked=1234,
        avatar_url="https://example.com/a.jpg",
        time="2024-01-01",
        replies=[reply],
    )
    assert item.liked == 1234
    assert item.replies[0].user == "乙"
    assert item.is_empty() is False
    assert CommentItem().is_empty() is True
    assert item.to_dict()["replies"][0]["content"] == "回复"


def test_comment_item_tolerates_api_shapes() -> None:
    item = CommentItem.from_mapping(
        {
            "user": {"nickname": "甲", "avatarUrl": "https://example.com/a.jpg"},
            "content": "好听",
            "likedCount": "1234",
            "timeStr": "刚刚",
            "beReplied": [{"user": {"nickname": "乙"}, "content": "同感"}],
        }
    )
    assert item.user == "甲"
    assert item.avatar_url == "https://example.com/a.jpg"
    assert item.liked == 1234
    assert item.time == "刚刚"
    assert [r.user for r in item.replies] == ["乙"]
    assert CommentItem.from_mapping("nope").is_empty() is True


def test_comment_page_construction_and_defaults() -> None:
    page = CommentPage(
        items=[CommentItem(user="甲", content="c1"), CommentItem(user="乙", content="c2")],
        total=2,
        has_more=True,
    )
    assert len(page.items) == 2
    assert page.total == 2
    assert page.has_more is True
    assert page.is_empty() is False
    assert CommentPage().is_empty() is True
    assert CommentPage().has_more is False


def test_comment_page_from_mapping_and_to_dict() -> None:
    page = CommentPage.from_mapping(
        {
            "hotComments": [{"user": {"nickname": "甲"}, "content": "c", "likedCount": 3}],
            "total": "12",
            "more": True,
        }
    )
    assert len(page.items) == 1
    assert page.total == 12
    assert page.has_more is True
    data = page.to_dict()
    assert data["items"][0]["liked"] == 3
    assert data["total"] == 12
    assert data["has_more"] is True
    assert CommentPage.from_mapping(None).items == []


def test_comment_page_normalises_item_inputs() -> None:
    page = CommentPage(items=["x", {"user": "甲", "content": "c", "liked": 1}])  # type: ignore[list-item]
    assert page.items[0].is_empty() is True
    assert page.items[1].liked == 1


def test_music_query_field_names_are_frozen() -> None:
    names = [f.name for f in dataclasses.fields(MusicQuery)]
    assert names == ["keyword", "artist", "limit"]


def test_music_query_basics() -> None:
    query = MusicQuery(keyword="海阔天空", artist="Beyond", limit=3)
    assert query.search_text == "海阔天空 Beyond"
    assert query.is_empty() is False
    assert MusicQuery().limit == 5
    assert MusicQuery().is_empty() is True
    assert MusicQuery(limit=0).limit == 1  # 非法 limit 修正
    assert query.to_dict()["search_text"] == "海阔天空 Beyond"


def test_music_query_from_text() -> None:
    assert MusicQuery.from_text("海阔天空") == MusicQuery(keyword="海阔天空")
    assert MusicQuery.from_text("海阔天空 Beyond") == MusicQuery(
        keyword="海阔天空", artist="Beyond"
    )
    assert MusicQuery.from_text("海阔天空 - Beyond") == MusicQuery(
        keyword="海阔天空", artist="Beyond"
    )
    assert MusicQuery.from_text("  ") == MusicQuery()
    assert MusicQuery.from_text("晴天", limit=2).limit == 2
    assert MusicQuery.from_text("晴天", artist="周杰伦") == MusicQuery(
        keyword="晴天", artist="周杰伦"
    )


def test_all_models_are_dataclasses_and_never_raise() -> None:
    for cls in (LyricLine, Lyric, CommentItem, CommentPage, SongInfo, MusicQuery):
        assert dataclasses.is_dataclass(cls)
        instance = cls()
        assert isinstance(instance.to_dict(), dict)


# --------------------------------------------------------- Provider 契约


def test_provider_registry_holds_provider_instances() -> None:
    assert isinstance(PROVIDER_REGISTRY, dict)
    for key, provider in PROVIDER_REGISTRY.items():
        assert isinstance(key, str)
        assert provider.key == key


def test_get_provider_returns_none_for_unknown_keys() -> None:
    """未知 / 空 key 一律返回 None，而不是抛 KeyError 或 ImportError。"""
    assert get_provider(None) is None
    assert get_provider("") is None
    assert get_provider("not-a-provider") is None


def test_get_provider_resolves_netease_when_available() -> None:
    """core/netease 存在时，get_provider("netease") 必须解析出可用的 provider。"""
    import importlib.util

    from core import provider as provider_module

    module_name = f"{provider_module._PACKAGE}.{provider_module._LAZY_PROVIDERS['netease'][0]}"
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError):
        spec = None
    if spec is None:
        pytest.skip(f"{module_name} 尚不存在，跳过正向用例。")

    provider = get_provider("netease")
    assert provider is not None
    assert provider.key == "netease"
    assert isinstance(provider, MusicProvider)
    # 解析成功后会被缓存进注册表，后续调用不再重复导入
    assert PROVIDER_REGISTRY.get("netease") is provider


def test_get_provider_degrades_gracefully_when_module_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约本体：惰性 import 失败时必须返回 None（绝不抛 ImportError）。

    这里用 sys.modules 屏蔽 netease 模块来模拟「core/netease 不存在」，
    因此该用例不受 t9 是否落地影响。
    """
    from core import provider as provider_module

    # 私有常量来自 core/provider.py 自身的惰性表，模块改名时本用例会显式失败
    module_name = (
        f"{provider_module._PACKAGE}.{provider_module._LAZY_PROVIDERS['netease'][0]}"
    )
    package_name = module_name.rsplit(".", 1)[0]
    monkeypatch.delitem(PROVIDER_REGISTRY, "netease", raising=False)
    monkeypatch.setitem(sys.modules, module_name, None)
    monkeypatch.setitem(sys.modules, package_name, None)

    assert get_provider("netease") is None


def test_register_provider_validates_and_indexes() -> None:
    class DummyProvider:
        key = "dummy"
        display_name = "Dummy"

        async def search(self, query: MusicQuery, transport: Any) -> list[SongInfo]:
            return []

        async def lyrics(self, song: SongInfo, transport: Any) -> Lyric | None:
            return None

        async def comments(
            self, song: SongInfo, transport: Any, limit: int = 20, offset: int = 0
        ) -> CommentPage | None:
            return None

        def card_payload(self, song: SongInfo) -> dict[str, Any]:
            return {"kind": "share", "url": song.url}

    provider = DummyProvider()
    PROVIDER_REGISTRY.pop("dummy", None)
    try:
        assert register_provider(provider) is provider
        assert get_provider("dummy") is provider
        assert get_provider("DUMMY") is provider  # key 大小写不敏感
        assert "dummy" in available_providers()
        assert register_provider(object()) is None  # 非法对象被拒绝
    finally:
        PROVIDER_REGISTRY.pop("dummy", None)


def test_music_provider_protocol_is_runtime_checkable() -> None:
    class FakeProvider:
        key = "fake"
        display_name = "Fake"

        async def search(self, query: MusicQuery, transport: Any) -> list[SongInfo]:
            return []

        async def lyrics(self, song: SongInfo, transport: Any) -> Lyric | None:
            return None

        async def comments(
            self, song: SongInfo, transport: Any, limit: int = 20, offset: int = 0
        ) -> CommentPage | None:
            return None

        def card_payload(self, song: SongInfo) -> dict[str, Any]:
            return {}

    assert isinstance(FakeProvider(), MusicProvider)
    assert not isinstance(object(), MusicProvider)


@pytest.mark.asyncio
async def test_fake_provider_can_be_used_through_the_protocol() -> None:
    class FakeTransport:
        def get(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - 协议桩
            raise NotImplementedError

        def post(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - 协议桩
            raise NotImplementedError

        def request(self, method: str, url: str, **kwargs: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    transport = FakeTransport()
    assert isinstance(transport, Transport)

    class FakeProvider:
        key = "fake2"
        display_name = "Fake2"

        async def search(self, query: MusicQuery, transport: Any) -> list[SongInfo]:
            return [SongInfo(id="1", name=query.keyword)]

        async def lyrics(self, song: SongInfo, transport: Any) -> Lyric | None:
            return Lyric(text="la")

        async def comments(
            self, song: SongInfo, transport: Any, limit: int = 20, offset: int = 0
        ) -> CommentPage | None:
            return CommentPage(items=[], total=0)

        def card_payload(self, song: SongInfo) -> dict[str, Any]:
            return {"kind": "music", "type": "163", "id": song.id}

    provider = FakeProvider()
    songs = await provider.search(MusicQuery(keyword="x"), transport)
    assert songs[0].name == "x"
    assert (await provider.lyrics(songs[0], transport)).text == "la"
    assert (await provider.comments(songs[0], transport, 10, 0)).is_empty() is True
    assert provider.card_payload(songs[0])["id"] == "1"
