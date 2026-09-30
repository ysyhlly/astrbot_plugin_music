"""网易云响应解析（纯函数，不联网，永不抛异常）。

字段契约见 core/models.py（Lyric / LyricLine / CommentItem / CommentPage / SongInfo）。
本模块只做「原始 JSON -> 数据模型」的转换：
- 所有 parse_* 对 None / 缺字段 / 类型错误 / 空数组都健壮，任何异常都退化成空结果；
- LRC 时间戳支持 [mm:ss]、[mm:ss.x]、[mm:ss.xx]、[mm:ss.xxx]，一行多时间戳会展开成多行；
- 翻译（tlyric）按时间戳对齐到 LyricLine.translation，允许 ±400ms 偏移；两边都没有
  时间戳时按行序对齐（长度不足则留空）；
- Lyric.text / Lyric.translated 是去掉时间戳标签后的纯文本（供 t2i 兜底渲染），
  逐行结构在 Lyric.lines；
- 评论正文会折叠空白并按 comments_max_chars 截断，撤回/空正文的评论条目会被丢弃；
- 另外提供歌手 / 歌名模糊匹配打分与排序辅助，供 provider 与 core/search.py 复用。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from ..models import CommentItem, CommentPage, Lyric, LyricLine, SongInfo
from .endpoints import picture_url, song_web_url

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIST_MATCH_EXACT",
    "ARTIST_MATCH_PARTIAL",
    "NAME_MATCH_EXACT",
    "NAME_MATCH_PARTIAL",
    "TRANSLATION_TOLERANCE_MS",
    "artist_match_score",
    "build_song",
    "extract_songs",
    "name_match_score",
    "normalise_for_match",
    "parse_comments",
    "parse_lrc",
    "parse_lyric",
    "parse_search",
    "parse_song_detail",
    "prioritise_by_artist",
]

TRANSLATION_TOLERANCE_MS = 400
"""翻译行与原文行时间戳的最大容差（毫秒）。"""

ARTIST_MATCH_EXACT = 3
"""歌手名完全一致。"""

ARTIST_MATCH_PARTIAL = 2
"""歌手名互相包含（含多歌手拼接串）。"""

NAME_MATCH_EXACT = 3
"""歌名完全一致。"""

NAME_MATCH_PARTIAL = 2
"""歌名互相包含。"""

_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LEADING_TAGS_RE = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")
_MATCH_NOISE_RE = re.compile(r"[\s\-_·・.,，。、!！?？'\"“”‘’()（）\[\]【】{}<>/\\|&+~*#@:：;；]+")
_WHITESPACE_RE = re.compile(r"\s+")
_ARTIST_SPLIT_RE = re.compile(r"[,，、/;；&]|\bfeat\.?\b|\bft\.?\b|\bwith\b", re.IGNORECASE)
_SECONDS_UPPER_BOUND = 10_000_000_000
"""大于该值的时间戳视为毫秒（网易云评论 time 字段为毫秒）。"""


# ------------------------------------------------------------------ 基础工具


def _mapping(value: Any) -> Mapping[str, Any]:
    """把任意值安全地当成映射使用。"""
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """把任意值安全地转成字符串（容器/布尔给空串）。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _int(value: Any, default: int = 0) -> int:
    """安全转 int。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _bool(value: Any, default: bool = False) -> bool:
    """安全转 bool（兼容 true/false 字符串与 0/1）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off", ""}:
            return False
    return default


def _collapse(text: str) -> str:
    """折叠空白（评论正文用）。"""
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalise_for_match(text: Any) -> str:
    """归一化用于匹配的文本：小写、去掉空白与标点。"""
    value = _text(text)
    if not value:
        return ""
    return _MATCH_NOISE_RE.sub("", value).strip().lower()


def _iter_names(value: Any) -> list[str]:
    """从字符串 / 映射 / 列表里取出歌手名列表。"""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        name = value.get("name") or value.get("nickname") or value.get("text")
        text = _text(name)
        return [text] if text.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        names: list[str] = []
        for item in value:
            names.extend(_iter_names(item))
        return names
    if isinstance(value, (int, float)):
        return [str(value)]
    return []


