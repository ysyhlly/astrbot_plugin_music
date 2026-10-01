"""AstrBot 点歌插件入口：/点歌 <歌名> [歌手]。

用户可见行为（全部参数见 _conf_schema.json）：

1. 解析指令与参数（第一个位置参数=歌名，其余=歌手，歌手允许含空格），缺歌名回中文提示；
2. 开关 / 群白黑名单 / 用户黑名单 / 冷却 / 每日限额；
3. 搜歌选曲（core/search.py + provider）→ 歌曲卡片（core/cards，163/custom/share
   三态与 card_fallback 降级）→ 歌词图（core/lyrics_flow）→ 评论图（core/comments_flow）；
4. 每一条消息失败都只降级那一条（图 -> 纯文本），绝不把异常抛给用户；
5. HTTP 会话按事件创建并按事件关闭（core/session.py）。

本模块只做「解析 + 权限 + 编排 + 发送」，业务逻辑都在 core/ 里，纯函数
（parse_command_text / check_access / prepare_messages / run_music_request）可脱离
AstrBot 事件对象直接单测。

main.py 是 AstrBot 的插件模块（相对 import 依赖包上下文）：AstrBot 以
`data.plugins.astrbot_plugin_music.main` 加载它；测试里把插件根目录的**父目录**加入
sys.path 后 `import astrbot_plugin_music.main` 即可复用这些纯函数。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger as astrbot_logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry
except ImportError as exc:  # pragma: no cover - 只在 AstrBot 运行时可用
    raise ImportError(
        "点歌插件需要在 AstrBot (>=4.16,<5) 运行时中加载：当前环境无法导入 astrbot.api。"
        "请把本插件目录放进 AstrBot 的 data/plugins 下，由 AstrBot 启动时加载；"
        "直接用 python main.py 运行不会生效。",
    ) from exc

from .core.cards import (
    CardResult,
    at_component,
    build_card_result,
    build_card_text,
    plain_component,
    song_page_url,
)
from .core.comments_flow import CommentsFlowResult, run_comments_flow
from .core.config import RuntimeConfig, ensure_runtime_config
from .core.logging_utils import (
    describe_config,
    get_logger,
    log_debug,
    log_info,
    log_warning,
)
from .core.lyrics_flow import LyricsFlowResult, run_lyrics_flow
from .core.models import MusicQuery, SongInfo
from .core.provider import get_provider
from .core.ratelimit import RateLimiter, build_key
from .core.renderer import DefaultRenderer
from .core.search import resolve_song
from .core.session import transport_session

__all__ = [
    "DEFAULT_COMMAND",
    "DEFAULT_COMMAND_ALIASES",
    "CommandRequest",
    "MusicPlugin",
    "MusicRequestOutcome",
    "check_access",
    "parse_command_text",
    "prepare_messages",
    "resolve_provider",
    "run_music_request",
    "run_music_request_staged",
]

logger = get_logger("main")

PLUGIN_NAME = "astrbot_plugin_music"
PLUGIN_AUTHOR = "ysyhlly"
PLUGIN_DISPLAY_NAME = "点歌"
PLUGIN_VERSION = "v0.1.1"
PLUGIN_REPO = "https://github.com/ysyhlly/astrbot_plugin_music"
PLUGIN_DESC = "网易云点歌：/点歌 歌名 [歌手]，发送歌曲卡片、歌词图与评论图。"

DEFAULT_COMMAND = "点歌"
"""主指令名（@filter.command 注册用的就是它）。"""

DEFAULT_COMMAND_ALIASES = ("点歌", "听歌", "music")
"""静态注册的指令别名：与 _conf_schema.json 的 command_aliases 默认值保持一致。"""

MAX_KEYWORD_CHARS = 80
MAX_ARTIST_CHARS = 40

MISSING_ARGUMENT_MESSAGE = "请告诉我歌名哦～ 例如：/点歌 晴天 周杰伦（歌手可选，可以带空格）"
NOT_FOUND_MESSAGE = (
    "没有找到「{keyword}」这首歌～\n"
    "可以试试补充歌手名（/点歌 歌名 歌手），或者换成更完整的歌名再点一次。"
)
PROVIDER_UNAVAILABLE_MESSAGE = (
    "点歌服务暂时不可用：没有找到可用的网易云 provider，"
    "请管理员检查插件是否安装完整（core/netease）。"
)
FETCH_ERROR_MESSAGE = "点歌失败了，请稍后再试～"

_WHITESPACE_RE = re.compile(r"\s+")
_PREFIX_CHARS = "/／!！·.。"

_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
"""零宽字符与 BOM 等不可见字符。

