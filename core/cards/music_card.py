"""歌曲卡片构造：163 / custom / share 三态 + 降级链。

AstrBot respond stage 的非空校验规则（astrbot/core/pipeline/respond/stage.py:21-50）::

    Comp.Music  : (comp.id and comp._type and comp._type != "custom")
                  or (comp._type == "custom" and comp.url and comp.audio and comp.title)
    Comp.Share  : bool(comp.url) or bool(comp.title)
    Comp.Record : bool(comp.file)
    Comp.Plain  : bool(comp.text and comp.text.strip())
    Comp.At     : bool(comp.qq) or bool(comp.name)

真正的高危场景（respond/stage.py:109-128, 242-257 实测语义）
----------------------------------------------------------
- 只有**整条消息链所有组件都不合法**时，stage 才判定为空并跳过整条链 —— 用户什么都收不到；
- stage 只会单独摘掉「全空白 Plain」，**不合法的 Music 不会被摘掉**，会继续下发给平台
  适配器（可能退化成平台侧报错或空白气泡）。
所以卡片构造失败/降级时必须保证链里**至少有一个合法组件**（退化的 Share 或非空 Plain），
本模块的降级链正是为此设计，绝不依赖「非法 Music 会被自动剔除」。

实测发现（决定了卡片能不能真的发出去）
------------------------------------------
AstrBot 4.28.1 在 Python < 3.14 下用 `from pydantic.v1 import BaseModel` 定义消息组件
（astrbot/core/message/components.py:35-38），而 pydantic v1 默认
`underscore_attrs_are_private=False` 且 extra=ignore：**构造函数里的 _type 会被直接丢弃**。

    >>> from astrbot.api.message_components import Music
    >>> comp = Music(_type="163", id=186016, title="晴天")
    >>> hasattr(comp, "_type")
    False
    >>> RespondStage._component_validators[Comp.Music](comp)   # 内部读 comp._type
    AttributeError: 'Music' object has no attribute '_type'

后果有两个，都比「被静默丢弃」更隐蔽：
1) Music 校验器抛 AttributeError，被 respond/stage.py:246 的 `except Exception` 吞掉
   只记一行 warning，于是**空内容检查被跳过**，卡片照样下发；
2) `toDict()` 依赖 `self.__dict__` 里的 `_type` 生成 `data["type"]`（components.py:98-106），
   拿不到就发出一个没有 type 的 music 段 → 平台侧空白/报错。

因此 _construct_music 构造完组件后会调用 _ensure_music_type 把 _type 补写进实例
`__dict__`（`object.__setattr__`，绕过 pydantic 的未知字段限制），让
comp._type / toDict()["data"]["type"] / respond 校验器三者一致。pydantic v2（Python >= 3.14）
把 _type 当 private attribute，构造参数同样进不去，这段补写对两个版本都成立；
补写失败只记 debug 日志，不影响后续降级逻辑。

关键契约
--------
- build_card(song, cfg, ...) -> list[组件]：契约要求的入口，可直接放进
  event.chain_result(...)；需要知道降级原因时用 build_card_result(...)。
- CardResult.components 非空时由调用方发送组件链；为空时才发送纯文本兜底。
  为避免调用方不慎重复发送，请统一读 @@BT@@CardResult.text_payload@@BT@@
  （components 非空时它固定为空串），而不是直接读 .text。
- COMPONENT_FACTORY 是模块级可注入的组件工厂（默认延迟 import
  astrbot.api.message_components），因此卡片逻辑在没有 astrbot 的环境里也能单测。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..config import ensure_runtime_config

logger = logging.getLogger(__name__)

__all__ = [
    "CARD_TYPES",
    "COMPONENT_FACTORY",
    "FALLBACK_TYPES",
    "CardResult",
    "at_component",
    "build_card",
    "build_card_result",
    "build_card_result_from_payload",
    "build_card_text",
    "build_payload_from_song",
    "card_extra_components",
    "component_is_deliverable",
    "default_component_factory",
    "ensure_music_type",
    "music_component_is_valid",
    "normalise_card_type",
    "normalise_song_id",
    "plain_component",
    "resolve_component_factory",
    "song_page_url",
]

CARD_TYPES: tuple[str, ...] = ("163", "custom", "share")
"""支持的卡片形态。"""

FALLBACK_TYPES: tuple[str, ...] = ("share", "text")
"""cfg.card_fallback 的合法取值。"""

DEFAULT_CARD_TYPE = "163"
DEFAULT_FALLBACK_TYPE = "share"

SOURCE_LABEL = "网易云音乐"
"""卡片 content 里的来源文案（= NeteaseProvider.display_name）。"""

UNKNOWN_SONG_NAME = "未知歌曲"

_HINT_BY_REASON: dict[str, str] = {
    "music_invalid_id": "网易云返回的歌曲 id 不可用，已改为分享链接",
    "custom_no_audio": "暂时拿不到播放地址（官方直连常见），已改为分享链接",
    "custom_no_url": "这首歌没有可用链接，已改为文本",
    "custom_invalid": "网易云的歌曲信息不完整，已改为分享链接",
    "share_invalid": "这首歌没有可用链接，已改为文本",
}
_DEFAULT_HINT = "当前配置下无法生成歌曲卡片，已改为文本"


# --------------------------------------------------------------------- 组件工厂


class _ComponentBundle:
    """默认工厂返回的组件命名空间（把 AstrBot 的组件类捆在一起）。"""

    __slots__ = ("At", "Music", "Plain", "Record", "Share")

    def __init__(self, Music: Any, Share: Any, Record: Any, Plain: Any, At: Any) -> None:
        self.Music = Music
        self.Share = Share
        self.Record = Record
        self.Plain = Plain
        self.At = At


def default_component_factory() -> Any:
    """默认组件工厂：延迟 import AstrBot 官方组件。

    延迟 import 让 core 包在没有 astrbot 的环境（单测 / 静态检查）里也能导入；
    真的拿不到组件时抛 ImportError，由 resolve_component_factory 兜底成 None。
    """
    from astrbot.api.message_components import At, Music, Plain, Record, Share

    return _ComponentBundle(Music=Music, Share=Share, Record=Record, Plain=Plain, At=At)


COMPONENT_FACTORY: Callable[[], Any] | None = default_component_factory
"""可注入的组件工厂。