def _artist_tokens(artist: Any) -> list[str]:
    """把「周杰伦 / 费玉清 feat. 阿信」拆成归一化后的歌手片段。"""
    raw = _text(artist)
    if not raw.strip():
        return []
    tokens = [
        normalise_for_match(part) for part in _ARTIST_SPLIT_RE.split(raw)
    ]
    cleaned = [token for token in tokens if token]
    if cleaned:
        return cleaned
    fallback = normalise_for_match(raw)
    return [fallback] if fallback else []


def artist_match_score(artists: Any, artist: Any) -> int:
    """歌手匹配打分：完全一致 3、互相包含 2、不匹配 0（取所有歌手的最优值）。"""
    targets = _artist_tokens(artist)
    if not targets:
        return 0
    best = 0
    for name in _iter_names(artists):
        normalised = normalise_for_match(name)
        if not normalised:
            continue
        for target in targets:
            if normalised == target:
                best = max(best, ARTIST_MATCH_EXACT)
            elif target in normalised or normalised in target:
                best = max(best, ARTIST_MATCH_PARTIAL)
    return best


def name_match_score(name: Any, keyword: Any) -> int:
    """歌名匹配打分：完全一致 3、互相包含 2、不匹配 0。"""
    left = normalise_for_match(name)
    right = normalise_for_match(keyword)
    if not left or not right:
        return 0
    if left == right:
        return NAME_MATCH_EXACT
    if right in left or left in right:
        return NAME_MATCH_PARTIAL
    return 0


