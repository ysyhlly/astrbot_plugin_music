"""core/t2i/plain.py：文本构造、人类可读格式化与按配置截断（不联网）。"""

from __future__ import annotations

import pytest

from core.config import RuntimeConfig
from core.models import (
    CommentItem,
    CommentPage,
    Lyric,
    LyricLine,
    SongInfo,
)
from core.t2i.plain import (
    EMPTY_LYRIC_TEXT,
    build_comments_text,
    build_lyrics_text,
    comment_entries,
    format_count,
    format_likes,
    lyric_lines,
    song_artist,
    song_duration,
    song_heading,
    song_meta,
    song_source,
    truncate_text,
)

TITLE = "晴天"
ARTIST = "周杰伦"
ALBUM = "叶惠美"


def make_song(**overrides: object) -> SongInfo:
    """默认歌曲：4 分 29 秒以内的 3:45，带封面与专辑。"""
    values: dict[str, object] = {
        "id": "186016",
        "name": TITLE,
        "artists": [ARTIST],
        "album": ALBUM,
        "duration_ms": 225_000,
        "cover_url": "https://p1.music.126.net/cover.jpg",
        "source": "netease",
        "provider_key": "netease",
        "url": "https://music.163.com/#/song?id=186016",
    }
    values.update(overrides)
    return SongInfo(**values)  # type: ignore[arg-type]


def make_lyric(count: int = 3, *, translation: bool = False) -> Lyric:
    """构造 count 行歌词（可选逐行翻译）。"""
    lines = [
        LyricLine(
            text=f"第{index}行歌词",
            translation=f"line {index}" if translation else "",
        )
        for index in range(1, count + 1)
    ]
    return Lyric(lines=lines)


def make_page(count: int = 3, *, liked: int = 0, replies: int = 0) -> CommentPage:
    """构造一页评论（第 i 条正文为 评论正文i）。"""
    items = [
        CommentItem(
            user=f"用户{index}",
            content=f"评论正文{index}",
            liked=liked,
            avatar_url=f"https://p1.music.126.net/avatar{index}.jpg",
            time=f"2024-01-0{index}",
            replies=[
                CommentItem(user=f"回复者{index}", content=f"回复内容{index}")
                for _ in range(replies)
            ],
        )
        for index in range(1, count + 1)
    ]
    return CommentPage(items=items, total=count, has_more=False)


# --------------------------------------------------------------- 人类可读格式化


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (9999, "9999"),
        (10_000, "1万"),
        (12_345, "1.2万"),
        (99_999, "10万"),
        (100_000, "10万"),
        (1_234_567, "123.5万"),
        (100_000_000, "1亿"),
        (123_456_789, "1.2亿"),
        (-5, "0"),
        ("12345", "1.2万"),
        ("abc", "0"),
        (None, "0"),
        (True, "0"),
    ],
)
def test_format_count(value: object, expected: str) -> None:
    assert format_count(value) == expected


def test_format_likes_matches_format_count() -> None:
    assert format_likes(12_345) == "1.2万"
    assert format_likes(0) == "0"
    assert format_likes(-1) == "0"


@pytest.mark.parametrize(
    ("text", "max_chars", "expected"),
    [
        ("abcdef", 3, "abc…"),
        ("abc", 3, "abc"),
        ("abc", 10, "abc"),
        ("abc", 0, "abc"),
        ("abc", -1, "abc"),
        ("", 5, ""),
        ("你好世界", 2, "你好…"),
        ("你好世界", 4, "你好世界"),
        ("  abc  ", 5, "abc"),
        (None, 5, ""),
        (123456, 3, "123…"),
    ],
)
def test_truncate_text(text: object, max_chars: int, expected: str) -> None:
    assert truncate_text(text, max_chars) == expected


# --------------------------------------------------------------------- 歌曲信息


def test_song_helpers() -> None:
    song = make_song()
    assert song_heading(song) == f"{TITLE} - {ARTIST}"
    assert song_artist(song) == ARTIST
    assert song_duration(song) == "03:45"
    assert song_source(song) == "网易云音乐"
    meta = song_meta(song)
    assert f"专辑：{ALBUM}" in meta
    assert "时长：03:45" in meta
    assert "网易云音乐" in meta


