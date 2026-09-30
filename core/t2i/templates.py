"""内置 Jinja2 HTML 模板（自包含单文件 HTML）与共享渲染辅助。

模板契约（冻结，lyrics_card / comments_card 组装数据时必须提供下列键）：

LYRICS_TEMPLATE
    title, artist, album, duration("3:45"), cover_url(空串走占位块), source,
    theme("light"/"dark"), width, font_size, line_spacing, max_lines(0=不限),
    show_meta, highlight_translation, lines([{'text','translation'}]),
    total_lines, empty_text, footer, fallback_text

COMMENTS_TEMPLATE
    title, artist, album, cover_url, source, theme, width, font_size,
    total, total_text, shown, has_more, show_avatar, show_likes, show_reply,
    items([{'user','content','liked_text','avatar_url','time','replies'}]),
    empty_text, footer, fallback_text

约束：

- 两个模板都是自包含单文件 HTML：CSS 内联在 style 标签里，不引用外部 CSS/JS/字体，
  也不包含任何 script 标签；封面/头像为空时走占位块。
- 用户可控文本（歌名 / 昵称 / 评论正文 / 图片 URL）一律通过双花括号插值，渲染必须
  使用 get_environment()（显式 autoescape=True）或等价环境，禁止使用 safe 过滤器。
- fallback_text 是给 DefaultRenderer 在 mode="local" 时用的 Markdown 兜底文本
  （AstrBot 本地策略只支持 Markdown，不支持 HTML）。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any

from jinja2 import Environment

logger = logging.getLogger(__name__)

__all__ = [
    "COMMENTS_TEMPLATE",
    "LYRICS_TEMPLATE",
    "call_renderer",
    "escape_markdown_text",
    "get_environment",
    "is_template_renderable",
    "render_template",
]

LYRICS_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{{ title }}</title>
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #ffffff; --card: #ffffff; --panel: #f6f7f9;
    --fg: #22252a; --muted: #6b7280; --line: #e5e7eb; --accent: #2f86bd;
  }
  body.theme-dark {
    --bg: #15171c; --card: #1a1d23; --panel: #232830;
    --fg: #e8eaed; --muted: #9aa3ad; --line: #2c313a; --accent: #5fb0e5;
  }
  body {
    margin: 0; padding: 28px; background: var(--bg); color: var(--fg);
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC",
                 "Hiragino Sans GB", sans-serif;
  }
  .card { width: {{ width }}px; max-width: 100%; margin: 0 auto; }
  .head {
    display: flex; gap: 18px; align-items: flex-start;
    padding-bottom: 16px; border-bottom: 1px solid var(--line);
  }
  .cover {
    width: 118px; height: 118px; border-radius: 12px; object-fit: cover;
    background: var(--panel); flex: 0 0 auto;
  }
  .cover-placeholder {
    display: flex; align-items: center; justify-content: center;
    font-size: 42px; color: var(--muted); border: 1px dashed var(--line);
  }
  .meta { flex: 1 1 auto; min-width: 0; }
  .title { margin: 0 0 8px; font-size: 26px; line-height: 1.3; font-weight: 700; word-break: break-word; }
  .artist { margin: 0 0 8px; font-size: 17px; color: var(--muted); word-break: break-word; }
  .tags { margin: 0; font-size: 14px; line-height: 1.6; color: var(--muted); word-break: break-word; }
  .tags span { margin-right: 12px; white-space: nowrap; }
  .lyrics { margin-top: 22px; font-size: {{ font_size }}px; line-height: {{ line_spacing }}; }
  .line { margin: 0 0 0.35em; }
  .line .text { display: block; word-break: break-word; }
  .line .translation { display: block; font-size: 0.84em; color: var(--muted); word-break: break-word; }
  .line .translation-strong { color: var(--accent); }
  .empty { margin: 0; color: var(--muted); }
  .more {
    margin-top: 16px; padding-top: 12px; border-top: 1px dashed var(--line);
    font-size: 13px; color: var(--muted);
  }
  .footer {
    margin-top: 20px; padding-top: 12px; border-top: 1px solid var(--line);
    display: flex; justify-content: space-between; gap: 12px;
    font-size: 12px; color: var(--muted);
  }
</style>
</head>
<body class="theme-{{ 'dark' if theme == 'dark' else 'light' }}">
<div class="card">
  {% set lyric_lines = lines | default([], true) %}
  {% set total = lyric_lines | length %}
  {% set limit = max_lines | default(0, true) %}
  {% if show_meta %}
  <div class="head">
    {% if cover_url %}
    <img class="cover" src="{{ cover_url }}" alt="封面" referrerpolicy="no-referrer" />
    {% else %}
    <div class="cover cover-placeholder">♪</div>
    {% endif %}
    <div class="meta">
      <h1 class="title">{{ title }}</h1>
      {% if artist %}<p class="artist">{{ artist }}</p>{% endif %}
      <p class="tags">
        {% if album %}<span>专辑：{{ album }}</span>{% endif %}
        {% if duration %}<span>时长：{{ duration }}</span>{% endif %}
        {% if source %}<span>{{ source }}</span>{% endif %}
      </p>
    </div>
  </div>
  {% endif %}
  <div class="lyrics">
    {% if total == 0 %}
    <p class="empty">{{ empty_text | default('（暂无歌词）', true) }}</p>
    {% else %}
    {% set visible = lyric_lines[:limit] if limit and limit < total else lyric_lines %}
    {% for line in visible %}
    <div class="line">
      {% if line.text %}<span class="text">{{ line.text }}</span>{% endif %}
      {% if line.translation %}<span class="translation{{ ' translation-strong' if highlight_translation else '' }}">{{ line.translation }}</span>{% endif %}
    </div>
    {% endfor %}
    {% if limit and limit < total %}
    <div class="more">仅显示前 {{ limit }} 行，已省略 {{ total - limit }} 行</div>
    {% endif %}
    {% endif %}
  </div>
  <div class="footer">
    <span>{{ footer | default('', true) }}</span>
    <span>共 {{ total_lines | default(total, true) }} 行歌词</span>
  </div>
</div>
</body>
</html>
"""

