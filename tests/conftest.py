"""共享测试脚手架：注入 AstrBot 参考源码路径（ASTRBOT_REF）与插件仓库根。

约定（全队必须一致）：
- 环境变量 ASTRBOT_REF 优先；未设置时回退到 <仓库根的上一级>/_astrbot_ref，
  即 Path(__file__).resolve().parents[2] / "_astrbot_ref"。
- 参考源码不存在时不报错，只是 ASTRBOT_AVAILABLE 为 False；
  需要真实 AstrBot API 的测试请调用 require_astrbot()（自动 pytest.skip）。
- 禁止在插件源码里写死绝对路径。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 隔离 AstrBot 运行期数据目录
# ---------------------------------------------------------------------------
# astrbot.core.utils.astrbot_path.get_astrbot_root() 在 ASTRBOT_ROOT 未设置时
# 回退到 os.getcwd()；而 pytest 的 cwd 就是插件仓库根。于是任何 import astrbot
# 的测试都会在仓库里凭空写出 data/cmd_config.json 与 data/t2i_templates/*.html，
# 污染插件仓库。这里在导入 astrbot 之前把根目录重定向到进程级临时目录。
# 该赋值必须发生在任何 astrbot 导入之前，因此只能放在模块级（早于下方 sys.path 注入）。
if not (os.environ.get("ASTRBOT_ROOT") or "").strip():
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot-test-root-")

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
"""插件仓库根目录。"""

DEFAULT_ASTRBOT_REF = Path(__file__).resolve().parents[2] / "_astrbot_ref"
"""ASTRBOT_REF 未设置时的回退路径：仓库根的上一级 / _astrbot_ref。"""


def resolve_astrbot_ref() -> Path:
    """解析 AstrBot 参考源码检出目录（环境变量优先，其次相对回退）。"""
    env = (os.environ.get("ASTRBOT_REF") or "").strip()
    if env:
        try:
            return Path(env).expanduser().resolve()
        except OSError:  # pragma: no cover - 极端路径异常时退回默认值
            return DEFAULT_ASTRBOT_REF
    return DEFAULT_ASTRBOT_REF


ASTRBOT_REF = resolve_astrbot_ref()
"""本轮测试使用的 AstrBot 参考源码路径（可能不存在）。"""


def _ensure_on_sys_path(path: Path) -> bool:
    """把目录插入 sys.path 头部；目录不存在时返回 False。"""
    try:
        if not path.is_dir():
            return False
    except OSError:  # pragma: no cover
        return False
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
    return True


# 仓库根：让测试可以 import core.*
PLUGIN_ROOT_ON_PATH = _ensure_on_sys_path(PLUGIN_ROOT)
# AstrBot 参考源码：让测试可以 import astrbot.*（不存在则跳过相关测试）
ASTRBOT_AVAILABLE = _ensure_on_sys_path(ASTRBOT_REF)


def require_astrbot() -> Path:
    """真实 AstrBot API 不可用时跳过当前测试。"""
    if not ASTRBOT_AVAILABLE:
        pytest.skip(
            f"AstrBot 参考源码不存在（ASTRBOT_REF={ASTRBOT_REF}），跳过集成测试。"
        )
    return ASTRBOT_REF


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    """插件仓库根目录。"""
    return PLUGIN_ROOT


@pytest.fixture(scope="session")
def astrbot_ref() -> Path:
    """AstrBot 参考源码目录（不可用时 skip）。"""
    return require_astrbot()


@pytest.fixture(scope="session")
def conf_schema_path() -> Path:
    """插件配置 schema 路径。"""
    return PLUGIN_ROOT / "_conf_schema.json"
