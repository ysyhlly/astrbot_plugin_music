"""结构化日志与脱敏 helper（受 cfg.debug_log 控制）。

设计要点
--------
- 日志器名统一在 `astrbot_plugin_music` 命名空间下，便于 AstrBot 面板按插件调级别；
- 脱敏是**默认行为**：cookie / token / authorization / password / secret / api_key
  在写入日志前一律替换成 ***，无论是键值对文本还是整体映射；
- debug 级别日志受 cfg.debug_log 控制（AstrBot 传来的配置可能是嵌套字典，
  因此按键名做深度查找，与 RuntimeConfig.from_mapping 的扁平化口径一致）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "PLUGIN_LOGGER_NAME",
    "SENSITIVE_KEYS",
    "config_value",
    "debug_enabled",
    "describe_config",
    "get_logger",
    "log_debug",
    "log_info",
    "log_warning",
    "redact",
    "redact_mapping",
    "warn_once",
]

PLUGIN_LOGGER_NAME = "astrbot_plugin_music"
"""插件日志器命名空间。"""

SENSITIVE_KEYS: tuple[str, ...] = (
    "cookie",
    "music_u",
    "token",
    "authorization",
    "auth",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "session",
)
"""需要脱敏的键名（小写，子串匹配）。"""

_MASK = "***"

_KEY_ALTERNATION = "|".join(re.escape(key) for key in SENSITIVE_KEYS)
_PAIR_PATTERN = re.compile(
    rf"(?i)(\b(?:{_KEY_ALTERNATION})\b)\s*[:=]\s*([^\s,;\"']+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_TRUE_WORDS = {"1", "true", "yes", "y", "on", "enable", "enabled", "开", "是"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "disable", "disabled", "关", "否"}

_warned: set[str] = set()


def get_logger(name: str | None = None) -> logging.Logger:
    """取插件日志器（带子模块名可选）。"""
    if name:
        return logging.getLogger(f"{PLUGIN_LOGGER_NAME}.{name}")
    return logging.getLogger(PLUGIN_LOGGER_NAME)


# --------------------------------------------------------------------- 脱敏


def redact(value: Any) -> str:
    """把文本里的敏感片段替换成 ***（cookie/token/authorization...）。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    text = _BEARER_PATTERN.sub(f"Bearer {_MASK}", text)
    return _PAIR_PATTERN.sub(lambda match: f"{match.group(1)}={_MASK}", text)


def _is_sensitive_key(key: Any) -> bool:
    name = str(key or "").strip().lower()
    return any(item in name for item in SENSITIVE_KEYS)


def redact_mapping(data: Any, *, depth: int = 0) -> dict[str, Any]:
    """递归脱敏映射（值被替换成 ***，键保持原样）。"""
    if not isinstance(data, Mapping) or depth > 4:
        return {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if _is_sensitive_key(name):
            result[name] = _MASK
        elif isinstance(value, Mapping):
            result[name] = redact_mapping(value, depth=depth + 1)
        elif isinstance(value, str):
            result[name] = redact(value)
        else:
            result[name] = value
    return result


# --------------------------------------------------------------------- 配置读取


def config_value(raw: Any, key: str, default: Any = None) -> Any:
    """在（可能嵌套的）配置里按键名深度查找叶子值。

    AstrBot 传给插件的配置是按 schema 分组嵌套的字典；叶子键名唯一，
    因此深度优先查找与 RuntimeConfig.from_mapping 的扁平化口径一致。
    """
    if raw is None or not key:
        return default
    if isinstance(raw, Mapping):
        if key in raw and not isinstance(raw.get(key), Mapping):
            return raw.get(key)
        for value in raw.values():
            if isinstance(value, Mapping):
                found = config_value(value, key, _MISSING)
                if found is not _MISSING:
                    return found
        return default
    value = getattr(raw, key, _MISSING)
    return default if value is _MISSING else value


_MISSING = object()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def debug_enabled(cfg: Any = None) -> bool:
    """cfg.debug_log 是否开启（接受 RuntimeConfig / 嵌套配置 / 布尔）。"""
    if isinstance(cfg, bool):
        return cfg
    value = config_value(cfg, "debug_log", False)
    return _as_bool(value, False)


# --------------------------------------------------------------------- 日志输出


def _prepare(message: str, args: tuple[Any, ...]) -> str:
    """先按 %-格式拼装，再脱敏。

    顺序很重要：先脱敏会把 "@@BT@@%s@@BT@@" 这类占位符一并吃掉（例如
    @@BT@@log_debug(cfg, "cookie=%s", value)@@BT@@），导致 logging 抛
    "not all arguments converted"。拼装后再脱敏既安全又不会破坏日志格式。
    """
    text = message if isinstance(message, str) else str(message)
    if not args:
        return redact(text)
    try:
        return redact(text % args)
    except Exception:  # pragma: no cover - 参数与占位符不匹配时兜底
        return redact(" ".join([text, *(str(item) for item in args)]))


def log_debug(cfg: Any, message: str, *args: Any, logger: logging.Logger | None = None) -> None:
    """受 cfg.debug_log 控制的 debug 日志（内容与参数都会脱敏）。"""
    if not debug_enabled(cfg):
        return
    (logger or get_logger()).debug(_prepare(message, args))


def log_info(cfg: Any, message: str, *args: Any, logger: logging.Logger | None = None) -> None:
    """info 日志；debug_log 打开时用 info 级，否则降到 debug 级（默认不刷屏）。"""
    target = logger or get_logger()
    prepared = _prepare(message, args)
    if debug_enabled(cfg):
        target.info(prepared)
    else:
        target.debug(prepared)


def log_warning(message: str, *args: Any, logger: logging.Logger | None = None) -> None:
    """warning 日志（内容与参数都会脱敏）。"""
    (logger or get_logger()).warning(_prepare(message, args))


def warn_once(key: str, message: str, *args: Any, logger: logging.Logger | None = None) -> None:
    """同一个 key 只警告一次，避免刷屏。"""
    if key in _warned:
        return
    _warned.add(key)
    log_warning(message, *args, logger=logger)


def describe_config(cfg: Any) -> str:
    """一行配置摘要（不含 cookie 等敏感值）。"""
    describe = getattr(cfg, "describe", None)
    if callable(describe):
        try:
            return redact(str(describe()))
        except Exception:  # pragma: no cover - 兜底
            pass
    keys = (
        "enabled",
        "provider",
        "netease_mode",
        "card_enable",
        "card_type",
        "card_fallback",
        "lyrics_enable",
        "lyrics_t2i",
        "lyrics_render_mode",
        "comments_enable",
        "comments_t2i",
        "debug_log",
    )
    parts = [f"{key}={config_value(cfg, key, None)!r}" for key in keys if config_value(cfg, key, None) is not None]
    return " ".join(parts)