测试里可以整体替换：`music_card.COMPONENT_FACTORY = lambda: FakeComponents`，
或按调用点传入 factory=...（优先级：显式 factory > COMPONENT_FACTORY）。
置为 None 表示「当前环境没有可用组件」，卡片逻辑会退化成纯文本。
"""


def _looks_like_factory(obj: Any) -> bool:
    """工厂产物至少要能造 Music 与 Share（其余组件按需取）。"""
    return callable(getattr(obj, "Music", None)) and callable(getattr(obj, "Share", None))


def resolve_component_factory(factory: Any = None) -> Any | None:
    """解析组件工厂；不可用时返回 None（绝不抛异常）。"""
    candidate = COMPONENT_FACTORY if factory is None else factory
    if candidate is None:
        return None
    if not _looks_like_factory(candidate):
        if not callable(candidate):
            return None
        try:
            candidate = candidate()
        except Exception as exc:
            logger.warning("加载 AstrBot 组件失败，卡片将退化为纯文本：%r", exc)
            return None
    return candidate if _looks_like_factory(candidate) else None


# --------------------------------------------------------------------- 校验规则


def music_component_is_valid(component: Any) -> bool:
    """复刻 respond stage 对 Comp.Music 的非空校验。"""
    card_type = str(getattr(component, "_type", "") or "")
    custom_ok = (
        card_type == "custom"
        and bool(getattr(component, "url", ""))
        and bool(getattr(component, "audio", ""))
        and bool(getattr(component, "title", ""))
    )
    standard_ok = bool(getattr(component, "id", None)) and bool(card_type) and card_type != "custom"
    return bool(standard_ok or custom_ok)


_COMPONENT_RULES: dict[str, Callable[[Any], bool]] = {
    "Music": music_component_is_valid,
    "Share": lambda comp: bool(getattr(comp, "url", "")) or bool(getattr(comp, "title", "")),
    "Record": lambda comp: bool(getattr(comp, "file", "")),
    "Video": lambda comp: bool(getattr(comp, "file", "")),
    "Image": lambda comp: bool(getattr(comp, "file", "")),
    "Plain": lambda comp: bool(str(getattr(comp, "text", "") or "").strip()),
    "At": lambda comp: bool(getattr(comp, "qq", "")) or bool(getattr(comp, "name", "")),
}


def _component_kind(component: Any) -> str:
    """判断组件类型：先看类名（与 AstrBot 同名），再按字段鸭子类型兜底。"""
    name = type(component).__name__
    if name in _COMPONENT_RULES:
        return name
    if hasattr(component, "audio"):
        return "Music"
    if hasattr(component, "url") and hasattr(component, "content"):
        return "Share"
    if hasattr(component, "file"):
        return "Record"
    if hasattr(component, "qq"):
        return "At"
    if hasattr(component, "text"):
        return "Plain"
    return ""


def component_is_deliverable(component: Any) -> bool:
    """组件能否真的被 AstrBot 发出去（判定规则与 respond stage 完全一致）。"""
    if component is None:
        return False
    rule = _COMPONENT_RULES.get(_component_kind(component))
    if rule is None:
        # 未知组件类型不阻断（例如未来 AstrBot 新增的类型）
        return True
    return bool(rule(component))


def _is_validation_error(exc: BaseException) -> bool:
    """是否为 pydantic 校验错误（延迟 import，不引入强依赖）。"""
    try:
        from pydantic import ValidationError  # 延迟 import
    except Exception:  # pragma: no cover - pydantic 随 astrbot 一起安装
        return False
    return isinstance(exc, ValidationError)


def _safe_construct(builder: Any, **kwargs: Any) -> Any | None:
    """构造单个组件；任何异常都吞掉并返回 None（卡片降级由调用方处理）。"""
    try:
        return builder(**kwargs)
    except Exception as exc:
        if _is_validation_error(exc):
            logger.warning("AstrBot 组件字段校验失败，已触发卡片降级：%s", exc)
        else:
            logger.warning("构造 AstrBot 组件失败，已触发卡片降级：%r", exc)
        return None


# --------------------------------------------------------------------- 结果对象


@dataclass
class CardResult:
    """卡片构造结果（components 为空时才需要发送 text）。"""

    components: list[Any] = field(default_factory=list)
    text: str = ""
    card_type: str = "none"
    requested_type: str = DEFAULT_CARD_TYPE
    degraded: bool = False
    reason: str = ""
    audio_url: str = ""
    song_id: int | None = None

    def __iter__(self):
        """允许 `for comp in result` / `chain_result(*result)`。"""
        return iter(self.components)

    def __len__(self) -> int:
        return len(self.components)

    def __getitem__(self, index: int) -> Any:
        return self.components[index]

    def __bool__(self) -> bool:
        return bool(self.components)

    @property
    def text_payload(self) -> str:
        """调用方应当发送的纯文本：components 非空时为空串，避免重复发送。"""
        return "" if self.components else self.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_type": self.card_type,
            "requested_type": self.requested_type,
            "degraded": self.degraded,
            "reason": self.reason,
            "components": len(self.components),
            "text": self.text,
            "audio_url": self.audio_url,
            "song_id": self.song_id,
        }


# --------------------------------------------------------------------- 小工具


def _as_text(value: Any) -> str:
    """安全转字符串（None/bool 给空串、数字转文本、其余给空串）。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    return ""


