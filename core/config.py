"""运行期配置：把 AstrBot 传入的配置字典解析成冻结 dataclass。

契约（冻结）：
- RuntimeConfig：冻结 dataclass，字段名与 _conf_schema.json 的叶子键一一对应。
- RuntimeConfig.from_mapping(raw) -> RuntimeConfig：缺失键给默认值，
  类型不符做强制转换并把说明收进 warnings: list[str]，**绝不抛异常**。
- 同时接受嵌套分组字典（AstrBotConfig 的原始结构）与扁平字典。
- is_group_allowed(group_id)：群白名单/黑名单判断。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from .renderer import RenderOptions

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_USER_AGENT",
    "FIELD_SPEC_BY_NAME",
    "FIELD_SPECS",
    "RuntimeConfig",
    "ensure_runtime_config",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
"""默认 UA：官方 API 需要浏览器 UA 才返回完整数据。"""

DEFAULT_ALIASES = ["点歌", "听歌", "music"]


@dataclass(frozen=True)
class FieldSpec:
    """单个配置项的契约：类型、默认值、范围与所属分组。"""

    name: str
    kind: str  # bool | int | float | str | list
    default: Any
    group: str = "basic"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    text: bool = False


def _spec(name, kind, default, group, *, minimum=None, maximum=None, choices=(), secret=False, text=False) -> FieldSpec:
    return FieldSpec(
        name=name,
        kind=kind,
        default=default,
        group=group,
        minimum=minimum,
        maximum=maximum,
        choices=tuple(choices),
        secret=secret,
        text=text,
    )


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # 1) 基础
    _spec("enabled", "bool", True, "basic"),
    _spec("command_aliases", "list", list(DEFAULT_ALIASES), "basic"),
    _spec("cooldown_seconds", "int", 5, "basic", minimum=0, maximum=86400),
    _spec("daily_limit", "int", 0, "basic", minimum=0, maximum=100000),
    _spec("group_whitelist", "list", [], "basic"),
    _spec("group_blacklist", "list", [], "basic"),
    _spec("user_blacklist", "list", [], "basic"),
    # 2) 点歌来源
    _spec("provider", "str", "netease", "source", choices=("netease",)),
    _spec(
        "netease_mode",
        "str",
        "official_direct",
        "source",
        choices=("official_direct", "self_hosted_api"),
    ),
    _spec("netease_api_base", "str", "", "source"),
    _spec("search_limit", "int", 5, "source", minimum=1, maximum=20),
    _spec("pick_strategy", "str", "top", "source", choices=("top", "first")),
    _spec("search_timeout", "float", 8.0, "source", minimum=0.5, maximum=120.0),
    _spec("api_timeout", "float", 10.0, "source", minimum=0.5, maximum=120.0),
    _spec("max_retries", "int", 2, "source", minimum=0, maximum=10),
    _spec("user_agent", "str", DEFAULT_USER_AGENT, "source"),
    _spec("cookie", "str", "", "source", secret=True),
    _spec("fallback_to_plain", "bool", True, "source"),
    # 3) 歌曲卡片
    _spec("card_enable", "bool", True, "card"),
    _spec("card_type", "str", "163", "card", choices=("163", "custom", "share")),
    _spec("card_show_cover", "bool", True, "card"),
    _spec("card_show_source", "bool", True, "card"),
    _spec("card_attach_audio", "bool", False, "card"),
    _spec("card_fallback", "str", "share", "card", choices=("share", "text")),
    # 4) 歌词（t2i）
    _spec("lyrics_enable", "bool", True, "lyrics"),
    _spec("lyrics_t2i", "bool", True, "lyrics"),
    _spec(
        "lyrics_render_mode",
        "str",
        "auto",
        "lyrics",
        choices=("network", "local", "auto"),
    ),
    _spec("lyrics_t2i_endpoint", "str", "", "lyrics"),
    _spec("lyrics_template", "str", "", "lyrics", text=True),
    _spec("lyrics_width", "int", 900, "lyrics", minimum=200, maximum=4096),
    _spec("lyrics_theme", "str", "light", "lyrics", choices=("light", "dark")),
    _spec("lyrics_font_size", "int", 22, "lyrics", minimum=8, maximum=128),
    _spec("lyrics_line_spacing", "float", 1.6, "lyrics", minimum=0.5, maximum=6.0),
    _spec("lyrics_max_lines", "int", 200, "lyrics", minimum=0, maximum=5000),
    _spec("lyrics_show_meta", "bool", True, "lyrics"),
    _spec("lyrics_highlight_translation", "bool", True, "lyrics"),
    _spec("lyrics_fallback_text", "bool", True, "lyrics"),
    # 5) 网易云评论
    _spec("comments_enable", "bool", True, "comments"),
    _spec("comments_t2i", "bool", True, "comments"),
    _spec("comments_count", "int", 10, "comments", minimum=0, maximum=50),
    _spec("comments_sort", "str", "hot", "comments", choices=("hot", "new")),
    _spec("comments_page", "int", 1, "comments", minimum=1, maximum=1000),
    _spec("comments_max_chars", "int", 120, "comments", minimum=0, maximum=2000),
    _spec("comments_show_avatar", "bool", True, "comments"),
    _spec("comments_show_likes", "bool", True, "comments"),
    _spec("comments_show_reply", "bool", False, "comments"),
    _spec("comments_reply_count", "int", 1, "comments", minimum=0, maximum=10),
    _spec("comments_width", "int", 900, "comments", minimum=200, maximum=4096),
    _spec("comments_theme", "str", "light", "comments", choices=("light", "dark")),
    _spec("comments_font_size", "int", 20, "comments", minimum=8, maximum=128),
    _spec("comments_fallback_text", "bool", True, "comments"),
    # 6) 发送与调试
    _spec("send_delay", "float", 0.6, "send", minimum=0.0, maximum=30.0),
    _spec("reply_with_at", "bool", False, "send"),
    _spec("debug_log", "bool", False, "send"),
)
"""全部配置项契约（顺序与 _conf_schema.json 一致）。"""

FIELD_SPEC_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_SPECS}
"""按键名索引的配置项契约。"""

_TRUE_WORDS = {"1", "true", "yes", "y", "on", "enable", "enabled", "开", "是"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "disable", "disabled", "关", "否"}


def _normalise_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return str(value)
        return str(int(value))
    return ""


def _coerce_bool(spec: FieldSpec, value: Any) -> tuple[bool, list[str]]:
    if isinstance(value, bool):
        return value, []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value), [f"配置项 {spec.name} 的值 {value!r} 已转换为布尔值 {bool(value)}。"]
        return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是布尔值，已使用默认值 {spec.default}。"]
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True, [f"配置项 {spec.name} 的值 {value!r} 已转换为 True。"]
        if text in _FALSE_WORDS:
            return False, [f"配置项 {spec.name} 的值 {value!r} 已转换为 False。"]
    return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是布尔值，已使用默认值 {spec.default}。"]


def _coerce_int(spec: FieldSpec, value: Any) -> tuple[int, list[str]]:
    notes: list[str] = []
    if isinstance(value, bool):
        return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是整数，已使用默认值 {spec.default}。"]
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是整数，已使用默认值 {spec.default}。"]
        result = int(value)
        notes.append(f"配置项 {spec.name} 的值 {value!r} 已由浮点数转换为整数 {result}。")
    elif isinstance(value, str):
        text = value.strip()
        try:
            result = int(float(text))
            notes.append(f"配置项 {spec.name} 的值 {value!r} 已转换为整数 {result}。")
        except (TypeError, ValueError):
            return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是整数，已使用默认值 {spec.default}。"]
    else:
        return spec.default, [f"配置项 {spec.name} 的值 {value!r} 不是整数，已使用默认值 {spec.default}。"]
    return _apply_range(spec, result, notes)


def _coerce_float(spec: FieldSpec, value: Any) -> tuple[float, list[str]]:
    notes: list[str] = []
    if isinstance(value, bool):
        return float(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是数字，已使用默认值 {spec.default}。"]
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            result = float(text)
            notes.append(f"配置项 {spec.name} 的值 {value!r} 已转换为数字 {result}。")
        except (TypeError, ValueError):
            return float(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是数字，已使用默认值 {spec.default}。"]
    else:
        return float(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是数字，已使用默认值 {spec.default}。"]
    if result != result or result in (float("inf"), float("-inf")):
        return float(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是有效数字，已使用默认值 {spec.default}。"]
    return _apply_range(spec, result, notes)


def _coerce_str(spec: FieldSpec, value: Any) -> tuple[str, list[str]]:
    notes: list[str] = []
    if isinstance(value, str):
        result = value
    elif isinstance(value, bool):
        return str(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是字符串，已使用默认值 {spec.default!r}。"]
    elif isinstance(value, (int, float)):
        result = str(value)
        notes.append(f"配置项 {spec.name} 的值 {value!r} 已转换为字符串 {result!r}。")
    else:
        return str(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是字符串，已使用默认值 {spec.default!r}。"]
    if spec.choices:
        stripped = result.strip()
        if stripped not in spec.choices:
            return (
                str(spec.default),
                notes
                + [f"配置项 {spec.name} 的值 {stripped!r} 不在可选值 {list(spec.choices)} 中，已使用默认值 {spec.default!r}。"],
            )
        result = stripped
    return result, notes


def _coerce_list(spec: FieldSpec, value: Any) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("，", ",").replace("、", ",").split(",")]
        result = [part for part in parts if part]
        notes.append(f"配置项 {spec.name} 的值 {value!r} 已按逗号拆分为列表 {result!r}。")
        return result, notes
    if isinstance(value, Mapping):
        result = [str(key).strip() for key in value.keys() if str(key).strip()]
        notes.append(f"配置项 {spec.name} 的值已由字典键转换为列表 {result!r}。")
        return result, notes
    if isinstance(value, (list, tuple, set, frozenset)):
        result = []
        for item in value:
            if isinstance(item, bool) or item is None:
                notes.append(f"配置项 {spec.name} 中的元素 {item!r} 已忽略。")
                continue
            if isinstance(item, (str, int, float)):
                text = str(item).strip()
                if text:
                    result.append(text)
            else:
                notes.append(f"配置项 {spec.name} 中的元素 {item!r} 不是字符串，已忽略。")
        return result, notes
    return list(spec.default), [f"配置项 {spec.name} 的值 {value!r} 不是列表，已使用默认值 {spec.default!r}。"]


def _apply_range(spec: FieldSpec, value: Any, notes: list[str]) -> tuple[Any, list[str]]:
    if spec.minimum is not None and value < spec.minimum:
        notes.append(f"配置项 {spec.name} 的值 {value!r} 小于下限 {spec.minimum}，已修正为 {spec.minimum}。")
        value = spec.minimum
    if spec.maximum is not None and value > spec.maximum:
        notes.append(f"配置项 {spec.name} 的值 {value!r} 大于上限 {spec.maximum}，已修正为 {spec.maximum}。")
        value = spec.maximum
    if spec.kind == "int":
        value = int(value)
    elif spec.kind == "float":
        value = float(value)
    return value, notes


_MISSING = object()
"""哨兵：区分「键缺失」与「键存在但值为 None」。"""


_COERCERS = {
    "bool": _coerce_bool,
    "int": _coerce_int,
    "float": _coerce_float,
    "str": _coerce_str,
    "list": _coerce_list,
}


def _flatten(raw: Mapping[str, Any], out: dict[str, Any]) -> None:
    """递归摊平嵌套配置；叶子键名直接作为扁平键。"""
    for key, value in raw.items():
        name = str(key)
        if isinstance(value, Mapping):
            _flatten(value, out)
        else:
            out[name] = value


def _default_values() -> dict[str, Any]:
    return {spec.name: spec.default for spec in FIELD_SPECS}


@dataclass(frozen=True)
class RuntimeConfig:
    """已解析、已纠错的运行期配置（字段与 _conf_schema.json 叶子键同名）。"""

    # 1) 基础
    enabled: bool = True
    command_aliases: list[str] = field(default_factory=lambda: list(DEFAULT_ALIASES))
    cooldown_seconds: int = 5
    daily_limit: int = 0
    group_whitelist: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)
    user_blacklist: list[str] = field(default_factory=list)
    # 2) 点歌来源
    provider: str = "netease"
    netease_mode: str = "official_direct"
    netease_api_base: str = ""
    search_limit: int = 5
    pick_strategy: str = "top"
    search_timeout: float = 8.0
    api_timeout: float = 10.0
    max_retries: int = 2
    user_agent: str = DEFAULT_USER_AGENT
    cookie: str = ""
    fallback_to_plain: bool = True
    # 3) 歌曲卡片
    card_enable: bool = True
    card_type: str = "163"
    card_show_cover: bool = True
    card_show_source: bool = True
    card_attach_audio: bool = False
    card_fallback: str = "share"
    # 4) 歌词（t2i）
    lyrics_enable: bool = True
    lyrics_t2i: bool = True
    lyrics_render_mode: str = "auto"
    lyrics_t2i_endpoint: str = ""
    lyrics_template: str = ""
    lyrics_width: int = 900
    lyrics_theme: str = "light"
    lyrics_font_size: int = 22
    lyrics_line_spacing: float = 1.6
    lyrics_max_lines: int = 200
    lyrics_show_meta: bool = True
    lyrics_highlight_translation: bool = True
    lyrics_fallback_text: bool = True
    # 5) 网易云评论
    comments_enable: bool = True
    comments_t2i: bool = True
    comments_count: int = 10
    comments_sort: str = "hot"
    comments_page: int = 1
    comments_max_chars: int = 120
    comments_show_avatar: bool = True
    comments_show_likes: bool = True
    comments_show_reply: bool = False
    comments_reply_count: int = 1
    comments_width: int = 900
    comments_theme: str = "light"
    comments_font_size: int = 20
    comments_fallback_text: bool = True
    # 6) 发送与调试
    send_delay: float = 0.6
    reply_with_at: bool = False
    debug_log: bool = False
    # 解析过程记录（不是配置项）
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> RuntimeConfig:
        """解析任意配置映射；非法输入被纠正并记录在 warnings，绝不抛异常。"""
        warnings: list[str] = []
        flat: dict[str, Any] = {}
        if raw is None:
            warnings.append("未提供插件配置，全部使用默认值。")
        elif isinstance(raw, Mapping):
            try:
                _flatten(raw, flat)
            except Exception as exc:  # pragma: no cover - 极端异常兜底
                warnings.append(f"解析插件配置失败（{exc}），全部使用默认值。")
                flat = {}
        else:
            warnings.append(
                f"插件配置类型异常（{type(raw).__name__}），全部使用默认值。"
            )
        values: dict[str, Any] = {}
        for spec in FIELD_SPECS:
            value = flat.get(spec.name, _MISSING)
            if value is _MISSING:
                values[spec.name] = (
                    list(spec.default) if isinstance(spec.default, list) else spec.default
                )
                continue
            if value is None:
                values[spec.name] = (
                    list(spec.default) if isinstance(spec.default, list) else spec.default
                )
                warnings.append(
                    f"配置项 {spec.name} 为 null，已使用默认值 {spec.default!r}。"
                )
                continue
            try:
                converted, notes = _COERCERS[spec.kind](spec, value)
            except Exception as exc:  # pragma: no cover - 兜底
                converted, notes = spec.default, [
                    f"配置项 {spec.name} 解析失败（{exc}），已使用默认值 {spec.default!r}。"
                ]
            values[spec.name] = converted
            warnings.extend(notes)
        values["warnings"] = warnings
        return cls(**values)

    # ------------------------------------------------------------ 便捷访问

    @property
    def aliases(self) -> list[str]:
        """去掉空白的指令别名。"""
        return [alias.strip() for alias in self.command_aliases if str(alias).strip()]

    def is_group_allowed(self, group_id: Any) -> bool:
        """群白名单/黑名单判断。

        - group_id 为空（私聊等非群消息）时不受群名单限制，返回 True；
        - 命中黑名单返回 False（黑名单中可用 "*" 表示全部群）；
        - 白名单非空时必须命中才返回 True（可用 "*" 表示全部群）。
        """
        gid = _normalise_id(group_id)
        blacklist = {_normalise_id(item) for item in self.group_blacklist}
        blacklist.discard("")
        if gid and (gid in blacklist or "*" in blacklist):
            return False
        if not gid:
            return True
        whitelist = {_normalise_id(item) for item in self.group_whitelist}
        whitelist.discard("")
        if not whitelist or "*" in whitelist:
            return True
        return gid in whitelist

    def is_user_allowed(self, user_id: Any) -> bool:
        """用户黑名单判断（可用 "*" 表示全部用户）。"""
        uid = _normalise_id(user_id)
        blacklist = {_normalise_id(item) for item in self.user_blacklist}
        blacklist.discard("")
        if not uid:
            return "*" not in blacklist
        return uid not in blacklist and "*" not in blacklist

    def lyrics_render_options(self, **overrides: Any) -> RenderOptions:
        """由歌词配置生成渲染参数（供 core/t2i 使用）。"""
        options = RenderOptions.from_mapping(
            {
                "width": self.lyrics_width,
                "theme": self.lyrics_theme,
                "font_size": self.lyrics_font_size,
                "line_spacing": self.lyrics_line_spacing,
                "max_lines": self.lyrics_max_lines,
                "endpoint": self.lyrics_t2i_endpoint,
                "mode": self.lyrics_render_mode,
                "timeout": self.api_timeout,
            }
        )
        return RenderOptions.from_mapping(options.to_dict(), **overrides)

    def comments_render_options(self, **overrides: Any) -> RenderOptions:
        """由评论配置生成渲染参数（评论无独立端点/模式，复用歌词的）。"""
        options = RenderOptions.from_mapping(
            {
                "width": self.comments_width,
                "theme": self.comments_theme,
                "font_size": self.comments_font_size,
                "endpoint": self.lyrics_t2i_endpoint,
                "mode": self.lyrics_render_mode,
                "timeout": self.api_timeout,
            }
        )
        return RenderOptions.from_mapping(options.to_dict(), **overrides)

    def to_dict(self) -> dict[str, Any]:
        """扁平字典（含 warnings 副本）。"""
        data: dict[str, Any] = {}
        for spec in fields(type(self)):
            name = spec.name
            value = getattr(self, name)
            data[name] = list(value) if isinstance(value, list) else value
        return data

    def describe(self) -> str:
        """一行摘要（调试日志用，不包含 cookie）。"""
        return (
            f"provider={self.provider} mode={self.netease_mode} "
            f"card={self.card_enable}/{self.card_type} "
            f"lyrics={self.lyrics_enable}/{self.lyrics_t2i}/{self.lyrics_render_mode} "
            f"comments={self.comments_enable}/{self.comments_t2i} "
            f"warnings={len(self.warnings)}"
        )


def ensure_runtime_config(raw: Any) -> RuntimeConfig:
    """把任意对象变成 RuntimeConfig（已是则原样返回）。"""
    if isinstance(raw, RuntimeConfig):
        return raw
    return RuntimeConfig.from_mapping(raw if isinstance(raw, Mapping) else None)