聊天里完全看不见（从网页复制歌名、某些输入法/转发链路都会带上），但会被当成
合法歌名，让用户收到「没有找到该歌曲」而不是「请告诉我歌名」（B1 缺陷）。
"""

_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")


# --------------------------------------------------------------------- 小工具


def _as_text(value: Any) -> str:
    """安全转字符串。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _strip_invisible(text: str) -> str:
    """剔除零宽字符与 BOM（见 _INVISIBLE_CHARS）。

    语义定义为**删除**而不是当分隔符，所以：
    - "/点歌 \u200b"        -> 歌名为空 -> error="missing_keyword"（可读提示）；
    - "/点歌 晴\u200b天"    -> 歌名 "晴天"（保留可见部分，不制造断词）；
    - "/点\u200b歌 晴天"    -> 依然能识别指令词（部分平台/输入法会插入零宽）；
    - "\ufeff/点歌 晴天"    -> BOM 被剔除后唤醒前缀仍可识别。
    正常歌名（emoji / CJK / 全半角空格 / 路径样式字符串）不受影响。
    """
    if not text:
        return ""
    return _INVISIBLE_RE.sub("", text)


def _collapse(value: Any) -> str:
    """剔除零宽/BOM 后折叠空白并 strip（"晴天   周杰伦" -> "晴天 周杰伦"）。"""
    return _WHITESPACE_RE.sub(" ", _strip_invisible(_as_text(value))).strip()


# --------------------------------------------------------------------- 指令解析


@dataclass
class CommandRequest:
    """一次 /点歌 的解析结果。"""

    keyword: str = ""
    artist: str = ""
    raw: str = ""
    command: str = ""
    error: str = ""
    message: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.error and bool(self.keyword)

    def to_query(self, *, limit: int = 5) -> MusicQuery:
        return MusicQuery(keyword=self.keyword, artist=self.artist, limit=limit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "artist": self.artist,
            "command": self.command,
            "error": self.error,
        }


def _alias_set(aliases: Iterable[Any] | None) -> set[str]:
    """可接受的指令名集合（配置别名 ∪ 静态注册别名，小写比较）。"""
    accepted = {alias.lower() for alias in DEFAULT_COMMAND_ALIASES}
    for alias in aliases or ():
        text = _collapse(alias).lower()
        if text:
            accepted.add(text)
    return accepted


def _strip_prefix(text: str) -> tuple[str, bool]:
    """剥掉一个唤醒前缀（/ ／ ! ！ . 。）；返回 (剩余文本, 是否剥过)。

    AstrBot 的 WakingCheckStage 会在 filter 之前就把 wake_prefix 从
    event.message_str 里去掉（waking_check/stage.py:130），这里再兜一次是为了
    直接对着 "/点歌 晴天" 解析的场景（测试与手工调用）。
    """
    stripped = text.lstrip()
    if stripped and stripped[0] in _PREFIX_CHARS:
        return stripped[1:].strip(), True
    return stripped, False


