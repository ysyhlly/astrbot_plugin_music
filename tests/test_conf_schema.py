"""_conf_schema.json 校验：JSON 合法性、类型、默认值与 RuntimeConfig 一致性。

验收覆盖点：
- 合法 JSON、六组配置齐全；
- 每个叶子节点都有 type 且属于 AstrBot 支持的类型集合；
- object 节点必须有 items；
- int/float 节点的 default 类型正确；
- 任务点名的每一个键路径都存在且类型正确。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.config import DEFAULT_USER_AGENT, FIELD_SPEC_BY_NAME, RuntimeConfig

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_ROOT / "_conf_schema.json"

ALLOWED_TYPES = {
    "string",
    "text",
    "int",
    "float",
    "bool",
    "object",
    "list",
    "dict",
    "template_list",
}

GROUP_KEYS = ("basic", "source", "card", "lyrics", "comments", "send")

# 任务点名的全部键路径：(组, 键, 期望类型)
EXPECTED_KEYS: tuple[tuple[str, str, str], ...] = (
    # 1) 基础
    ("basic", "enabled", "bool"),
    ("basic", "command_aliases", "list"),
    ("basic", "cooldown_seconds", "int"),
    ("basic", "daily_limit", "int"),
    ("basic", "group_whitelist", "list"),
    ("basic", "group_blacklist", "list"),
    ("basic", "user_blacklist", "list"),
    # 2) 点歌来源
    ("source", "provider", "string"),
    ("source", "netease_mode", "string"),
    ("source", "netease_api_base", "string"),
    ("source", "search_limit", "int"),
    ("source", "pick_strategy", "string"),
    ("source", "search_timeout", "float"),
    ("source", "api_timeout", "float"),
    ("source", "max_retries", "int"),
    ("source", "user_agent", "string"),
    ("source", "cookie", "string"),
    ("source", "fallback_to_plain", "bool"),
    # 3) 歌曲卡片
    ("card", "card_enable", "bool"),
    ("card", "card_type", "string"),
    ("card", "card_show_cover", "bool"),
    ("card", "card_show_source", "bool"),
    ("card", "card_attach_audio", "bool"),
    ("card", "card_fallback", "string"),
    # 4) 歌词（t2i）
    ("lyrics", "lyrics_enable", "bool"),
    ("lyrics", "lyrics_t2i", "bool"),
    ("lyrics", "lyrics_render_mode", "string"),
    ("lyrics", "lyrics_t2i_endpoint", "string"),
    ("lyrics", "lyrics_template", "text"),
    ("lyrics", "lyrics_width", "int"),
    ("lyrics", "lyrics_theme", "string"),
    ("lyrics", "lyrics_font_size", "int"),
    ("lyrics", "lyrics_line_spacing", "float"),
    ("lyrics", "lyrics_max_lines", "int"),
    ("lyrics", "lyrics_show_meta", "bool"),
    ("lyrics", "lyrics_highlight_translation", "bool"),
    ("lyrics", "lyrics_fallback_text", "bool"),
    # 5) 网易云评论
    ("comments", "comments_enable", "bool"),
    ("comments", "comments_t2i", "bool"),
    ("comments", "comments_count", "int"),
    ("comments", "comments_sort", "string"),
    ("comments", "comments_page", "int"),
    ("comments", "comments_max_chars", "int"),
    ("comments", "comments_show_avatar", "bool"),
    ("comments", "comments_show_likes", "bool"),
    ("comments", "comments_show_reply", "bool"),
    ("comments", "comments_reply_count", "int"),
    ("comments", "comments_width", "int"),
    ("comments", "comments_theme", "string"),
    ("comments", "comments_font_size", "int"),
    ("comments", "comments_fallback_text", "bool"),
    # 6) 发送与调试
    ("send", "send_delay", "float"),
    ("send", "reply_with_at", "bool"),
    ("send", "debug_log", "bool"),
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _iter_nodes(node: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    for key, value in node.items():
        assert isinstance(value, dict), f"{prefix}{key} 必须是对象"
        path = f"{prefix}{key}"
        yield path, value
        if value.get("type") == "object":
            items = value.get("items")
            if isinstance(items, dict):
                yield from _iter_nodes(items, f"{path}.")


def _leaves(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        path: node
        for path, node in _iter_nodes(schema)
        if node.get("type") != "object"
    }


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def test_schema_file_is_valid_json(schema: dict[str, Any]) -> None:
    assert isinstance(schema, dict)
    assert schema, "schema 不能为空"


def test_top_level_groups_exist(schema: dict[str, Any]) -> None:
    assert set(GROUP_KEYS).issubset(schema.keys())


@pytest.mark.parametrize("group", GROUP_KEYS)
def test_group_is_object_with_items(schema: dict[str, Any], group: str) -> None:
    node = schema[group]
    assert node["type"] == "object", f"{group} 必须是 object"
    assert isinstance(node.get("items"), dict) and node["items"], f"{group} 必须有 items"


def test_every_node_has_supported_type(schema: dict[str, Any]) -> None:
    for path, node in _iter_nodes(schema):
        assert "type" in node, f"{path} 缺少 type"
        assert node["type"] in ALLOWED_TYPES, f"{path} 的 type={node['type']} 不受支持"


def test_object_nodes_have_items(schema: dict[str, Any]) -> None:
    for path, node in _iter_nodes(schema):
        if node["type"] == "object":
            items = node.get("items")
            assert isinstance(items, dict) and items, f"{path} 是 object 但没有 items"


def test_int_and_float_defaults_have_correct_type(schema: dict[str, Any]) -> None:
    for path, node in _iter_nodes(schema):
        if node["type"] == "int":
            default = node.get("default")
            assert isinstance(default, int) and not isinstance(default, bool), (
                f"{path} 的 int 默认值类型错误：{default!r}"
            )
        elif node["type"] == "float":
            default = node.get("default")
            assert isinstance(default, float), f"{path} 的 float 默认值类型错误：{default!r}"


def test_leaves_have_chinese_description_and_hint(schema: dict[str, Any]) -> None:
    for path, node in _leaves(schema).items():
        description = node.get("description")
        hint = node.get("hint")
        assert isinstance(description, str) and description.strip(), f"{path} 缺少 description"
        assert _has_cjk(description), f"{path} 的 description 需要是中文"
        assert isinstance(hint, str) and hint.strip(), f"{path} 缺少 hint"
        assert _has_cjk(hint), f"{path} 的 hint 需要是中文"


def test_string_options_default_is_listed(schema: dict[str, Any]) -> None:
    for path, node in _leaves(schema).items():
        options = node.get("options")
        if options is None:
            continue
        assert isinstance(options, list) and options, f"{path} 的 options 必须是非空列表"
        assert node["type"] in {"string", "text"}, f"{path} 只有字符串类型才支持 options"
        assert node.get("default") in options, f"{path} 的默认值不在 options 中"


def test_cookie_is_secret(schema: dict[str, Any]) -> None:
    assert schema["source"]["items"]["cookie"].get("secret") is True


@pytest.mark.parametrize(("group", "key", "expected_type"), EXPECTED_KEYS)
def test_named_key_paths_exist_with_expected_type(
    schema: dict[str, Any], group: str, key: str, expected_type: str
) -> None:
    node = schema[group]["items"].get(key)
    assert node is not None, f"缺少配置项 {group}.{key}"
    assert node["type"] == expected_type, f"{group}.{key} 类型应为 {expected_type}"
    assert "default" in node, f"{group}.{key} 缺少 default"


def test_named_defaults_equal_documented_values(schema: dict[str, Any]) -> None:
    expectations = {
        "basic.enabled": True,
        "basic.command_aliases": ["点歌", "听歌", "music"],
        "basic.cooldown_seconds": 5,
        "basic.daily_limit": 0,
        "basic.group_whitelist": [],
        "basic.group_blacklist": [],
        "basic.user_blacklist": [],
        "source.provider": "netease",
        "source.netease_mode": "official_direct",
        "source.netease_api_base": "",
        "source.search_limit": 5,
        "source.pick_strategy": "top",
        "source.search_timeout": 8.0,
        "source.api_timeout": 10.0,
        "source.max_retries": 2,
        "source.user_agent": DEFAULT_USER_AGENT,
        "source.cookie": "",
        "source.fallback_to_plain": True,
        "card.card_enable": True,
        "card.card_type": "163",
        "card.card_show_cover": True,
        "card.card_show_source": True,
        "card.card_attach_audio": False,
        "card.card_fallback": "share",
        "lyrics.lyrics_enable": True,
        "lyrics.lyrics_t2i": True,
        "lyrics.lyrics_render_mode": "auto",
        "lyrics.lyrics_t2i_endpoint": "",
        "lyrics.lyrics_template": "",
        "lyrics.lyrics_width": 900,
        "lyrics.lyrics_theme": "light",
        "lyrics.lyrics_font_size": 22,
        "lyrics.lyrics_line_spacing": 1.6,
        "lyrics.lyrics_max_lines": 200,
        "lyrics.lyrics_show_meta": True,
        "lyrics.lyrics_highlight_translation": True,
        "lyrics.lyrics_fallback_text": True,
        "comments.comments_enable": True,
        "comments.comments_t2i": True,
        "comments.comments_count": 10,
        "comments.comments_sort": "hot",
        "comments.comments_page": 1,
        "comments.comments_max_chars": 120,
        "comments.comments_show_avatar": True,
        "comments.comments_show_likes": True,
        "comments.comments_show_reply": False,
        "comments.comments_reply_count": 1,
        "comments.comments_width": 900,
        "comments.comments_theme": "light",
        "comments.comments_font_size": 20,
        "comments.comments_fallback_text": True,
        "send.send_delay": 0.6,
        "send.reply_with_at": False,
        "send.debug_log": False,
    }
    leaves = _leaves(schema)
    assert set(expectations) == set(leaves), "独立默认值表与 schema 叶子不一致"
    for path, expected in expectations.items():
        assert leaves[path].get("default") == expected, f"{path} 默认值应为 {expected!r}"


def test_schema_defaults_match_runtime_config_defaults(schema: dict[str, Any]) -> None:
    runtime = RuntimeConfig()
    leaves = _leaves(schema)
    assert set(FIELD_SPEC_BY_NAME) == {path.split(".")[-1] for path in leaves}
    for path, node in leaves.items():
        name = path.split(".")[-1]
        expected = getattr(runtime, name)
        assert node.get("default") == expected, (
            f"{path} 的 schema 默认值 {node.get('default')!r} 与 RuntimeConfig 默认值 "
            f"{expected!r} 不一致"
        )


def test_astrbot_config_accepts_schema(schema: dict[str, Any], astrbot_ref: Path, tmp_path: Path) -> None:
    """用真实 AstrBotConfig 解析本 schema，确保默认值可被 AstrBot 完整装载。"""
    from astrbot.api import AstrBotConfig

    config_path = tmp_path / "astrbot_plugin_music_config.json"
    config = AstrBotConfig(config_path=str(config_path), schema=schema)
    assert config_path.exists()

    stored = dict(config)
    assert set(stored) == set(GROUP_KEYS)
    for group in GROUP_KEYS:
        assert set(stored[group]) == set(schema[group]["items"])

    runtime = RuntimeConfig.from_mapping(config)
    assert runtime.warnings == [], runtime.warnings
    assert runtime.to_dict() == RuntimeConfig.from_mapping({}).to_dict()

    config["basic"]["cooldown_seconds"] = 12
    config["lyrics"]["lyrics_theme"] = "dark"
    updated = RuntimeConfig.from_mapping(config)
    assert updated.cooldown_seconds == 12
    assert updated.lyrics_theme == "dark"
    assert updated.warnings == []
