"""F1 回归测试：source.fallback_to_plain 必须真实生效（captain 修复轮 t16）。

背景：评审 t15 发现 F1（medium）—— source.fallback_to_plain 在 _conf_schema.json 中声明、
在 RuntimeConfig 中有字段与默认值，但插件源码中**没有任何读取点**，用户切换该开关完全无效，
违反「所有参数可调」的需求。

修复：在 main.py 的兜底分支真正接入它，作为「全局纯文本兜底」总开关：
  * fallback_to_plain=True（默认）→ 歌词/评论渲染失败时补发纯文本（原行为）；
  * fallback_to_plain=False      → 不再补发纯文本。
刻意**不受**该开关影响的两处（语义不同，已有注释说明）：
  * card_fallback（share/text）是用户显式选择的卡片形态；
  * 末尾 _summary_message 是「三者全关时至少说一句」的安全网——空链会被
    AstrBot 的 respond stage 整条跳过，用户将什么都收不到。

不修改 tests/test_command_parse.py（复用其 fixture 与假件，避免写入冲突）。
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if _p not in sys.path:  # pragma: no cover - 首次导入生效
        sys.path.insert(0, _p)

# 复用既有假件与 fixture（tests 目录在 pytest 的 rootdir 上，可直接模块导入）
from test_command_parse import (  # noqa: E402
    FakeProvider,
    FakeRenderer,
    fake_components,  # noqa: F401  (pytest fixture)
    make_cfg,
    plugin_main,  # noqa: F401  (pytest fixture)
)


def _kinds(outcome: Any) -> list[str]:
    return [kind for kind, _ in outcome.messages]


async def test_fallback_to_plain_false_suppresses_lyrics_and_comments_text(
    plugin_main, fake_components
) -> None:
    """渲染失败 + 开关关闭 → 不应出现任何纯文本兜底消息。"""
    outcome = await plugin_main.run_music_request(
        make_cfg({"fallback_to_plain": False}),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    assert outcome.ok is True
    assert "text" not in _kinds(outcome), outcome.messages


async def test_fallback_to_plain_true_keeps_lyrics_and_comments_text(
    plugin_main, fake_components
) -> None:
    """同一场景 + 开关开启（默认）→ 必须补发纯文本，证明该键确实改变行为。"""
    outcome = await plugin_main.run_music_request(
        make_cfg({"fallback_to_plain": True}),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    texts = [text for kind, text in outcome.messages if kind == "text"]
    assert texts, outcome.messages
    assert len(texts) >= 2, f"预期歌词与评论各一条纯文本兜底，实际 {texts!r}"


async def test_default_fallback_to_plain_is_true(plugin_main, fake_components) -> None:
    """不配置该键时行为等同 True（默认值必须生效，不得因修复而回归）。"""
    outcome = await plugin_main.run_music_request(
        make_cfg({}),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    assert "text" in _kinds(outcome), outcome.messages


async def test_fallback_to_plain_false_never_produces_empty_chain(
    plugin_main, fake_components
) -> None:
    """安全网必须保留：三者全关 + 开关关闭时，仍要有 _summary_message。

    空消息链会被 AstrBot respond stage 整条跳过（stage.py「The message is empty」），
    用户会什么都收不到——这是本轮修复中最需要守住的边界。
    """
    outcome = await plugin_main.run_music_request(
        make_cfg(
            {
                "fallback_to_plain": False,
                "card_enable": False,
                "lyrics_enable": False,
                "comments_enable": False,
            }
        ),
        keyword="晴天",
        provider=FakeProvider(),
        transport=object(),
        renderer=FakeRenderer(),
        factory=fake_components,
    )
    assert outcome.ok is True
    assert outcome.messages, "消息链不得为空"
    assert _kinds(outcome) == ["text"]
    kind, text = outcome.messages[0]
    assert "晴天" in text


async def test_card_fallback_text_unaffected_by_fallback_to_plain(
    plugin_main, fake_components
) -> None:
    """card_fallback=text 是用户显式选择的卡片形态，不应被 fallback_to_plain 关掉。"""
    outcome = await plugin_main.run_music_request(
        make_cfg(
            {
                "fallback_to_plain": False,
                "card_type": "custom",
                "card_attach_audio": False,
                "card_fallback": "text",
            }
        ),
        keyword="晴天",
        provider=FakeProvider(audio=""),
        transport=object(),
        renderer=FakeRenderer(html=None, markdown=None),
        factory=fake_components,
    )
    assert outcome.ok is True
    assert outcome.messages, "消息链不得为空"
    # 注意消息形态：card_fallback="text" 产出的是一条 **chain**（内含 Plain 组件），
    # 不是 ("text", str) 形态——后者只用于歌词/评论的纯文本兜底与安全网。
    assert "chain" in _kinds(outcome), outcome.messages
    plain = outcome.messages[0][1][0]
    text = str(getattr(plain, "text", "") or "")
    assert text.strip(), "卡片退化文本不得为空"
    assert "晴天" in text or "186016" in text, text