def parse_command_text(raw: Any, aliases: Iterable[Any] | None = None) -> CommandRequest:
    """解析 "/点歌 晴天 周杰伦" -> keyword="晴天", artist="周杰伦"。

    规则：
    - 折叠空白后先剥一个唤醒前缀，再取第一个词作为指令名（大小写不敏感）；
    - 第一个位置参数 = 歌名；**其余全部** = 歌手（保留内部空格：
      "/点歌 晴天 周 杰 伦" -> artist="周 杰 伦"）；
    - 任何异常输入（None / 空串 / 只有指令名）都返回带中文提示的 CommandRequest，
      绝不抛异常。
    """
    text = _collapse(raw)
    accepted = _alias_set(aliases)
    if not text:
        return CommandRequest(raw="", error="empty", message=MISSING_ARGUMENT_MESSAGE)

    stripped, had_prefix = _strip_prefix(text)
    if not stripped:
        return CommandRequest(raw=text, error="empty", message=MISSING_ARGUMENT_MESSAGE)

    first, _, rest = stripped.partition(" ")
    command = ""
    for alias in sorted(accepted, key=len, reverse=True):
        if stripped.lower() == alias or stripped.lower().startswith(f"{alias} "):
            command = stripped[:len(alias)]
            rest = stripped[len(alias):].strip()
            break
    if not command and had_prefix:
        # 唤醒前缀之后的第一个词就是指令名（例如用户自定义别名，AstrBot 已经匹配到本
        # handler，只是不在配置的别名表里）
        command = first
    elif not command:
        # 理论上 AstrBot 的 CommandFilter 已经过滤过；留一条可读提示兜底
        return CommandRequest(raw=text, error="not_command", message=MISSING_ARGUMENT_MESSAGE)

    payload = rest.strip()
    if not payload:
        return CommandRequest(
            raw=text, command=command, error="missing_keyword", message=MISSING_ARGUMENT_MESSAGE
        )

    parts = payload.split(" ", 1)
    keyword = parts[0].strip()[:MAX_KEYWORD_CHARS]
    artist = parts[1].strip()[:MAX_ARTIST_CHARS] if len(parts) > 1 else ""
    if not keyword:
        return CommandRequest(
            raw=text, command=command, error="missing_keyword", message=MISSING_ARGUMENT_MESSAGE
        )
    return CommandRequest(keyword=keyword, artist=artist, raw=text, command=command)


# --------------------------------------------------------------------- 权限


def check_access(cfg: Any, *, group_id: Any = "", user_id: Any = "") -> str:
    """权限判断：返回 "" 表示放行，否则返回拒绝原因（disabled / group / user）。

    被拒绝时刻意**保持沉默**（不回消息也不惊动 LLM），调用方只需记 debug 日志。
    """
    config = ensure_runtime_config(cfg)
    if not config.enabled:
        return "disabled"
    if not config.is_group_allowed(group_id):
        return "group"
    if not config.is_user_allowed(user_id):
        return "user"
    return ""


def resolve_provider(config: RuntimeConfig) -> Any | None:
    """按配置取 provider，并把用户配置同步给它（热重载后仍生效）。"""
    provider = get_provider(config.provider)
    if provider is None:
        return None
    configure = getattr(provider, "configure", None)
    if callable(configure):
        try:
            provider = configure(config) or provider
        except Exception as exc:  # pragma: no cover - provider 实现异常
            log_warning("同步 provider 配置失败：%r", exc, logger=logger)
    return provider


# --------------------------------------------------------------------- 编排


@dataclass
class MusicRequestOutcome:
    """一次点歌请求的编排结果（messages 按发送顺序排列）。

    messages 的每一项是 (kind, payload)：
    - ("chain", [组件...])：用 event.chain_result 发送；
    - ("image", url/path)：用 event.image_result 发送；
    - ("text", str)：用 event.plain_result 发送。
    """

    ok: bool = False
    status: str = ""
    keyword: str = ""
    artist: str = ""
    song: SongInfo | None = None
    messages: list[tuple[str, Any]] = field(default_factory=list)
    card_phase_ok: bool = False
    """第一段（限额/选曲/卡片）是否走通：True 表示可以继续跑第二段。

    与 ok 的区别：ok 表示整条流程完成；卡片被配置关掉时第一段 messages 为空，
    但 card_phase_ok 仍为 True（要靠第二段的歌词/评论出内容）。
    """
    card: CardResult | None = None
    lyrics: LyricsFlowResult | None = None
    comments: CommentsFlowResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "card_phase_ok": self.card_phase_ok,
            "status": self.status,
            "keyword": self.keyword,
            "artist": self.artist,
            "song_id": self.song.id if self.song else "",
            "messages": [
                (kind, len(payload) if kind == "chain" else len(str(payload)))
                for kind, payload in self.messages
            ],
            "card": self.card.to_dict() if self.card else None,
            "lyrics": self.lyrics.to_dict() if self.lyrics else None,
            "comments": self.comments.to_dict() if self.comments else None,
        }


