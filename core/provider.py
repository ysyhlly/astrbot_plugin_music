"""Provider 协议、卡片载荷结构与注册表。

契约（冻结）：
- MusicProvider: key / display_name / async search(query, transport) /
  async lyrics(song, transport) / async comments(song, transport, limit, offset) /
  card_payload(song)（同步方法，返回普通 dict）。
- PROVIDER_REGISTRY: dict[str, MusicProvider]（注册表本体在本模块）。
- get_provider(key) -> MusicProvider | None：netease 的真实实现在
  core/netease/provider.py，本模块用函数体内延迟 import 接入以避免循环依赖；
  core.netease 不存在或导入失败时返回 None，绝不抛 ImportError。
- transport 是 aiohttp.ClientSession 的鸭子类型子集（见 Transport），
  由调用方负责创建/关闭，provider 不得关闭它。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, TypedDict, runtime_checkable

from .models import CommentPage, Lyric, MusicQuery, SongInfo

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROVIDER_KEY",
    "PROVIDER_REGISTRY",
    "CardPayload",
    "MusicProvider",
    "Transport",
    "available_providers",
    "get_provider",
    "register_provider",
    "resolve_provider_class",
]

DEFAULT_PROVIDER_KEY = "netease"
"""默认点歌来源。"""

_PACKAGE = __name__.rpartition(".")[0] or "core"
"""本模块所属包名（core 或 data.plugins.<plugin>.core），用于延迟 import。"""

_LAZY_PROVIDERS: dict[str, tuple[str, str]] = {
    # key: (相对本包的模块后缀, 约定的 provider 类名)
    DEFAULT_PROVIDER_KEY: ("netease.provider", "NeteaseProvider"),
}
"""惰性 provider 表：模块不存在时 get_provider 返回 None。"""


class CardPayload(TypedDict, total=False):
    """歌曲卡片载荷（字典键与 AstrBot Music/Share 组件参数对齐）。

    kind="music" 时使用 type/id/title/content/image/url/audio 构造 Music 组件；
    kind="share" 时使用 url/title/content/image 构造 Share 组件。
    """

    kind: str
    type: str
    id: str
    title: str
    content: str
    image: str
    url: str
    audio: str


@runtime_checkable
class Transport(Protocol):
    """HTTP 传输层：aiohttp.ClientSession 的鸭子类型子集。

    provider 只使用 get/post/request 返回的异步上下文管理器，
    不得调用 close()，生命周期由调用方管理。
    """

    def get(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - 协议声明
        ...

    def post(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - 协议声明
        ...

    def request(
        self, method: str, url: str, **kwargs: Any
    ) -> Any:  # pragma: no cover - 协议声明
        ...


@runtime_checkable
class MusicProvider(Protocol):
    """点歌来源协议。所有方法都不得向外抛异常（失败返回空结果/None）。"""

    key: str
    """provider 唯一键，例如 "netease"。"""

    display_name: str
    """用于日志 / 卡片展示的来源名称。"""

    async def search(self, query: MusicQuery, transport: Transport) -> list[SongInfo]:
        """按关键词搜索歌曲，失败返回空列表。"""
        ...

    async def lyrics(self, song: SongInfo, transport: Transport) -> Lyric | None:
        """获取歌词，无歌词或失败返回 None。"""
        ...

    async def comments(
        self,
        song: SongInfo,
        transport: Transport,
        limit: int = 20,
        offset: int = 0,
    ) -> CommentPage | None:
        """获取评论分页，失败返回 None。"""
        ...

    def card_payload(self, song: SongInfo) -> CardPayload:
        """构造歌曲卡片载荷（同步方法，返回普通 dict）。"""
        ...


PROVIDER_REGISTRY: dict[str, MusicProvider] = {}
"""已注册的 provider（key 小写）。"""

_INSTANCE_ATTRS = ("PROVIDER", "provider", "PROVIDER_INSTANCE", "instance")
_FACTORY_ATTRS = ("PROVIDER_FACTORY", "create_provider", "build_provider", "make_provider")
_REGISTER_ATTRS = ("register_provider", "register", "setup")


def _normalise_key(key: Any) -> str:
    return str(key or "").strip().lower()


def _looks_like_provider(obj: Any) -> bool:
    """鸭子类型校验：至少要有 key/search/card_payload。"""
    if obj is None or isinstance(obj, type):
        return False
    return (
        isinstance(getattr(obj, "key", None), str)
        and bool(str(getattr(obj, "key", "")).strip())
        and callable(getattr(obj, "search", None))
        and callable(getattr(obj, "card_payload", None))
    )


def register_provider(provider: Any) -> MusicProvider | None:
    """注册 provider（幂等，同 key 覆盖）；不合法对象返回 None。"""
    if not _looks_like_provider(provider):
        logger.warning("拒绝注册非法 provider：%r", type(provider).__name__)
        return None
    key = _normalise_key(provider.key)
    PROVIDER_REGISTRY[key] = provider
    return provider


def resolve_provider_class(key: Any) -> type | None:
    """解析惰性 provider 的类对象（找不到返回 None，不抛异常）。"""
    spec = _LAZY_PROVIDERS.get(_normalise_key(key))
    if spec is None:
        return None
    module = _import_optional(f"{_PACKAGE}.{spec[0]}")
    if module is None:
        return None
    candidate = getattr(module, spec[1], None)
    return candidate if isinstance(candidate, type) else None


def _import_optional(module_name: str) -> Any:
    """尽力导入模块；缺失或导入失败时返回 None（不抛 ImportError）。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        logger.debug("可选模块 %s 不可用：%s", module_name, exc)
        return None
    except Exception as exc:  # pragma: no cover - 模块自身初始化异常
        logger.warning("加载可选模块 %s 失败：%s", module_name, exc)
        return None


