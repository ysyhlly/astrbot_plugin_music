"""每个事件一个 HttpTransport 的 async contextmanager 封装。

契约
----
- transport_session(cfg) 每次进入都新建一个 HttpTransport，退出时**必须**关闭，
  异常路径也一样（finally 里 close）；provider 按协议不拥有 transport，由这里兜底；
- transport_session(cfg, transport=xxx) 复用调用方传入的 transport，并且**不关闭它**
  （生命周期归调用方，与 core.provider.Transport 的约定一致）；
- 关闭失败只记日志，绝不把异常抛给业务路径。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .config import ensure_runtime_config
from .logging_utils import get_logger

logger = get_logger("session")

__all__ = ["build_transport", "close_transport", "transport_session"]


def build_transport(cfg: Any = None) -> Any:
    """按配置构造 HttpTransport（延迟 import core.netease.http）。"""
    from .netease.http import HttpTransport  # 延迟 import：缺失时给出可读错误

    return HttpTransport.from_config(ensure_runtime_config(cfg))


async def close_transport(transport: Any) -> None:
    """尽力关闭 transport（close 或 aclose），失败只记日志。"""
    if transport is None:
        return
    for name in ("close", "aclose"):
        closer = getattr(transport, name, None)
        if not callable(closer):
            continue
        try:
            result = closer()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # pragma: no cover - 关闭失败不影响业务
            logger.debug("关闭 transport 失败：%r", exc)
        return


@asynccontextmanager
async def transport_session(cfg: Any = None, *, transport: Any = None) -> AsyncIterator[Any]:
    """为单次事件提供 transport；异常路径同样保证关闭。"""
    if transport is not None:
        # 外部注入的 transport 不归我们关（provider 协议也是这么约定的）
        yield transport
        return

    instance = build_transport(cfg)
    logger.debug("创建 HttpTransport（mode=%s）", getattr(instance, "mode", "?"))
    try:
        yield instance
    finally:
        await close_transport(instance)
        logger.debug("已关闭 HttpTransport")