def _summary_message(song: SongInfo | None) -> str:
    """兜底摘要（避免「歌找到了但用户什么都收不到」）。"""
    if song is None:
        return FETCH_ERROR_MESSAGE
    name = song.display_name or song.name or "未知歌曲"
    url = song.url or song_page_url(song.id)
    lines = [f"🎵 {name}"]
    if url:
        lines.append(url)
    return "\n".join(lines)


async def _enrich_song(
    config: RuntimeConfig, song: SongInfo, provider: Any, transport: Any
) -> SongInfo:
    """按需补全封面/时长（官方直连的旧搜索接口不带封面）。"""
    needs_cover = bool(config.card_show_cover) and not song.cover_url and bool(config.card_enable)
    needs_duration = not song.duration_ms
    if not (needs_cover or needs_duration):
        return song
    fetch = getattr(provider, "song_detail", None)
    if not callable(fetch):
        return song
    try:
        result = fetch(song, transport)
        if hasattr(result, "__await__"):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_warning("补全歌曲详情失败（不影响后续流程）：%r", exc, logger=logger)
        return song
    if isinstance(result, SongInfo):
        return result
    if isinstance(result, dict):
        return SongInfo.from_mapping(result)
    return song


async def run_music_request(
    cfg: Any,
    *,
    keyword: Any,
    artist: Any = "",
    provider: Any = None,
    transport: Any = None,
    renderer: Any = None,
    limiter: RateLimiter | None = None,
    rate_key: str = "",
    factory: Any = None,
    platform_name: str | None = None,
) -> MusicRequestOutcome:
    """编排一次点歌：限额 → 选曲 → 卡片 → 歌词 → 评论。

    分两段执行，便于调用方**先把卡片发出去**（见 run_music_request_staged）：
    第一段返回时 outcome.messages 里只有「卡片或前置错误」；第二段（_run_enrich_phase）
    再补歌词与评论。单跑本函数等价于两段连着执行完。

    不抛异常（包括 provider / 渲染器异常）：每一步失败都退化成可读中文消息，
    并且保证 ok=True 时 messages 至少有一条（用户不会什么都收不到）。
    """
    outcome = await _run_card_phase(
        cfg,
        keyword=keyword,
        artist=artist,
        provider=provider,
        transport=transport,
        factory=factory,
        limiter=limiter,
        rate_key=rate_key,
        platform_name=platform_name,
    )
    if not outcome.card_phase_ok:
        return outcome
    await _run_enrich_phase(
        outcome, cfg, provider=provider, transport=transport, renderer=renderer
    )
    return outcome


async def _run_card_phase(
    cfg: Any,
    *,
    keyword: Any,
    artist: Any = "",
    provider: Any = None,
    transport: Any = None,
    limiter: RateLimiter | None = None,
    rate_key: str = "",
    factory: Any = None,
    platform_name: str | None = None,
) -> MusicRequestOutcome:
    """第一段：限额 → 选曲 → 卡片。返回时 messages 里只有卡片或前置错误。

    card_phase_ok=True 表示前置步骤都过了（可以继续跑第二段），此时 messages
    可能是空的——那不代表失败，只是卡片被配置关掉了（要靠第二段补内容）。
    """
    config = ensure_runtime_config(cfg)
    outcome = MusicRequestOutcome(keyword=_collapse(keyword), artist=_collapse(artist))
    log_debug(config, "点歌请求：%s", describe_config(config), logger=logger)

    if limiter is not None:
        allowed, message = await limiter.check_and_consume(rate_key)
        if not allowed:
            outcome.status = "rate_limited"
            outcome.messages = [("text", message or "点歌太频繁啦，稍后再试～")]
            return outcome

    source = provider if provider is not None else resolve_provider(config)
    if source is None:
        outcome.status = "no_provider"
        outcome.messages = [("text", PROVIDER_UNAVAILABLE_MESSAGE)]
        return outcome

    try:
        song = await resolve_song(config, transport, outcome.keyword, outcome.artist, provider=source)
    except Exception as exc:  # pragma: no cover - resolve_song 自身已兜底
        log_warning("选曲失败：%r", exc, logger=logger)
        song = None
    if song is None:
        outcome.status = "not_found"
        outcome.messages = [("text", NOT_FOUND_MESSAGE.format(keyword=outcome.keyword))]
        return outcome

    song = await _enrich_song(config, song, source, transport)
    outcome.song = song

    # 1) 歌曲卡片（失败按 card_fallback 退化为 Share 或纯文本）
    try:
        card = await build_card_result(
            song, config, provider=source, transport=transport, factory=factory,
            platform_name=platform_name,
        )
    except Exception as exc:  # pragma: no cover - 卡片层自身已兜底
        log_warning("卡片构造异常，改用纯文本：%r", exc, logger=logger)
        card = CardResult(components=[], text=build_card_text(song), degraded=True, reason="exception")
    outcome.card = card
    if card.components:
        outcome.messages.append(("chain", list(card.components)))
    elif card.text_payload:
        outcome.messages.append(("text", card.text_payload))

    outcome.card_phase_ok = True
    return outcome