def _resolve_from_module(module: Any, key: str) -> MusicProvider | None:
    """按约定优先级从模块里取出 provider 实例。"""
    for attr in _INSTANCE_ATTRS:
        candidate = getattr(module, attr, None)
        if _looks_like_provider(candidate):
            return candidate
    for attr in _REGISTER_ATTRS:
        func = getattr(module, attr, None)
        if callable(func):
            try:
                candidate = func()
            except Exception as exc:
                logger.debug("%s.%s() 调用失败：%s", module.__name__, attr, exc)
                candidate = None
            if _looks_like_provider(candidate):
                return candidate
            if key in PROVIDER_REGISTRY:
                return PROVIDER_REGISTRY[key]
    for attr in _FACTORY_ATTRS:
        func = getattr(module, attr, None)
        if callable(func):
            try:
                candidate = func()
            except Exception as exc:
                logger.debug("%s.%s() 调用失败：%s", module.__name__, attr, exc)
                candidate = None
            if _looks_like_provider(candidate):
                return candidate
    spec = _LAZY_PROVIDERS.get(key)
    class_names = ("Provider",) if spec is None else (spec[1], "Provider")
    for class_name in class_names:
        cls = getattr(module, class_name, None)
        if isinstance(cls, type):
            try:
                candidate = cls()
            except Exception as exc:
                logger.debug("%s() 实例化失败：%s", class_name, exc)
                continue
            if _looks_like_provider(candidate):
                return candidate
    return None


def get_provider(key: Any) -> MusicProvider | None:
    """按 key 取 provider；不可用时返回 None。

    优先查已注册表；netease 走延迟 import（core.netease 不存在时返回 None）。
    """
    normalised = _normalise_key(key)
    if not normalised:
        return None
    registered = PROVIDER_REGISTRY.get(normalised)
    if registered is not None:
        return registered
    spec = _LAZY_PROVIDERS.get(normalised)
    if spec is None:
        return None
    module = _import_optional(f"{_PACKAGE}.{spec[0]}")
    if module is None:
        # 模块缺失，但可能已被别处注册
        return PROVIDER_REGISTRY.get(normalised)
    provider = _resolve_from_module(module, normalised)
    if provider is None:
        provider = PROVIDER_REGISTRY.get(normalised)
    if provider is not None:
        PROVIDER_REGISTRY.setdefault(normalised, provider)
    return provider


def available_providers() -> tuple[str, ...]:
    """当前已实例化注册的 provider key 列表。"""
    return tuple(sorted(PROVIDER_REGISTRY))
