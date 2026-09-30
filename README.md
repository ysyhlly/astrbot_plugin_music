# astrbot_plugin_music · 点歌

给 AstrBot 加一个点歌台：`/点歌 歌名 [歌手]`，机器人依次回你 **歌曲卡片 → 歌词图 → 网易云热评图**。

三样东西都能单独开关、单独调样式；全部 54 个参数都在插件配置面板里。

---

## 效果

```
/点歌 晴天 周杰伦
  ↓
[歌曲卡片]  网易云音乐卡片（可点播）
[歌词图]    封面 + 曲名/歌手/专辑/时长 + 歌词（含翻译副行）
[热评图]    评论区精选：头像 / 昵称 / 点赞数 / 正文 / 楼中楼
```

- 歌词图与评论图走 AstrBot 的 **t2i（文转图）**，默认模板自包含、无需额外部署
- 渲染服务不可用时**不会让指令失败**：三级降级 `HTML → Markdown → 纯文本`

---

## 安装

### 方式一：用安装包（推荐）

`astrbot_plugin_music-v0.1.0.zip` 解压后**顶层就是 `astrbot_plugin_music/` 目录**，整目录放进 AstrBot 的 `data/plugins/`：

```
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_music/     ← 解压出来的这个目录
            ├── main.py
            ├── _conf_schema.json
            ├── metadata.yaml
            ├── requirements.txt
            └── core/
```

然后在 WebUI「插件管理」点重载。**注意不要把里面的文件直接倒进 `plugins/`**，必须是 `plugins/astrbot_plugin_music/` 这一层。

### 方式二：从源码目录

直接把本仓库目录整体复制到 `data/plugins/astrbot_plugin_music/` 即可。

### 环境要求

| 项 | 要求 |
|---|---|
| AstrBot | `>=4.16,<5`（开发与验证基于 4.28.1） |
| Python | 3.12+ |
| 依赖 | `aiohttp>=3.9`（见 `requirements.txt`）；AstrBot 已自带 `aiohttp`/`jinja2`，通常无需额外安装 |

> 安装包内**不含** `tests/` 与开发报告；它们只在本仓库中，用于开发验证。

---

## 指令

| 指令 | 说明 |
|---|---|
| `/点歌 <歌名>` | 直接搜歌名 |
| `/点歌 <歌名> <歌手>` | 歌手可选，**允许含空格**（`/点歌 晴天 周 杰 伦` 会当作歌手「周 杰 伦」） |

别名默认 `点歌 / 听歌 / music`，可在 `basic.command_aliases` 改。

**找不到歌时**会回可读中文提示而不是静默失败。

---

## 配置（54 项，全部可在面板调整）

### 1. 基础 `basic`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用点歌功能 |
| `command_aliases` | list | `["点歌","听歌","music"]` | 指令别名列表 |
| `cooldown_seconds` | int | `5` | 同一用户冷却秒数 |
| `daily_limit` | int | `0` | 每人每日点歌上限，`0` = 不限 |
| `group_whitelist` | list | `[]` | 群白名单（非空时仅这些群可用；支持 `*` 通配） |
| `group_blacklist` | list | `[]` | 群黑名单（优先级高于白名单） |
| `user_blacklist` | list | `[]` | 用户黑名单 |

### 2. 点歌来源 `source`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `provider` | string | `netease` | 音源 |
| `netease_mode` | string | `official_direct` | `official_direct`（免配置直连）/`self_hosted_api`（自建 API） |
| `netease_api_base` | string | `""` | 自建 NeteaseCloudMusicApi 根地址 |
| `search_limit` | int | `5` | 搜索候选数量（1–20） |
| `pick_strategy` | string | `top` | `top`=歌手匹配优先；`first`=歌手匹配优先后再比完全同名 |
| `search_timeout` | float | `8.0` | 搜索超时（秒） |
| `api_timeout` | float | `10.0` | 接口超时（秒） |
| `max_retries` | int | `2` | 失败重试次数（指数退避，仅 5xx/超时重试） |
| `user_agent` | string | Chrome UA | 请求 UA |
| `cookie` | string | `""` | 网易云 Cookie（**密钥字段**，用于需要登录的接口） |
| `fallback_to_plain` | bool | `true` | **全局纯文本兜底总开关**：关闭后歌词/评论渲染失败不再补发纯文本 |

> `fallback_to_plain` 的作用范围（三者语义不同，勿混淆）：
> - **受它控制**：歌词、评论渲染失败后的纯文本兜底；
> - **不受它控制**：`card_fallback`（`share`/`text`）是你**显式选择**的卡片形态——选了 `text` 就应该拿到文本；
> - **不受它控制**：三个功能全关时那条「点到了什么」提示，它是防止**空消息链**的安全网（空链会被 AstrBot 出站环节整条跳过，你将什么都收不到）。

> **官方直连的已知限制**：评论多数需要 Cookie 且无法按时间排序、无逐字歌词、旧搜索接口可能无封面、播放地址基本取不到。
> 想要完整体验（封面 / 音频 / 按时间排序评论），建议自建 [NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) 并把 `netease_mode` 设为 `self_hosted_api`、`netease_api_base` 填其地址。

### 3. 歌曲卡片 `card`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `card_enable` | bool | `true` | 是否发送歌曲卡片 |
| `card_type` | string | `163` | `163`=网易云音乐卡片；`custom`=自定义卡片（需音频直链）；`share`=链接分享卡片 |
| `card_show_cover` | bool | `true` | 卡片显示封面 |
| `card_show_source` | bool | `true` | 卡片显示来源 |
| `card_attach_audio` | bool | `false` | 额外附带音频直链（`custom` 形态必需） |
| `card_fallback` | string | `share` | 卡片无法构造时的降级形态：`share`/`text` |