async def _run_enrich_phase(
    outcome: MusicRequestOutcome,
    cfg: Any,
    *,
    provider: Any = None,
    transport: Any = None,
    renderer: Any = None,
) -> MusicRequestOutcome:
    """第二段：歌词图 + 评论图（并发），追加到 outcome.messages 末尾。

    只在第一段 card_phase_ok=True 时调用；song 缺失时直接返回。
    """
    config = ensure_runtime_config(cfg)
    song = outcome.song
    if song is None:
        return outcome

    # provider 可能是 None（调用方没传，由第一段自己 resolve 的），这里要补回来。
    # 漏了这一步时歌词/评论流程会拿到 None -> 取不到数据 -> 全部退化成纯文本，
    # 表现是"图变成了一行字"。
    source = provider if provider is not None else resolve_provider(config)
    if source is None:
        return outcome

    # fallback_to_plain 是「全局纯文本兜底」总开关：关掉后渲染失败不再补发纯文本。
    # 主动关闭 t2i 的文本输出以 status="text" 标记，不受兜底开关影响。
    # 注意与两个相邻开关的区别，勿混用：
    #   * card_fallback（share/text）是用户显式选择的「卡片形态」，因此不受本开关影响——
    #     用户主动选 text 就应该拿到文本；
    #   * 末尾的 _summary_message 是「三者全关时至少告诉用户点到了什么」的安全网，
    #     它保证消息链非空（空链会被 AstrBot 的 respond stage 整条跳过，用户什么都收不到），
    #     因此同样不受本开关影响。
    plain_fallback = bool(getattr(config, "fallback_to_plain", True))

    # 2) 歌词图 + 3) 评论图：**并发**执行。
    # 两者互不依赖（各取各的接口、渲染各自模板），串行会让耗时相加；
    # 并发后总耗时约等于较慢的那一个。实测：串行 2.8~4.6s -> 并发 1.4~2.3s。
    # 注意：aiohttp 的 ClientSession 支持并发请求，renderer 无状态，因此共享
    # 同一个 transport/renderer 是安全的；各自内部已有的异常兜底保持不变。
    async def _safe(label: str, flow: Any) -> Any:
        try:
            return await flow
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 流程自身已兜底
            log_warning("%s异常：%r", label, exc, logger=logger)
            return None

    lyrics, comments = await asyncio.gather(
        _safe("歌词流程", run_lyrics_flow(config, song, source, transport, renderer)),
        _safe("评论流程", run_comments_flow(config, song, source, transport, renderer)),
    )
    outcome.lyrics = lyrics
    if lyrics is not None:
        if lyrics.has_image:
            outcome.messages.append(("image", lyrics.image))
        elif lyrics.has_text and (lyrics.status == "text" or plain_fallback):
            outcome.messages.append(("text", lyrics.text))

    outcome.comments = comments
    if comments is not None:
        if comments.has_image:
            outcome.messages.append(("image", comments.image))
        elif comments.has_text and (comments.status == "text" or plain_fallback):
            outcome.messages.append(("text", comments.text))

    if not outcome.messages:
        # 卡片/歌词/评论全被配置关掉时的兜底：至少告诉用户点到了什么
        outcome.messages.append(("text", _summary_message(song)))

    outcome.ok = True
    outcome.status = "ok"
    return outcome