def test_song_helpers_tolerate_missing_fields() -> None:
    song = make_song(album="", duration_ms=0, artists=[])
    assert song_heading(song) == TITLE
    assert song_artist(song) == ""
    assert song_duration(song) == ""
    assert "时长" not in song_meta(song)
    assert "专辑" not in song_meta(song)


def test_song_helpers_accept_plain_mapping() -> None:
    raw = {"name": "夜曲", "artists": ["周杰伦"], "duration_ms": 228_000}
    assert song_heading(raw) == "夜曲 - 周杰伦"
    assert song_duration(raw) == "03:48"


def test_song_source_unknown_key_is_shown_as_is() -> None:
    assert song_source(make_song(provider_key="kugou")) == "kugou"


# --------------------------------------------------------------------- 歌词归一


def test_lyric_lines_prefers_structured_lines() -> None:
    lines = lyric_lines(make_lyric(2, translation=True))
    assert lines == [
        {"text": "第1行歌词", "translation": "line 1"},
        {"text": "第2行歌词", "translation": "line 2"},
    ]


def test_lyric_lines_falls_back_to_text() -> None:
    lines = lyric_lines(Lyric(text="第一句\n第二句\n第三句"))
    assert [line["text"] for line in lines] == ["第一句", "第二句", "第三句"]
    assert all(line["translation"] == "" for line in lines)


def test_lyric_lines_falls_back_to_translated_text() -> None:
    lines = lyric_lines(Lyric(translated="only translation"))
    assert [line["text"] for line in lines] == ["only translation"]


def test_lyric_lines_trims_edge_blank_lines_only() -> None:
    lyric = Lyric(
        lines=[
            LyricLine(text=""),
            LyricLine(text="第一段"),
            LyricLine(text=""),
            LyricLine(text="第二段"),
            LyricLine(text=""),
        ]
    )
    lines = lyric_lines(lyric)
    assert [line["text"] for line in lines] == ["第一段", "", "第二段"]


@pytest.mark.parametrize("empty", [Lyric(), Lyric(text="   "), Lyric(lines=[LyricLine()])])
def test_lyric_lines_empty(empty: Lyric) -> None:
    assert lyric_lines(empty) == []


# --------------------------------------------------------------------- 歌词文本


def test_build_lyrics_text_layout() -> None:
    text = build_lyrics_text(make_song(), make_lyric(2, translation=True), 0)
    assert text.splitlines()[0] == f"{TITLE} - {ARTIST}"
    assert "专辑：叶惠美 ｜ 时长：03:45 ｜ 网易云音乐" in text
    assert "第1行歌词" in text
    assert "  line 1" in text
    assert "（已省略" not in text


def test_build_lyrics_text_without_lyrics_uses_placeholder() -> None:
    text = build_lyrics_text(make_song(), Lyric(), 0)
    assert EMPTY_LYRIC_TEXT in text
    assert f"{TITLE} - {ARTIST}" in text


@pytest.mark.parametrize(
    ("max_lines", "visible", "truncated"),
    [
        (0, 5, False),
        (-3, 5, False),
        (1, 1, True),
        (2, 2, True),
        (4, 4, True),
        (5, 5, False),
        (99, 5, False),
    ],
)
def test_build_lyrics_text_max_lines(max_lines: int, visible: int, truncated: bool) -> None:
    text = build_lyrics_text(make_song(), make_lyric(5), max_lines)
    lines = text.splitlines()
    for index in range(1, visible + 1):
        assert f"第{index}行歌词" in lines
    for index in range(visible + 1, 6):
        assert f"第{index}行歌词" not in lines
    if truncated:
        assert f"（已省略 {5 - visible} 行，共 5 行）" in text
    else:
        assert "（已省略" not in text


def test_build_lyrics_text_tolerates_broken_input() -> None:
    text = build_lyrics_text(None, None, "abc")
    assert EMPTY_LYRIC_TEXT in text
    assert build_lyrics_text(make_song(), make_lyric(2), None)


# --------------------------------------------------------------------- 评论归一


def test_comment_entries_limits_count_and_keeps_order() -> None:
    entries = comment_entries(make_page(4), {"comments_count": 2})
    assert [entry["user"] for entry in entries] == ["用户1", "用户2"]
    assert entries[0]["content"] == "评论正文1"
    assert entries[0]["avatar_url"].endswith("avatar1.jpg")