def normalise_card_type(value: Any) -> str:
    """把任意值规范成 163/custom/share；非法值返回空串。"""
    text = _as_text(value).lower()
    return text if text in CARD_TYPES else ""


def normalise_song_id(raw: Any) -> int | None:
    """防御性整数化：AstrBot 的 Music.id 是 `int | None`。

    网易云官方 JSON 里的 id 常常是字符串形态；一旦上游返回脏数据
    （自建 API 异常体 / 接口变更），直接传给 pydantic 会抛 ValidationError，
    让整条 /点歌 指令变成用户可见的报错。这里统一转成 int 或 None。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            return None
        return int(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return None


def song_page_url(song_id: Any) -> str:
    """由歌曲 id 自造网易云网页外链（id 为空返回空串）。

    优先复用 core/netease/endpoints.song_web_url，拿不到该模块时用等价实现兜底，
    绝不因为一个链接让卡片构造失败。
    """
    text = _as_text(song_id)
    if not text:
        return ""
    fallback = f"https://music.163.com/song?id={text}"
    try:
        from ..netease.endpoints import song_web_url
    except Exception:  # pragma: no cover - core.netease 缺失时走兜底
        return fallback
    try:
        return _as_text(song_web_url(text)) or fallback
    except Exception:  # pragma: no cover - 兜底实现异常
        return fallback


def _field(source: Any, name: str) -> str:
    """从 SongInfo / 映射 / 任意对象里取文本字段。"""
    if source is None:
        return ""
    if isinstance(source, Mapping):
        for key in (name, f"song_{name}"):
            if key in source:
                return _as_text(source.get(key))
        return ""
    return _as_text(getattr(source, name, ""))


def _song_artists(song: Any) -> str:
    """歌手文本：优先 SongInfo.artist_text，其次 artists 列表。"""
    if song is None:
        return ""
    value = getattr(song, "artist_text", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(song, Mapping):
        artists = song.get("artists") or song.get("artist")
        if isinstance(artists, (list, tuple)):
            return " / ".join(item for item in (_as_text(one) for one in artists) if item)
        return _as_text(artists)
    artists = getattr(song, "artists", None)
    if isinstance(artists, (list, tuple)):
        return " / ".join(item for item in (_as_text(one) for one in artists) if item)
    return ""


# --------------------------------------------------------------------- 载荷构造


def _content_text(config: Any, song: Any, payload: Mapping[str, Any]) -> str:
    """卡片 content：歌手 · 专辑 · 来源（与 NeteaseProvider.card_payload 对齐）。"""
    parts = [
        _song_artists(song) or _as_text(payload.get("artist")),
        _field(song, "album") or _as_text(payload.get("album")),
    ]
    if bool(getattr(config, "card_show_source", True)):
        parts.append(SOURCE_LABEL)
    return " · ".join(part for part in parts if part)


def _fallback_payload(song: Any, config: Any) -> dict[str, Any]:
    """不经过 provider 的内置载荷（provider 缺失 / card_payload 异常时使用）。"""
    show_cover = bool(getattr(config, "card_show_cover", True))
    attach_audio = bool(getattr(config, "card_attach_audio", False))
    song_id = _field(song, "id")
    return {
        "kind": "music",
        "type": normalise_card_type(getattr(config, "card_type", "")) or DEFAULT_CARD_TYPE,
        "id": song_id,
        "title": _field(song, "name"),
        "content": "",  # 交给 _content_text 统一拼装
        "image": _field(song, "cover_url") if show_cover else "",
        "url": _field(song, "url") or song_page_url(song_id),
        "audio": _field(song, "audio_url") if attach_audio else "",
    }


def build_payload_from_song(song: Any, cfg: Any = None, *, provider: Any = None) -> dict[str, Any]:
    """取卡片载荷：优先 provider.card_payload(song)，失败则用内置兜底载荷。

    provider.card_payload 是同步方法且按协议不抛异常；为了「卡片构造失败绝不能
    冒泡」这条底线，这里仍然再兜一层。
    """
    config = ensure_runtime_config(cfg)
    payload_fn = getattr(provider, "card_payload", None) if provider is not None else None
    if callable(payload_fn):
        try:
            payload = payload_fn(song)
        except Exception as exc:
            logger.warning("provider.card_payload 失败，改用内置载荷：%r", exc)
        else:
            if isinstance(payload, Mapping) and payload:
                return {str(key): value for key, value in payload.items()}
    return _fallback_payload(song, config)


# --------------------------------------------------------------------- 纯文本兜底


def build_card_text(
    song: Any = None,
    *,
    payload: Mapping[str, Any] | None = None,
    hint: str = "",
) -> str:
    """卡片无法构造时的纯文本内容（歌名 / 歌手 / 专辑 / 时长 / 链接）。"""
    data = payload if isinstance(payload, Mapping) else {}
    name = _as_text(data.get("title")) or _field(song, "name") or UNKNOWN_SONG_NAME
    lines = [f"🎵 {name}"]
    artist = _as_text(data.get("artist")) or _song_artists(song)
    meta = " · ".join(part for part in (artist, _field(song, "album")) if part)
    duration = _field(song, "duration_text")
    if duration and duration != "--:--":
        meta = f"{meta} · {duration}" if meta else duration
    if meta:
        lines.append(meta)
    url = _as_text(data.get("url")) or _field(song, "url") or song_page_url(_as_text(data.get("id")) or _field(song, "id"))
    if url:
        lines.append(url)
    lines.append(f"（{hint or _DEFAULT_HINT}）")
    return "\n".join(line for line in lines if line)


def _hint_for(reason: str, config: Any) -> str:
    hint = _HINT_BY_REASON.get(reason, _DEFAULT_HINT)
    if reason in {"custom_no_audio", "custom_invalid", "music_invalid_id"} and (
        _as_text(getattr(config, "card_fallback", "")) or DEFAULT_FALLBACK_TYPE
    ) == "text":
        return hint.replace("已改为分享链接", "已改为文本")
    return hint


# --------------------------------------------------------------------- 组件构造


def ensure_music_type(component: Any, card_type: str) -> Any:
    """把 card_type 补写到 Music 实例上（见模块头「实测发现」）。

    AstrBot 的 Music 用 pydantic v1/v2 定义，`_type` 不是可构造字段：
    `Music(_type="163")` 之后 `hasattr(comp, "_type")` 是 False，respond stage 的
    Music 校验器与 `toDict()` 都拿不到类型。这里用 object.__setattr__ 直接写实例
    __dict__（pydantic 的 __setattr__ 会拒绝未知字段），失败只记日志、绝不抛异常。
    """
    target = str(card_type or "")
    if not target or _as_text(getattr(component, "_type", "")) == target:
        return component
    try:
        object.__setattr__(component, "_type", target)
    except Exception as exc:  # pragma: no cover - 组件实现异常时兜底
        logger.debug("补写 Music._type 失败（%r），组件类型信息可能缺失。", exc)
        return component
    if _as_text(getattr(component, "_type", "")) != target:
        logger.debug("Music._type 补写后仍不可读，组件类型信息可能缺失。")
    return component


def _construct_music(
    factory: Any,
    *,
    card_type: str,
    song_id: int | None,
    url: str,
    audio: str,
    title: str,
    content: str,
    image: str,
) -> Any | None:
    component = _safe_construct(
        factory.Music,
        _type=card_type,
        id=song_id,
        url=url,
        audio=audio,
        title=title,
        content=content,
        image=image,
    )
    if component is None:
        return None
    return ensure_music_type(component, card_type)


def _construct_share(factory: Any, *, url: str, title: str, content: str, image: str) -> Any | None:
    return _safe_construct(factory.Share, url=url, title=title, content=content, image=image)


def at_component(qq: Any, factory: Any = None) -> Any | None:
    """构造 @ 组件（cfg.reply_with_at 用）；不可用时返回 None。"""
    namespace = resolve_component_factory(factory)
    if namespace is None:
        return None
    target = _as_text(qq)
    builder = getattr(namespace, "At", None)
    if not target or not callable(builder):
        return None
    component = _safe_construct(builder, qq=target)
    if component is None or not component_is_deliverable(component):
        return None
    return component


def plain_component(text: Any, factory: Any = None) -> Any | None:
    """构造纯文本组件（需要把文本拼进消息链时用，例如 reply_with_at 的 @ 前缀）。"""
    namespace = resolve_component_factory(factory)
    if namespace is None:
        return None
    body = "" if text is None else str(text)
    if not body.strip():
        return None
    builder = getattr(namespace, "Plain", None)
    if not callable(builder):
        return None
    component = _safe_construct(builder, text=body)
    if component is None or not component_is_deliverable(component):
        return None
    return component


def card_extra_components(factory: Any, config: Any, audio: str) -> list[Any]:
    """card_attach_audio=True 时附带的语音组件。

    拿不到播放地址时直接跳过（缺少 Record 不影响卡片本身能否发出）。

    注意：开启后 AstrBot 会把音频转成 **未压缩 WAV** 再 base64 塞进消息帧，
    体积可达原文件的十几倍（实测 2.9MB -> 55MB base64），很容易超出 OneBot
    端的 WebSocket 上限并导致连接被断开。默认是关闭的，确实需要时再开，
    并优先考虑短音频。
    """
    namespace = resolve_component_factory(factory)
    if namespace is None or not bool(getattr(config, "card_attach_audio", False)):
        return []
    target = _as_text(audio)
    if not target:
        return []
    builder = getattr(namespace, "Record", None)
    if not callable(builder):
        return []
    component = _safe_construct(builder, file=target)
    if component is None or not component_is_deliverable(component):
        return []
    return [component]


def _degrade(
    factory: Any,
    config: Any,
    *,
    requested: str,
    song: Any,
    payload: Mapping[str, Any],
    reason: str,
    url: str,
    title: str,
    content: str,
    image: str,
    audio: str,
    song_id: int | None,
) -> CardResult:
    """按 cfg.card_fallback 退化：share -> Share 组件；text -> 纯文本。"""
    text = build_card_text(song, payload={**payload, "title": title, "url": url}, hint=_hint_for(reason, config))
    fallback = _as_text(getattr(config, "card_fallback", "")) or DEFAULT_FALLBACK_TYPE
    if fallback not in FALLBACK_TYPES:
        fallback = DEFAULT_FALLBACK_TYPE
    if fallback == "share" and requested != "share":
        share = _construct_share(factory, url=url, title=title, content=content, image=image)
        if share is not None and component_is_deliverable(share):
            return CardResult(
                components=[share],
                text="",
                card_type="share",
                requested_type=requested,
                degraded=True,
                reason=f"fallback_share:{reason}",
                audio_url=audio,
                song_id=song_id,
            )
    plain = _safe_construct(factory.Plain, text=text)
    if plain is not None and component_is_deliverable(plain):
        return CardResult(
            components=[plain],
            text=text,
            card_type="text",
            requested_type=requested,
            degraded=True,
            reason=f"fallback_text:{reason}",
            audio_url=audio,
            song_id=song_id,
        )
    return CardResult(
        components=[],
        text=text,
        card_type="none",
        requested_type=requested,
        degraded=True,
        reason=f"no_component:{reason}",
        audio_url=audio,
        song_id=song_id,
    )


# --------------------------------------------------------------------- 同步主逻辑


def build_card_result_from_payload(
    payload: Mapping[str, Any] | None,
    cfg: Any = None,
    *,
    factory: Any = None,
    song: Any = None,
    card_type: Any = None,
) -> CardResult:
    """由载荷（+ 可选 SongInfo）构造卡片；同步、无 I/O、绝不抛异常。

    card_type 的决定顺序：显式 card_type 参数 > cfg.card_type > 载荷 type > 163。
    cfg 是用户的最终意图；provider 载荷里的 type 只是同一份配置的投影，
    两者不一致时以 cfg 为准并记 debug 日志。
    """
    config = ensure_runtime_config(cfg)
    data: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
    payload_type = normalise_card_type(data.get("type"))
    configured_type = normalise_card_type(getattr(config, "card_type", ""))
    requested = normalise_card_type(card_type) or configured_type or payload_type or DEFAULT_CARD_TYPE
    if payload_type and configured_type and payload_type != configured_type:
        logger.debug(
            "provider 载荷 card_type=%s 与配置 %s 不一致，以配置为准。", payload_type, configured_type
        )

    title = _as_text(data.get("title")) or _field(song, "name") or UNKNOWN_SONG_NAME
    song_id_text = _as_text(data.get("id")) or _field(song, "id")
    song_id = normalise_song_id(song_id_text)
    url = _as_text(data.get("url")) or _field(song, "url") or song_page_url(song_id_text)
    audio = _as_text(data.get("audio")) or _field(song, "audio_url")
    content = _as_text(data.get("content")) or _content_text(config, song, data)
    image = ""
    if bool(getattr(config, "card_show_cover", True)):
        image = _as_text(data.get("image")) or _field(song, "cover_url")

    if not bool(getattr(config, "card_enable", True)):
        # 用户关掉了卡片：不产出任何组件（调用方仍可用 text 兜底）
        return CardResult(
            components=[],
            text="",
            card_type="none",
            requested_type=requested,
            degraded=False,
            reason="card_disabled",
            audio_url=audio,
            song_id=song_id,
        )

    namespace = resolve_component_factory(factory)
    if namespace is None:
        # 没有可用组件（例如脱离 AstrBot 运行）：给出纯文本，绝不返回空组件链
        text = build_card_text(song, payload={**data, "title": title, "url": url})
        return CardResult(
            components=[],
            text=text,
            card_type="none",
            requested_type=requested,
            degraded=True,
            reason="no_component_factory",
            audio_url=audio,
            song_id=song_id,
        )

    component: Any | None = None
    reason = ""
    if requested == "163":
        if song_id is None:
            reason = "music_invalid_id"
            logger.warning("歌曲 id %r 无法转成整数，163 卡片不可用。", song_id_text)
        else:
            component = _construct_music(
                namespace,
                card_type="163",
                song_id=song_id,
                url=url,
                audio=audio,
                title=title,
                content=content,
                image=image,
            )
            if component is None or not component_is_deliverable(component):
                reason = "music_invalid_id"
    elif requested == "custom":
        if not audio:
            reason = "custom_no_audio"
        elif not url:
            reason = "custom_no_url"
        else:
            component = _construct_music(
                namespace,
                card_type="custom",
                song_id=song_id,
                url=url,
                audio=audio,
                title=title,
                content=content,
                image=image,
            )
            if component is None or not component_is_deliverable(component):
                reason = "custom_invalid"
    else:  # share
        component = _construct_share(namespace, url=url, title=title, content=content, image=image)
        if component is None or not component_is_deliverable(component):
            reason = "share_invalid"

    if component is not None and component_is_deliverable(component):
        extras = card_extra_components(namespace, config, audio)
        return CardResult(
            components=[component, *extras],
            text="",
            card_type=requested,
            requested_type=requested,
            degraded=False,
            reason="ok",
            audio_url=audio,
            song_id=song_id,
        )

    return _degrade(
        namespace,
        config,
        requested=requested,
        song=song,
        payload=data,
        reason=reason or "unknown",
        url=url,
        title=title,
        content=content,
        image=image,
        audio=audio,
        song_id=song_id,
    )


# --------------------------------------------------------------------- 异步入口


async def _resolve_audio_url(song: Any, provider: Any, transport: Any) -> str:
    """向 provider 要播放地址（协议外的额外能力，失败返回空串）。"""
    if song is None or provider is None or transport is None:
        return ""
    resolver = getattr(provider, "audio_url", None)
    if not callable(resolver):
        return ""
    try:
        result = resolver(song, transport)
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("获取播放地址失败（不影响卡片）：%r", exc)
        return ""
    return _as_text(result)


def _needs_audio(config: Any, requested: str, kind: Any = "") -> bool:
    """什么时候必须拿到播放地址。

    两件事必须分开，别再耦合：

    1) **Music 卡片的 audio 字段**（就是一个 URL 字符串，体积可忽略）：
       音乐卡片要能点开播放就靠它，所以只要是 music 卡片就必须填。
       曾经这里错误地绑定了 card_attach_audio——那个开关一关，audio 变成空串，
       QQ 那边的卡片就变成"点不动"（能看不能播）。

    2) **额外的 Record 语音组件**：那才是真正的大体积来源 —— AstrBot 会把音频
       转成未压缩 WAV 再 base64（实测一首 2.9MB 的 m4a 变成 55MB），曾经直接把
       OneBot 的 WebSocket 撑爆。它由 card_attach_audio 控制，默认关闭，见
       card_extra_components()。

    另外 custom 卡片必须满足 url + audio + title 非空，否则会被 respond stage
    静默丢弃，所以它同样需要播放地址。
    """
    if _as_text(kind) == "music":
        return True
    return requested == "custom" or bool(getattr(config, "card_attach_audio", False))


async def build_card_result(
    song: Any,
    cfg: Any = None,
    *,
    payload: Mapping[str, Any] | None = None,
    provider: Any = None,
    transport: Any = None,
    factory: Any = None,
    card_type: Any = None,
    audio_url: Any = None,
) -> CardResult:
    """构造卡片（可能为了 music 卡片 / custom 去取一次播放地址）。

    custom 卡片必须满足 `url + audio + title` 三个字段非空，否则会被
    respond stage 静默丢弃；所以这里先尽力取播放地址，仍然为空时由
    build_card_result_from_payload 按 cfg.card_fallback 退化。
    """
    config = ensure_runtime_config(cfg)
    data: dict[str, Any] = (
        dict(payload)
        if isinstance(payload, Mapping) and payload
        else build_payload_from_song(song, config, provider=provider)
    )
    configured_type = normalise_card_type(getattr(config, "card_type", ""))
    requested = (
        normalise_card_type(card_type)
        or configured_type
        or normalise_card_type(data.get("type"))
        or DEFAULT_CARD_TYPE
    )
    if _needs_audio(config, requested, data.get("kind")) and not _as_text(data.get("audio")):
        resolved = _as_text(audio_url) or await _resolve_audio_url(song, provider, transport)
        if resolved:
            data["audio"] = resolved
    return build_card_result_from_payload(data, config, factory=factory, song=song, card_type=card_type)


async def build_card(
    song: Any,
    cfg: Any = None,
    *,
    payload: Mapping[str, Any] | None = None,
    provider: Any = None,
    transport: Any = None,
    factory: Any = None,
    card_type: Any = None,
    audio_url: Any = None,
) -> list[Any]:
    """契约入口：返回可直接喂给 `event.chain_result` 的组件列表。

    需要降级原因或纯文本兜底时改用 build_card_result(...)。
    """
    result = await build_card_result(
        song,
        cfg,
        payload=payload,
        provider=provider,
        transport=transport,
        factory=factory,
        card_type=card_type,
        audio_url=audio_url,
    )
    return list(result.components)