async def run_music_request_staged(
    cfg: Any,
    *,
    keyword: Any,
    artist: Any = "",
    provider: Any = None,
    transport: Any = None,
    renderer: Any = None,
    limiter: RateLimiter | None = None,
    rate_key: str = "",
    factory: Any = None,
    platform_name: str | None = None,
) -> AsyncGenerator[tuple[MusicRequestOutcome, list[tuple[str, Any]]], None]:
    """分段产出点歌结果：先给卡片，再给歌词/评论（顺序不变）。

    为什么这样切：歌词图与评论图各要 0.5~2s 渲染，而卡片几乎是瞬时的。
    调用方拿到第一段就立刻 yield 出去，用户马上看到卡片，再等两张图。
    若全程等完再发，用户要盯着空白等 1.5s 以上；分段后感知延迟≈搜歌时间。

    产出形状：(outcome, 本段新增的消息列表)。每次只含**本段新增**的部分，
    调用方直接逐条发送即可；不要把 outcome.messages 整个发出去，否则卡片会重发。

    约定：
    - 第一段一定先产出（哪怕内容是前置错误文案），保证用户有反馈；
    - 第一段是限流/未找到等前置失败时只产出一段就结束；
    - 第二段只在第一段成功时执行，产出顺序恒为 卡片 → 歌词 → 评论。
    """
    outcome = await _run_card_phase(
        cfg,
        keyword=keyword,
        artist=artist,
        provider=provider,
        transport=transport,
        factory=factory,
        limiter=limiter,
        rate_key=rate_key,
        platform_name=platform_name,
    )
    if not outcome.card_phase_ok:
        # 前置失败（限流 / 没搜到 / 无 provider）：一次性把该说的说了
        yield outcome, list(outcome.messages)
        return

    # 第一段：只有卡片。配置关掉卡片时这里为空列表，调用方不发东西即可，
    # 但外层仍会先跑完这一段，保证「先卡片后图片」的顺序不会被打乱。
    yield outcome, list(outcome.messages)

    sent = len(outcome.messages)
    await _run_enrich_phase(
        outcome, cfg, provider=provider, transport=transport, renderer=renderer
    )
    outcome.ok = True
    if not outcome.status:
        outcome.status = "ok"
    # 第二段：只产出**本段新增**的歌词/评论，避免把卡片重发一遍
    yield outcome, list(outcome.messages[sent:])


def prepare_messages(
    outcome: MusicRequestOutcome,
    *,
    reply_with_at: bool = False,
    user_id: Any = "",
    factory: Any = None,
    only: Iterable[tuple[str, Any]] | None = None,
) -> list[tuple[str, Any]]:
    """把编排结果整理成发送列表，并按需在第一条消息前加 @。

    only 给分段发送用：只处理这一段新增的消息（None 表示整条 outcome.messages）。
    """
    source = outcome.messages if only is None else list(only)
    messages = [(kind, payload) for kind, payload in source if payload]
    if not messages or not reply_with_at:
        return messages
    mention = at_component(user_id, factory)
    if mention is None:
        return messages
    kind, payload = messages[0]
    if kind == "chain":
        messages[0] = ("chain", [mention, *list(payload)])
    elif kind == "text":
        body = plain_component(payload, factory)
        if body is not None:
            messages[0] = ("chain", [mention, body])
    return messages