@pytest.mark.parametrize(("count", "expected"), [(0, 0), (1, 1), (3, 3), (10, 3)])
def test_comment_entries_comments_count(count: int, expected: int) -> None:
    entries = comment_entries(make_page(3), RuntimeConfig.from_mapping({"comments_count": count}))
    assert len(entries) == expected


def test_comment_entries_formats_likes_and_anonymous_user() -> None:
    page = CommentPage(items=[CommentItem(user="", content="好听", liked=12_345)])
    entry = comment_entries(page, {"comments_count": 5})[0]
    assert entry["user"] == "匿名用户"
    assert entry["liked"] == 12_345
    assert entry["liked_text"] == "1.2万"
    assert entry["replies"] == []


def test_comment_entries_hides_replies_by_default() -> None:
    entries = comment_entries(make_page(1, replies=2), {})
    assert entries[0]["replies"] == []


def test_comment_entries_respects_reply_count() -> None:
    cfg = {"comments_show_reply": True, "comments_reply_count": 1}
    entries = comment_entries(make_page(1, replies=3), cfg)
    assert len(entries[0]["replies"]) == 1
    assert entries[0]["replies"][0]["user"] == "回复者1"


def test_comment_entries_tolerates_broken_page() -> None:
    assert comment_entries(None, {}) == []
    assert comment_entries({"items": "not-a-list"}, {}) == []


# --------------------------------------------------------------------- 评论文本


def test_build_comments_text_layout() -> None:
    text = build_comments_text(make_song(), make_page(2, liked=12_345), {})
    lines = text.splitlines()
    assert lines[0] == f"{TITLE} - {ARTIST}"
    assert "网易云评论（共 2 条）" in lines
    assert "1. 用户1（赞 1.2万）" in lines
    assert "   评论正文1" in lines
    assert "2. 用户2（赞 1.2万）" in lines


def test_build_comments_text_shows_truncation_hint() -> None:
    page = CommentPage(items=[CommentItem(user="甲", content="正文内容")], total=100_000)
    text = build_comments_text(make_song(), page, {})
    assert "网易云评论（共 10万 条，显示 1 条）" in text
    assert "正文内容" in text


def test_build_comments_text_hides_likes_when_disabled() -> None:
    text = build_comments_text(make_song(), make_page(1, liked=10), {"comments_show_likes": False})
    assert "赞 " not in text


def test_build_comments_text_includes_replies_when_enabled() -> None:
    cfg = {"comments_show_reply": True, "comments_reply_count": 1}
    text = build_comments_text(make_song(), make_page(1, replies=2), cfg)
    assert "└ 回复者1：回复内容1" in text
    assert "回复内容2" not in text


@pytest.mark.parametrize(
    ("max_chars", "keep", "ellipsis"),
    [
        (0, 200, False),
        (-1, 200, False),
        (200, 200, False),
        (120, 120, True),
        (50, 50, True),
        (10, 10, True),
        (1, 1, True),
    ],
)
def test_build_comments_text_max_chars(max_chars: int, keep: int, ellipsis: bool) -> None:
    content = "字" * 200
    page = CommentPage(items=[CommentItem(user="甲", content=content)])
    text = build_comments_text(make_song(), page, {"comments_max_chars": max_chars})
    body = [line.strip() for line in text.splitlines() if line.startswith("   字")]
    assert body, "正文行应当出现在评论文本里"
    assert len(body[0]) == keep + (1 if ellipsis else 0)
    assert body[0].endswith("…") is ellipsis


@pytest.mark.parametrize("count", [0])
def test_build_comments_text_returns_empty_without_entries(count: int) -> None:
    assert build_comments_text(make_song(), make_page(3), {"comments_count": count}) == ""


def test_build_comments_text_returns_empty_for_empty_page() -> None:
    assert build_comments_text(make_song(), CommentPage(), {}) == ""
    assert build_comments_text(None, None, None) == ""


def test_build_comments_text_accepts_none_config() -> None:
    text = build_comments_text(make_song(), make_page(1), None)
    assert "1. 用户1" in text
