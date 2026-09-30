"""RuntimeConfig.from_mapping 行为：默认值、类型纠正、警告收集、群名单判断。"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest

from core.config import (
    DEFAULT_ALIASES,
    DEFAULT_USER_AGENT,
    FIELD_SPECS,
    RuntimeConfig,
    ensure_runtime_config,
)
from core.renderer import RenderOptions

DOCUMENTED_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "command_aliases": DEFAULT_ALIASES,
    "cooldown_seconds": 5,
    "daily_limit": 0,
    "group_whitelist": [],
    "group_blacklist": [],
    "user_blacklist": [],
    "provider": "netease",
    "netease_mode": "official_direct",
    "netease_api_base": "",
    "search_limit": 5,
    "pick_strategy": "top",
    "search_timeout": 8.0,
    "api_timeout": 10.0,
    "max_retries": 2,
    "user_agent": DEFAULT_USER_AGENT,
    "cookie": "",
    "fallback_to_plain": True,
    "card_enable": True,
    "card_type": "163",
    "card_show_cover": True,
    "card_show_source": True,
    "card_attach_audio": False,
    "card_fallback": "share",
    "lyrics_enable": True,
    "lyrics_t2i": True,
    "lyrics_render_mode": "auto",
    "lyrics_t2i_endpoint": "",
    "lyrics_template": "",
    "lyrics_width": 900,
    "lyrics_theme": "light",
    "lyrics_font_size": 22,
    "lyrics_line_spacing": 1.6,
    "lyrics_max_lines": 200,
    "lyrics_show_meta": True,
    "lyrics_highlight_translation": True,
    "lyrics_fallback_text": True,
    "comments_enable": True,
    "comments_t2i": True,
    "comments_count": 10,
    "comments_sort": "hot",
    "comments_page": 1,
    "comments_max_chars": 120,
    "comments_show_avatar": True,
    "comments_show_likes": True,
    "comments_show_reply": False,
    "comments_reply_count": 1,
    "comments_width": 900,
    "comments_theme": "light",
    "comments_font_size": 20,
    "comments_fallback_text": True,
    "send_delay": 0.6,
    "reply_with_at": False,
    "debug_log": False,
}


def test_documented_defaults_cover_every_field() -> None:
    assert set(DOCUMENTED_DEFAULTS) == {spec.name for spec in FIELD_SPECS}
    assert set(DOCUMENTED_DEFAULTS) == {
        name
        for name in RuntimeConfig.from_mapping({}).to_dict()
        if name != "warnings"
    }


def test_from_empty_mapping_yields_documented_defaults() -> None:
    config = RuntimeConfig.from_mapping({})
    assert config.warnings == []
    for name, expected in DOCUMENTED_DEFAULTS.items():
        actual = getattr(config, name)
        assert actual == expected, f"{name} 默认值应为 {expected!r}，实际 {actual!r}"
        assert type(actual) is type(expected), f"{name} 默认值类型应为 {type(expected)}"


def test_field_specs_defaults_match_dataclass_defaults() -> None:
    instance = RuntimeConfig()
    for spec in FIELD_SPECS:
        actual = getattr(instance, spec.name)
        assert actual == spec.default, f"{spec.name} 契约默认值与 dataclass 默认值不一致"
        assert type(actual) is type(spec.default), f"{spec.name} 默认值类型不一致"


def test_runtime_config_is_frozen() -> None:
    config = RuntimeConfig.from_mapping({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.cooldown_seconds = 1  # type: ignore[misc]
    assert dataclasses.is_dataclass(config)


def test_mutating_input_mapping_does_not_leak_into_defaults() -> None:
    first = RuntimeConfig.from_mapping({})
    first.command_aliases.append("污染")
    first.group_whitelist.append("123")
    second = RuntimeConfig.from_mapping({})
    assert second.command_aliases == DEFAULT_ALIASES
    assert second.group_whitelist == []


def test_invalid_int_is_corrected_and_warned() -> None:
    config = RuntimeConfig.from_mapping({"cooldown_seconds": "abc"})
    assert config.cooldown_seconds == 5
    assert config.warnings
    assert any("cooldown_seconds" in message for message in config.warnings)


def test_numeric_string_is_coerced_to_int() -> None:
    config = RuntimeConfig.from_mapping({"cooldown_seconds": "12"})
    assert config.cooldown_seconds == 12
    assert any("cooldown_seconds" in message for message in config.warnings)


def test_out_of_range_values_are_clamped() -> None:
    config = RuntimeConfig.from_mapping({"search_limit": 999, "comments_count": -3})
    assert config.search_limit == 20
    assert config.comments_count == 0
    assert any("search_limit" in message for message in config.warnings)
    assert any("comments_count" in message for message in config.warnings)


def test_bool_coercion_accepts_common_spellings() -> None:
    config = RuntimeConfig.from_mapping(
        {"enabled": "no", "fallback_to_plain": 0, "card_enable": "yes", "debug_log": 1}
    )
    assert config.enabled is False
    assert config.fallback_to_plain is False
    assert config.card_enable is True
    assert config.debug_log is True


def test_bool_type_error_falls_back_to_default() -> None:
    config = RuntimeConfig.from_mapping({"enabled": "perhaps"})
    assert config.enabled is True
    assert any("enabled" in message for message in config.warnings)


def test_float_coercion_and_range() -> None:
    config = RuntimeConfig.from_mapping({"send_delay": "1.5", "api_timeout": 0.0})
    assert config.send_delay == 1.5
    assert config.api_timeout == 0.5  # 下限修正
    assert any("api_timeout" in message for message in config.warnings)


def test_invalid_float_falls_back_to_default() -> None:
    config = RuntimeConfig.from_mapping({"search_timeout": "很快"})
    assert config.search_timeout == 8.0
    assert any("search_timeout" in message for message in config.warnings)


def test_invalid_choice_falls_back_to_default() -> None:
    config = RuntimeConfig.from_mapping(
        {"provider": "qqmusic", "lyrics_theme": "midnight", "pick_strategy": "random"}
    )
    assert config.provider == "netease"
    assert config.lyrics_theme == "light"
    assert config.pick_strategy == "top"
    assert len(config.warnings) >= 3


def test_list_coercion_accepts_comma_string_and_ignores_junk() -> None:
    config = RuntimeConfig.from_mapping(
        {
            "command_aliases": "点歌, 听歌，music",
            "group_whitelist": ["123", 456, None, "789"],
        }
    )
    assert config.command_aliases == ["点歌", "听歌", "music"]
    assert config.group_whitelist == ["123", "456", "789"]
    assert config.warnings  # 字符串拆分与非法元素都要有说明


def test_list_type_error_falls_back_to_default() -> None:
    config = RuntimeConfig.from_mapping({"group_whitelist": 123})
    assert config.group_whitelist == []
    assert any("group_whitelist" in message for message in config.warnings)


def test_nested_group_mapping_is_supported() -> None:
    config = RuntimeConfig.from_mapping(
        {
            "basic": {"enabled": False, "cooldown_seconds": 30},
            "lyrics": {"lyrics_width": 800, "lyrics_theme": "dark"},
            "comments": {"comments_count": 20},
        }
    )
    assert config.enabled is False
    assert config.cooldown_seconds == 30
    assert config.lyrics_width == 800
    assert config.lyrics_theme == "dark"
    assert config.comments_count == 20
    assert config.warnings == []


def test_flat_and_nested_mapping_are_equivalent() -> None:
    flat = RuntimeConfig.from_mapping({"lyrics_width": 700, "comments_sort": "new"})
    nested = RuntimeConfig.from_mapping(
        {"lyrics": {"lyrics_width": 700}, "comments": {"comments_sort": "new"}}
    )
    assert flat.to_dict() == nested.to_dict()


def test_unknown_keys_are_ignored_without_warning() -> None:
    config = RuntimeConfig.from_mapping({"not_a_real_key": 1, "lyrics": {"nope": 2}})
    assert config.warnings == []
    assert config.to_dict()["lyrics_width"] == 900


@pytest.mark.parametrize("raw", [None, 123, "config", [1, 2, 3]])
def test_non_mapping_input_never_raises(raw: Any) -> None:
    config = RuntimeConfig.from_mapping(raw)  # type: ignore[arg-type]
    assert config.cooldown_seconds == 5
    assert config.warnings


def test_none_value_uses_default_with_warning() -> None:
    config = RuntimeConfig.from_mapping({"cooldown_seconds": None})
    assert config.cooldown_seconds == 5
    assert any("null" in message for message in config.warnings)


def test_weird_types_never_raise() -> None:
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    raw: dict[str, Any] = {
        spec.name: Weird()
        for spec in FIELD_SPECS
    }
    config = RuntimeConfig.from_mapping(raw)
    for spec in FIELD_SPECS:
        assert getattr(config, spec.name) == (
            list(spec.default) if isinstance(spec.default, list) else spec.default
        )
    assert len(config.warnings) == len(FIELD_SPECS)


def test_ensure_runtime_config_accepts_various_inputs() -> None:
    assert isinstance(ensure_runtime_config(None), RuntimeConfig)
    assert isinstance(ensure_runtime_config({"enabled": False}), RuntimeConfig)
    existing = RuntimeConfig.from_mapping({})
    assert ensure_runtime_config(existing) is existing


def test_aliases_property_filters_blank_entries() -> None:
    config = RuntimeConfig.from_mapping({"command_aliases": ["点歌", "  ", "music"]})
    assert config.aliases == ["点歌", "music"]


def test_is_group_allowed_without_lists() -> None:
    config = RuntimeConfig.from_mapping({})
    assert config.is_group_allowed("123456") is True
    assert config.is_group_allowed(123456) is True
    assert config.is_group_allowed("") is True
    assert config.is_group_allowed(None) is True


def test_is_group_allowed_honours_whitelist() -> None:
    config = RuntimeConfig.from_mapping({"group_whitelist": ["111", 222]})
    assert config.is_group_allowed("111") is True
    assert config.is_group_allowed(222) is True
    assert config.is_group_allowed("333") is False
    assert config.is_group_allowed("") is True  # 私聊不受群白名单限制


def test_is_group_allowed_honours_blacklist_and_wildcard() -> None:
    config = RuntimeConfig.from_mapping({"group_blacklist": ["111"]})
    assert config.is_group_allowed("111") is False
    assert config.is_group_allowed("222") is True

    whitelist_all = RuntimeConfig.from_mapping({"group_whitelist": ["*"]})
    assert whitelist_all.is_group_allowed("999") is True

    blacklist_all = RuntimeConfig.from_mapping({"group_blacklist": ["*"]})
    assert blacklist_all.is_group_allowed("999") is False


def test_blacklist_wins_over_whitelist() -> None:
    config = RuntimeConfig.from_mapping(
        {"group_whitelist": ["111"], "group_blacklist": ["111"]}
    )
    assert config.is_group_allowed("111") is False


def test_is_user_allowed() -> None:
    config = RuntimeConfig.from_mapping({"user_blacklist": ["42"]})
    assert config.is_user_allowed("42") is False
    assert config.is_user_allowed(42) is False
    assert config.is_user_allowed("43") is True
    assert RuntimeConfig.from_mapping({"user_blacklist": ["*"]}).is_user_allowed("1") is False


def test_render_options_helpers() -> None:
    config = RuntimeConfig.from_mapping(
        {
            "lyrics_width": 820,
            "lyrics_theme": "dark",
            "lyrics_font_size": 26,
            "lyrics_line_spacing": 1.8,
            "lyrics_max_lines": 50,
            "lyrics_t2i_endpoint": "http://127.0.0.1:6100",
            "lyrics_render_mode": "network",
            "api_timeout": 12.0,
            "comments_width": 640,
            "comments_font_size": 18,
        }
    )
    lyrics = config.lyrics_render_options()
    assert isinstance(lyrics, RenderOptions)
    assert (lyrics.width, lyrics.theme, lyrics.font_size) == (820, "dark", 26)
    assert lyrics.line_spacing == 1.8
    assert lyrics.max_lines == 50
    assert lyrics.endpoint == "http://127.0.0.1:6100"
    assert lyrics.mode == "network"
    assert lyrics.timeout == 12.0

    comments = config.comments_render_options()
    assert (comments.width, comments.font_size) == (640, 18)
    assert comments.theme == "light"
    assert comments.endpoint == "http://127.0.0.1:6100"

    overridden = config.lyrics_render_options(width=1000, return_url=False)
    assert overridden.width == 1000
    assert overridden.return_url is False


def test_to_dict_returns_plain_mapping() -> None:
    config = RuntimeConfig.from_mapping({"enabled": False})
    data = config.to_dict()
    assert isinstance(data, Mapping)
    assert data["enabled"] is False
    assert data["warnings"] == []
    assert config.describe().startswith("provider=netease")