def prioritise_by_artist(songs: Any, artist: Any) -> list[SongInfo]:
    """按歌手匹配度稳定排序（匹配的排在前面，其余保持原顺序）。"""
    items = [song for song in (songs or []) if isinstance(song, SongInfo)]
    if not _text(artist).strip() or len(items) < 2:
        return items
    scored = [
        (artist_match_score(song.artists, artist), index, song)
        for index, song in enumerate(items)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [song for _, _, song in scored]


# ------------------------------------------------------------------ LRC 解析


def _fraction_to_ms(fraction: str | None) -> int:
    """把 [.xx] / [.xxx] 的百分位部分转成毫秒。"""
    if not fraction:
        return 0
    digits = re.sub(r"\D", "", fraction)
    if not digits:
        return 0
    if len(digits) == 1:
        return int(digits) * 100
    if len(digits) == 2:
        return int(digits) * 10
    return int(digits[:3])


def parse_lrc(text: Any) -> list[tuple[int, str]]:
    """解析 LRC 文本为 (毫秒, 正文) 列表（按时间升序，稳定）。

    一行多个时间戳会展开成多条；[ar:] [ti:] [offset:] 等标签行自动忽略。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    result: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        stamps = list(_TIMESTAMP_RE.finditer(raw_line))
        if not stamps:
            continue
        content = _TIMESTAMP_RE.sub("", raw_line).strip()
        if not content:
            continue
        for stamp in stamps:
            minutes = _int(stamp.group(1), 0)
            seconds = _int(stamp.group(2), 0)
            millis = _fraction_to_ms(stamp.group(3))
            result.append(((minutes * 60 + seconds) * 1000 + millis, content))
    result.sort(key=lambda item: item[0])
    return result


def _plain_lines(text: Any) -> list[str]:
    """取纯文本歌词行（去掉行首的 [00:01.00] / [ar:] 等标签，丢空行）。"""
    if not isinstance(text, str) or not text.strip():
        return []
    lines: list[str] = []
    for raw_line in text.splitlines():
        cleaned = _LEADING_TAGS_RE.sub("", raw_line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _text_of(value: Any) -> str:
    """从字符串或 {lyric: ...} 之类的映射里取歌词文本。"""
    if isinstance(value, str):
        return value if value.strip() else ""
    mapping = _mapping(value)
    for key in ("lyric", "text", "lrc", "trans"):
        inner = mapping.get(key)
        if isinstance(inner, str) and inner.strip():
            return inner
    return ""


def _first_text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序取第一个非空的歌词文本。"""
    for key in keys:
        text = _text_of(data.get(key))
        if text:
            return text
    return ""


def _nearest_translation(
    sorted_ms: list[int], index: Mapping[int, str], target: int, tolerance: int
) -> str:
    """在容差内找时间戳最接近的翻译行。"""
    if not sorted_ms or tolerance <= 0:
        return ""
    best_value = ""
    best_diff: int | None = None
    for candidate in sorted_ms:
        diff = candidate - target
        if diff > tolerance:
            break
        distance = abs(diff)
        if distance <= tolerance and (best_diff is None or distance < best_diff):
            best_value = index.get(candidate, "")
            best_diff = distance
    return best_value


def parse_lyric(payload: Any, *, tolerance_ms: int = TRANSLATION_TOLERANCE_MS) -> Lyric:
    """解析歌词接口响应为 Lyric（无歌词时返回空 Lyric）。

    兼容 lrc/tlyric 为映射（官方与自建 API）或直接为字符串（旧接口）两种形态。
    """
    try:
        data = _mapping(payload)
        if not data:
            return Lyric()
        lrc_text = _first_text(data, ("lrc", "lyric", "lrcLyric"))
        tlyric_text = _first_text(data, ("tlyric", "trans", "translation", "lyricTrans"))
        lrc_stamped = parse_lrc(lrc_text)
        trans_stamped = parse_lrc(tlyric_text)
        if lrc_stamped:
            base: list[tuple[int | None, str]] = list(lrc_stamped)
        else:
            base = [(None, line) for line in _plain_lines(lrc_text)]
        if trans_stamped:
            trans: list[tuple[int | None, str]] = list(trans_stamped)
        else:
            trans = [(None, line) for line in _plain_lines(tlyric_text)]
        lines: list[LyricLine] = []
        if lrc_stamped and trans_stamped:
            trans_index: dict[int, str] = {}
            for stamp, text in trans_stamped:
                trans_index.setdefault(int(stamp or 0), text)
            sorted_stamps = sorted(trans_index)
            for stamp, text in base:
                millis = int(stamp or 0)
                translation = trans_index.get(millis) or _nearest_translation(
                    sorted_stamps, trans_index, millis, _int(tolerance_ms, 0)
                )
                lines.append(LyricLine(text=text, translation=translation))
        else:
            for position, (_, text) in enumerate(base):
                translation = trans[position][1] if position < len(trans) else ""
                lines.append(LyricLine(text=text, translation=translation))
        return Lyric(
            text="\n".join(text for _, text in base),
            translated="\n".join(text for _, text in trans),
            lines=lines,
        )
    except Exception as exc:  # pragma: no cover - 兜底：绝不把异常抛给上层
        logger.warning("解析歌词失败：%r", exc)
        return Lyric()


# ------------------------------------------------------------------ 搜索解析


def extract_songs(payload: Any) -> list[Any]:
    """从搜索/详情响应里取出歌曲原始条目列表（找不到返回空列表）。"""
    data = _mapping(payload)
    if not data:
        return []
    sources: list[Mapping[str, Any]] = [data]
    for key in ("result", "data", "playlist"):
        inner = data.get(key)
        if isinstance(inner, Mapping):
            sources.append(inner)
    for source in sources:
        for key in ("songs", "song", "list", "items"):
            value = source.get(key)
            if isinstance(value, (list, tuple)):
                items = [item for item in value]
                if items:
                    return items
            if isinstance(value, Mapping):
                if key == "song" and ("id" in value or "name" in value):
                    return [value]
                nested = extract_songs(value)
                if nested:
                    return nested
    return []


def _cover_from_raw(raw: Mapping[str, Any]) -> str:
    """只认接口直接给出的封面 URL，**不再**由封面 id 拼兜底直链。

    为什么不再拼：网易云的封面路径段和文件名是**分别签发**的两个值，无法互相
    推导。同一个专辑接口返回的这两个字段经常对不上，例如：

        歌名            可直接使用的文件名(picUrl)   album.pic
        愛言葉III       109951170600289625           109951170600289630
        想你就写信      109951163038292176           109951163038292180
        再见莫妮卡      1099511657708805050          1099511657708805060

    用 album.pic 拼出来的 URL 会落到不存在的对象上，网易云返回
    HTTP 400 {"Code":"NotAnImage"}，机器人端表现为「封面没加载出来」。
    个别歌曲两者恰好相同时才会碰巧成功，所以这个兜底此前一直没被怀疑。

    正确做法：缺封面时由 provider 调 /song/detail 取权威 picUrl 回填。
    """
    for holder in (raw, _mapping(raw.get("al")), _mapping(raw.get("album"))):
        for key in ("picUrl", "pic_url", "blurPicUrl", "coverImgUrl"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                url = picture_url(value)
                if url:
                    return url
    return ""


def build_song(raw: Any, *, provider_key: str = "netease") -> SongInfo:
    """把一条原始歌曲条目转成 SongInfo（补封面兜底与外链）。"""
    source_key = _text(provider_key) or "netease"
    if not isinstance(raw, Mapping):
        return SongInfo(provider_key=source_key, source=source_key)
    song = SongInfo.from_mapping(raw, provider_key=source_key, source=source_key)
    if not song.cover_url:
        song.cover_url = _cover_from_raw(raw)
    if not song.url and song.id:
        song.url = song_web_url(song.id)
    return song


def parse_search(
    payload: Any,
    *,
    provider_key: str = "netease",
    limit: int = 0,
) -> list[SongInfo]:
    """解析搜索响应为 SongInfo 列表（无结果返回空列表）。

    丢弃没有歌名的条目（卡片必须有标题）；limit > 0 时截断到前 limit 条。
    """
    try:
        data = _mapping(payload)
        if not data:
            return []
        songs: list[SongInfo] = []
        for raw in extract_songs(data):
            song = build_song(raw, provider_key=provider_key)
            if song.name.strip():
                songs.append(song)
        cap = _int(limit, 0)
        if cap > 0:
            songs = songs[:cap]
        return songs
    except Exception as exc:  # pragma: no cover - 兜底：绝不把异常抛给上层
        logger.warning("解析搜索结果失败：%r", exc)
        return []


# ------------------------------------------------------------------ 详情解析


def parse_song_detail(payload: Any, song: Any = None) -> SongInfo:
    """用详情接口补全封面 / 时长 / 专辑 / 外链；无有效详情时原样返回 song。"""
    base = song if isinstance(song, SongInfo) else SongInfo.from_mapping(song)
    provider_key = base.provider_key or "netease"
    try:
        raws = extract_songs(payload)
        detail = (
            build_song(raws[0], provider_key=provider_key)
            if raws and isinstance(raws[0], Mapping)
            else SongInfo(provider_key=provider_key, source=provider_key)
        )
        merged = SongInfo(
            id=detail.id or base.id,
            name=detail.name or base.name,
            artists=detail.artists or base.artists,
            album=detail.album or base.album,
            duration_ms=detail.duration_ms or base.duration_ms,
            cover_url=detail.cover_url or base.cover_url,
            source=base.source or detail.source or provider_key,
            url=detail.url or base.url,
            audio_url=detail.audio_url or base.audio_url,
            provider_key=provider_key,
        )
        if not merged.url and merged.id:
            merged.url = song_web_url(merged.id)
        return merged
    except Exception as exc:  # pragma: no cover - 兜底：绝不把异常抛给上层
        logger.warning("解析歌曲详情失败：%r", exc)
        return base


# ------------------------------------------------------------------ 评论解析


def _format_epoch(value: int) -> str:
    """时间戳（秒或毫秒）格式化成 YYYY-MM-DD HH:MM，非法值返回空串。"""
    if value <= 0:
        return ""
    seconds = value / 1000 if value > _SECONDS_UPPER_BOUND else value
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))
    except (OSError, OverflowError, ValueError):
        return ""