> `custom` 形态要求 `url`+`audio`+`title` 同时非空（AstrBot 的出站校验规则），否则组件会被丢弃。
> 插件会自动尝试取音频直链，拿不到就按 `card_fallback` 降级，**不会出现「用户什么都收不到」**。

### 4. 歌词图 `lyrics`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `lyrics_enable` | bool | `true` | 是否发送歌词 |
| `lyrics_t2i` | bool | `true` | 渲染为图片（关闭则发纯文本） |
| `lyrics_render_mode` | string | `auto` | `network`（仅网络端点）/`local`（仅本地）/`auto`（网络失败回落本地） |
| `lyrics_t2i_endpoint` | string | `""` | 自定义文转图端点（留空用 AstrBot 内置） |
| `lyrics_template` | text | `""` | 自定义 Jinja2 HTML 模板（留空用内置模板） |
| `lyrics_width` | int | `900` | 歌词图宽度（px） |
| `lyrics_theme` | string | `light` | `light`/`dark` |
| `lyrics_font_size` | int | `22` | 字号 |
| `lyrics_line_spacing` | float | `1.6` | 行距 |
| `lyrics_max_lines` | int | `200` | 最大歌词行数，`0` = 全部 |
| `lyrics_show_meta` | bool | `true` | 显示曲名/歌手/专辑/时长 |
| `lyrics_highlight_translation` | bool | `true` | 显示翻译副行 |
| `lyrics_fallback_text` | bool | `true` | 渲染失败时退化为纯文本 |

### 5. 网易云评论 `comments`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `comments_enable` | bool | `true` | 是否发送评论 |
| `comments_t2i` | bool | `true` | 渲染为图片（关闭则发纯文本） |
| `comments_count` | int | `10` | 评论条数（0–50） |
| `comments_sort` | string | `hot` | `hot`=最热/`new`=最新（`new` 通常需自建 API） |
| `comments_page` | int | `1` | 评论页码 |
| `comments_max_chars` | int | `120` | 单条评论最大字数，超出加省略号 |
| `comments_show_avatar` | bool | `true` | 显示头像 |
| `comments_show_likes` | bool | `true` | 显示点赞数 |
| `comments_show_reply` | bool | `false` | 显示楼中楼回复 |
| `comments_reply_count` | int | `1` | 每条评论展示的回复数 |
| `comments_width` | int | `900` | 评论图宽度（px） |
| `comments_theme` | string | `light` | `light`/`dark` |
| `comments_font_size` | int | `20` | 字号 |
| `comments_fallback_text` | bool | `true` | 渲染失败时退化为纯文本 |

> **空评论 ≠ 获取失败**：请求成功但没有评论会提示「这首歌还没有评论」；请求失败才提示获取失败并引导配置自建 API / Cookie。

### 6. 发送与调试 `send`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `send_delay` | float | `0.6` | 分段发送间隔（秒） |
| `reply_with_at` | bool | `false` | 回复时 @ 发送者 |
| `debug_log` | bool | `false` | 输出调试日志（Cookie/Token 已脱敏） |

---

## 工作原理

```
/点歌 歌名 [歌手]
  │
  ├─ parse_command_text  参数解析 + 校验（剔除零宽字符/BOM）
  ├─ check_access        群白黑名单 / 用户黑名单
  ├─ RateLimiter         用户冷却 + 每日限额（asyncio.Lock 保护）
  ├─ resolve_song        搜索 → 选曲（歌手匹配优先）
  │
  ├─ build_card          歌曲卡片（163 / custom / share + 降级）
  ├─ lyrics_flow         取歌词 → render_lyrics → 纯文本兜底
  └─ comments_flow       取评论 → render_comments → 纯文本兜底
```

**目录结构**

```
main.py                 插件入口、指令、编排、消息组装
_conf_schema.json      54 项配置 schema
core/
  models.py            SongInfo / Lyric / CommentPage 等数据模型
  config.py            RuntimeConfig（schema ↔ 代码的唯一映射）
  provider.py          MusicProvider 协议 + 注册表
  search.py            选曲策略
  cards/music_card.py  卡片构造与降级
  lyrics_flow.py       歌词编排
  comments_flow.py     评论编排
  ratelimit.py         冷却与限额
  renderer.py          Renderer 协议 + DefaultRenderer
  netease/             网易云数据面（http / endpoints / parser / provider）
  t2i/                 歌词与评论模板 + 渲染入口
```

---

## 开发与测试

```bash
py -3.12 -m pytest tests -q
```

- 测试需要 AstrBot 参考源码：设 `ASTRBOT_REF` 指向 AstrBot 检出目录，或放到插件父目录的 `_astrbot_ref`
- 所有单测**不联网**：HTTP 一律用本地 mock 端点拦截
- 测试会把 AstrBot 运行期数据目录重定向到临时目录（`ASTRBOT_ROOT`），不会污染仓库

---

## 常见问题

**卡片没出现？** 检查 `card_enable`；若用 `custom`，官方直连通常拿不到音频直链，插件会按 `card_fallback` 降级为分享卡片。

**评论只有几条 / 拿不到？** 官方直连限制较多，建议自建 NeteaseCloudMusicApi 并填 `netease_api_base`。

**歌词图空白很多？** 已修：渲染时按 `lyrics_width` 显式传入视口尺寸，图片高度贴合内容。

**搜不到歌？** 换更精确的歌名或补上歌手；`search_limit` 调大可增加候选。
