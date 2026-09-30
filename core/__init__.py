"""点歌插件的契约层：配置、数据模型、Provider 协议与渲染器协议。

设计约束（全队一致）：
- 本包只依赖标准库与 aiohttp（仅在自定义 t2i 端点时使用），不 import astrbot，
  因此单元测试无需真实 AstrBot 运行时即可覆盖。
- 字段名与协议签名在此冻结，其他模块不得改名；新增能力只能追加。
"""

from __future__ import annotations

__all__ = ["config", "models", "provider", "renderer"]
__version__ = "0.1.0"