# --------------------------------------------------------------------- 插件类


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class MusicPlugin(Star):
    """点歌插件：/点歌 <歌名> [歌手]。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        runtime = ensure_runtime_config(self.config)
        self.rate_limiter = RateLimiter.from_config(runtime)
        handler = star_handlers_registry.get_handler_by_full_name(
            f"{self.cmd_song_request.__module__}_cmd_song_request"
        )
        self._command_filter: CommandFilter | None = None
        if handler is not None:
            for item in handler.event_filters:
                if isinstance(item, CommandFilter):
                    self._command_filter = item
                    item.alias = {
                        name for alias in runtime.command_aliases
                        if (name := _collapse(alias)) and name != item.command_name
                    }
                    # CommandFilter caches the registered names before dispatch.
                    item._cmpl_cmd_names = None
                    break

    # ------------------------------------------------------------ 指令

    @filter.command(DEFAULT_COMMAND, alias={"听歌", "music"})
    async def cmd_song_request(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """点歌：/点歌 歌名 [歌手]。"""
        config = ensure_runtime_config(self.config)
        group_id = _event_value(event, "get_group_id")
        user_id = _event_value(event, "get_sender_id")
        umo = _as_text(getattr(event, "unified_msg_origin", "")) or _event_value(
            event, "get_session_id"
        )

        denied = check_access(config, group_id=group_id, user_id=user_id)
        if denied:
            log_debug(
                config,
                "点歌被拒绝：reason=%s group=%s user=%s",
                denied,
                group_id,
                user_id,
                logger=logger,
            )
            _stop(event)
            return

        aliases = config.command_aliases
        if self._command_filter is not None:
            aliases = self._command_filter.get_complete_command_names()
        request = parse_command_text(
            _event_text(event),
            aliases=aliases,
        )
        if not request.is_valid:
            yield event.plain_result(request.message or MISSING_ARGUMENT_MESSAGE)
            return

        limiter = self._limiter(config)
        rate_key = build_key(umo, user_id)
        renderer = DefaultRenderer(self)
        # 分段发送：卡片先出去，歌词/评论渲染完再发。
        # 关键点是 yield 之后就交还给 AstrBot 发送，所以卡片不会等两张图。
        # 顺序仍然是 卡片 -> 歌词 -> 评论（各段 messages 天然有序）。
        sent_any = False
        try:
            async with transport_session(config) as transport:
                async for outcome, fresh in run_music_request_staged(
                    config,
                    keyword=request.keyword,
                    artist=request.artist,
                    transport=transport,
                    renderer=renderer,
                    limiter=limiter,
                    rate_key=rate_key,
                    platform_name=_event_value(event, "get_platform_name") or None,
                ):
                    messages = prepare_messages(
                        outcome,
                        reply_with_at=config.reply_with_at and not sent_any,
                        user_id=user_id,
                        only=fresh,
                    )
                    for kind, payload in messages:
                        if sent_any:
                            await self._pause(config)
                        if kind == "chain":
                            yield event.chain_result(list(payload))
                        elif kind == "image":
                            yield event.image_result(payload)
                        else:
                            yield event.plain_result(payload)
                        sent_any = True
                    log_info(
                        config,
                        "点歌分段完成：%s",
                        outcome.to_dict(),
                        logger=logger,
                    )
        except Exception as exc:
            astrbot_logger.error(f"点歌流程异常：{exc!r}", exc_info=True)
            if not sent_any:
                yield event.plain_result(FETCH_ERROR_MESSAGE)

    # ------------------------------------------------------------ 内部

    def _limiter(self, config: RuntimeConfig) -> RateLimiter:
        """复用同一个限流器（保留计数），但跟随配置热更新冷却与每日上限。"""
        limiter = self.rate_limiter
        if (
            limiter.cooldown_seconds != config.cooldown_seconds
            or limiter.daily_limit != config.daily_limit
        ):
            limiter.cooldown_seconds = config.cooldown_seconds
            limiter.daily_limit = config.daily_limit
        return limiter

    @staticmethod
    async def _pause(config: RuntimeConfig) -> None:
        """多条消息之间的节奏间隔（send_delay）。"""
        delay = float(getattr(config, "send_delay", 0.0) or 0.0)
        if delay > 0:
            await asyncio.sleep(delay)


def _event_text(event: Any) -> str:
    """取事件原始文本（优先 get_message_str，其次 message_str 属性）。"""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            return _as_text(getter())
        except Exception:  # pragma: no cover - 事件实现异常
            pass
    return _as_text(getattr(event, "message_str", ""))


def _event_value(event: Any, method: str) -> str:
    """安全调用事件上的无参方法（取群号/用户号等）。"""
    func = getattr(event, method, None)
    if not callable(func):
        return ""
    try:
        return _as_text(func()).strip()
    except Exception:  # pragma: no cover - 事件实现异常
        return ""


def _stop(event: Any) -> None:
    """静默吞掉指令（被权限拒绝时不要再让 LLM 接这句话）。"""
    stop = getattr(event, "stop_event", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # pragma: no cover - 事件实现异常
            pass