COMMENTS_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{{ title }} - 网易云评论</title>
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #ffffff; --panel: #f6f7f9; --item: #ffffff;
    --fg: #22252a; --muted: #6b7280; --line: #e5e7eb; --accent: #2f86bd;
  }
  body.theme-dark {
    --bg: #15171c; --panel: #1a1d23; --item: #1f242b;
    --fg: #e8eaed; --muted: #9aa3ad; --line: #2c313a; --accent: #5fb0e5;
  }
  body {
    margin: 0; padding: 28px; background: var(--bg); color: var(--fg);
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC",
                 "Hiragino Sans GB", sans-serif;
  }
  .card { width: {{ width }}px; max-width: 100%; margin: 0 auto; }
  .head { display: flex; gap: 16px; align-items: center; padding-bottom: 16px; border-bottom: 1px solid var(--line); }
  .cover { width: 84px; height: 84px; border-radius: 10px; object-fit: cover; background: var(--panel); flex: 0 0 auto; }
  .cover-placeholder {
    display: flex; align-items: center; justify-content: center;
    font-size: 32px; color: var(--muted); border: 1px dashed var(--line);
  }
  .meta { flex: 1 1 auto; min-width: 0; }
  .title { margin: 0 0 6px; font-size: 22px; line-height: 1.3; font-weight: 700; word-break: break-word; }
  .artist { margin: 0 0 6px; font-size: 15px; color: var(--muted); word-break: break-word; }
  .tags { margin: 0; font-size: 13px; color: var(--muted); }
  .tags span { margin-right: 12px; }
  .comments { margin: 18px 0 0; padding: 0; list-style: none; font-size: {{ font_size }}px; }
  .comment { padding: 14px 16px; margin-bottom: 12px; background: var(--item); border: 1px solid var(--line); border-radius: 12px; }
  .row { display: flex; gap: 12px; align-items: flex-start; }
  .avatar { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; background: var(--panel); flex: 0 0 auto; }
  .avatar-placeholder {
    display: flex; align-items: center; justify-content: center;
    font-size: 17px; color: var(--muted); border: 1px solid var(--line);
  }
  .body { flex: 1 1 auto; min-width: 0; }
  .top { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  .user { font-weight: 600; word-break: break-word; }
  .liked { font-size: 0.82em; color: var(--accent); white-space: nowrap; }
  .time { font-size: 0.78em; color: var(--muted); white-space: nowrap; }
  .index { display: inline-block; min-width: 22px; margin-right: 6px; font-size: 0.8em; color: var(--muted); }
  .content { margin: 0; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  .reply {
    margin: 8px 0 0; padding: 8px 12px; border-left: 3px solid var(--line);
    background: var(--panel); border-radius: 0 8px 8px 0;
    font-size: 0.88em; color: var(--muted); white-space: pre-wrap; word-break: break-word;
  }
  .reply-user { color: var(--accent); }
  .empty { margin: 18px 0 0; color: var(--muted); }
  .more { margin-top: 6px; font-size: 13px; color: var(--muted); }
  .footer { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); }
</style>
</head>
<body class="theme-{{ 'dark' if theme == 'dark' else 'light' }}">
<div class="card">
  <div class="head">
    {% if cover_url %}
    <img class="cover" src="{{ cover_url }}" alt="封面" referrerpolicy="no-referrer" />
    {% else %}
    <div class="cover cover-placeholder">♪</div>
    {% endif %}
    <div class="meta">
      <h1 class="title">{{ title }}</h1>
      {% if artist %}<p class="artist">{{ artist }}</p>{% endif %}
      <p class="tags">
        <span>{{ source | default('', true) }}</span>
        <span>评论 {{ total_text | default('0', true) }} 条</span>
        {% if shown %}<span>显示 {{ shown }} 条</span>{% endif %}
      </p>
    </div>
  </div>
  {% set comment_items = items | default([], true) %}
  {% if comment_items | length == 0 %}
  <p class="empty">{{ empty_text | default('（暂无评论）', true) }}</p>
  {% else %}
  <ol class="comments">
    {% for item in comment_items %}
    <li class="comment">
      <div class="row">
        {% if show_avatar %}
        {% if item.avatar_url %}
        <img class="avatar" src="{{ item.avatar_url }}" alt="" referrerpolicy="no-referrer" />
        {% else %}
        <span class="avatar avatar-placeholder">{{ (item.user or '?')[:1] }}</span>
        {% endif %}
        {% endif %}
        <div class="body">
          <div class="top">
            <span class="user"><span class="index">{{ loop.index }}</span>{{ item.user }}</span>
            {% if show_likes %}<span class="liked">赞 {{ item.liked_text }}</span>{% endif %}
            {% if item.time %}<span class="time">{{ item.time }}</span>{% endif %}
          </div>
          <p class="content">{{ item.content }}</p>
          {% if show_reply and item.replies %}
          {% for reply in item.replies %}
          <p class="reply"><span class="reply-user">{{ reply.user }}</span>：{{ reply.content }}</p>
          {% endfor %}
          {% endif %}
        </div>
      </div>
    </li>
    {% endfor %}
  </ol>
  {% endif %}
  {% if has_more and shown %}
  <div class="more">仅显示前 {{ shown }} 条评论</div>
  {% endif %}
  <div class="footer">{{ footer | default('', true) }}</div>
</div>
</body>
</html>
"""

_ENVIRONMENT: Environment | None = None
"""进程内共享的 Jinja2 环境（显式 autoescape=True，惰性创建）。"""


def get_environment() -> Environment:
    """返回显式 autoescape=True 的共享 Jinja2 环境。

    模板里的用户可控文本（歌名 / 昵称 / 评论正文 / 图片 URL）必须靠这个环境转义；
    禁止改用 autoescape=False，也禁止给插值加 safe 过滤器。
    """
    global _ENVIRONMENT
    if _ENVIRONMENT is None:
        _ENVIRONMENT = Environment(autoescape=True, keep_trailing_newline=True)
    return _ENVIRONMENT


def render_template(
    template: str,
    data: Mapping[str, Any] | None = None,
    *,
    strict: bool = False,
    environment: Environment | None = None,
) -> str | None:
    """用 autoescape=True 的 Jinja2 环境渲染模板。

    - strict=False（默认）：模板为空 / 语法错误 / 渲染异常都记日志并返回 None；
    - strict=True：异常继续向上抛（测试与用户模板自检用）；
    - 返回渲染后的 HTML 字符串，调用方再交给 renderer.render_html。
    """
    env = environment if environment is not None else get_environment()
    try:
        if not isinstance(template, str) or not template.strip():
            raise ValueError("模板为空")
        if data is None:
            payload: Mapping[str, Any] = {}
        elif isinstance(data, Mapping):
            payload = data
        else:
            payload = {"data": data}
        return env.from_string(template).render(payload)
    except Exception as exc:
        if strict:
            raise
        logger.error("模板渲染失败：%s", exc)
        return None


def is_template_renderable(
    template: str,
    data: Mapping[str, Any] | None = None,
) -> bool:
    """模板自检：能在 autoescape=True 环境下渲染出非空结果即为 True。"""
    result = render_template(template, data)
    return bool(result and result.strip())


async def call_renderer(method: Any, *args: Any, **kwargs: Any) -> str | None:
    """安全调用 Renderer 协议方法，返回图片 URL / 本地路径或 None。

    三级降级链的每一级都用它收口，保证语义一致：

    - 异常（含同步方法直接抛异常）一律记日志并返回 None，交给下一级降级；
    - asyncio.CancelledError 属于 BaseException，继续向上传播，不被吞掉；
    - 返回值必须是去空白后仍非空的字符串，否则视为失败。

    放在 templates 模块是为了让 lyrics_card / comments_card 共用同一套降级判定。
    """
    if not callable(method):
        return None
    try:
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        logger.error("渲染调用失败：%s", exc)
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def escape_markdown_text(text: str) -> str:
    """Encode Markdown syntax before passing external text to a legacy renderer.

    Entity decoding happens after Markdown block recognition in AstrBot, so an
    external image marker cannot become an ImageBlock. Chat fallbacks continue
    using the original plain text.

    Args:
        text: External lyrics, comments, and metadata.

    Returns:
        Markdown source displaying the original literal characters.
    """
    syntax = set("&<>\\`*_[]()!#|$~")
    return "".join(f"&#{ord(char)};" if char in syntax else char for char in text)