def _format_comment_time(value: Any) -> str:
    """评论时间：数字（或纯数字字符串）时间戳格式化，其余原样保留。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return _format_epoch(int(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.isdigit():
            return _format_epoch(int(text))
        return text
    return ""


def _truncate(content: str, max_chars: int) -> str:
    """按最大字符数截断（超出时补省略号）。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars].rstrip() + "…"


def _raw_comment_time(raw: Mapping[str, Any]) -> Any:
    """取原始评论时间字段（兼容 time / ctime / createTime / timeStr）。"""
    for key in ("time", "ctime", "createTime", "timeStr", "timestamp"):
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _comment_key(raw: Any, item: CommentItem) -> str:
    """评论去重键：优先评论 id，其次「用户 + 正文」。"""
    mapping = _mapping(raw)
    for key in ("commentId", "comment_id", "id", "cid"):
        value = mapping.get(key)
        if value is not None and not isinstance(value, bool) and str(value).strip():
            return f"id:{value}"
    return f"user:{item.user}|content:{item.content}"


def _normalise_reply(reply: CommentItem, max_chars: int, depth: int = 0) -> None:
    """递归规范化楼中楼回复（截断正文、格式化时间）。"""
    if depth > 5:
        reply.replies = []
        return
    reply.content = _truncate(_collapse(reply.content), max_chars)
    for nested in reply.replies:
        _normalise_reply(nested, max_chars, depth + 1)


