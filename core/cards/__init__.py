"""歌曲卡片构造包。

对外契约（供 main.py 调用）：

- build_card(song, cfg, ...) -> list[组件]：契约要求的同步/异步总入口
  （async，可能需要取一次播放地址），返回可直接放进 event.chain_result 的组件；
- build_card_result(...) -> CardResult：需要降级原因、纯文本兜底或 song_id 时用它；
  CardResult.components 非空时发送组件链，为空时才发送 CardResult.text；
- platform_name：传事件的 PlatformMetadata.name，aiocqhttp 使用原生 Music/Share，
  其他平台发送包含歌曲链接的 Plain；省略时维持原有组件构造行为；
- at_component(qq) / card_extra_components(...)：@ 组件与附加语音组件；
- COMPONENT_FACTORY / resolve_component_factory：可注入的组件工厂
  （默认延迟 import astrbot.api.message_components），使卡片逻辑在没有 astrbot
  的环境里也能单测。

底线：任何组件在返回前都要通过 component_is_deliverable 复核
（规则与 AstrBot respond stage 的非空校验一致），构造失败按 cfg.card_fallback
退化为 Share 或纯文本，绝不返回会被静默丢弃的空组件、绝不抛异常。
"""

from __future__ import annotations

from .music_card import (
    CARD_TYPES,
    COMPONENT_FACTORY,
    FALLBACK_TYPES,
    CardResult,
    at_component,
    build_card,
    build_card_result,
    build_card_result_from_payload,
    build_card_text,
    build_payload_from_song,
    card_extra_components,
    component_is_deliverable,
    default_component_factory,
    ensure_music_type,
    music_component_is_valid,
    normalise_card_type,
    normalise_song_id,
    plain_component,
    resolve_component_factory,
    song_page_url,
)

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
    "music_component_is_valid",
    "normalise_card_type",
    "normalise_song_id",
    "resolve_component_factory",
    "song_page_url",
]