def parse_comments(
    payload: Any,
    max_chars: int = 0,
    *,
    sort: str = "hot",
    offset: int = 0,
) -> CommentPage:
    """解析评论响应为 CommentPage（无评论返回空页）。

    - sort="hot"：热门评论（hotComments）在前，再补普通评论，按 id 去重；
      sort="new"：忽略 hotComments，只用 comments（即按时间序的列表）。
    - max_chars > 0 时截断正文（超出加省略号）；空正文的条目直接丢弃。
    """
    try:
        data = _mapping(payload)
        if not data:
            return CommentPage()
        nested = data.get("data")
        if isinstance(nested, Mapping):
            data = nested
        order = _text(sort).strip().lower()
        raws: list[Any] = []
        if order != "new":
            hot = data.get("hotComments")
            if isinstance(hot, (list, tuple)):
                raws.extend(hot)
        normal = data.get("comments")
        if isinstance(normal, (list, tuple)):
            raws.extend(normal)
        elif isinstance(data.get("list"), (list, tuple)):
            raws.extend(data.get("list") or [])
        limit = _int(max_chars, 0)
        items: list[CommentItem] = []
        seen: set[str] = set()
        for raw in raws:
            item = CommentItem.from_mapping(raw)
            item.content = _truncate(_collapse(item.content), limit)
            if not item.content:
                continue
            raw_time = _raw_comment_time(_mapping(raw))
            formatted = _format_comment_time(raw_time)
            if formatted:
                item.time = formatted
            key = _comment_key(raw, item)
            if key in seen:
                continue
            seen.add(key)
            for reply in item.replies:
                _normalise_reply(reply, limit)
            items.append(item)
        total = _int(data.get("total", data.get("totalCount")), 0)
        more_raw = data.get("more")
        if more_raw is None:
            more_raw = data.get("hasMore")
        if more_raw is None:
            more_raw = data.get("has_more")
        if more_raw is None:
            has_more = total > max(0, _int(offset, 0)) + len(items)
        else:
            has_more = _bool(more_raw, False)
        return CommentPage(items=items, total=total, has_more=has_more)
    except Exception as exc:  # pragma: no cover - 兜底：绝不把异常抛给上层
        logger.warning("解析评论失败：%r", exc)
        return CommentPage()
