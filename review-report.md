# 质量门评审报告（t15，复审轮）

| 项 | 值 |
|---|---|
| 任务 | t15 质量门评审：AstrBot 契约契合度与可交付性 |
| 评审对象 | t8(契约) → t9(网易云数据面) → t10(t2i 渲染) → t11(卡片/指令/编排) → t14(B1 修复) |
| 评审轮次 | 复审轮（替代因坏依赖取消的 t13） |
| attempt | 5 |
| **评审人** | **captain（接管）** |
| verdict | **needs_revision**（1 项 medium finding，无 blocker/high） |

---

## ⚠️ 独立性披露（必读）

本报告由 **captain 亲自出具，独立性存在折扣**，请据此权衡其证据权重：

1. 原定评审人 `reviewer` 连续 2 次、改派 `verifier` 1 次、新增 `reviewer2` 1 次，**共 4 次均因 DeepSeek Messages transport 失败（code TRANSPORT）中断**，跨 3 个不同会话，属基础设施限制而非实质评审失败。继续重试无意义，经用户连续指示继续推进，captain 接管。
2. captain 是本项目的**任务契约制定者**，也是 `tests/conftest.py`（AstrBot 数据目录隔离）与 `.gitignore` 的**直接修改者**，并编写了 `README.md`。
3. 因此：**本报告对「实现是否满足 captain 自己设定的契约」这一维度具有充分证据力；但对「契约本身是否合理」这一维度缺乏独立性**。若需完全独立的签字，应由人类或一个正常工作的独立评审会话复核。
4. 依据协议「Never approve your own implementation」——本报告**不涉及 captain 的任何实现代码**（实现全部由 architect / provider-dev / render-dev / bot-dev 完成），仅评审他人产出，故不属于自审；但上述 1–3 点的折扣仍然成立。

---

## 一、评审维度与证据

### 维度 1：AstrBot 4.28.1 契约契合度 ✅

| 检查项 | 证据 | 结论 |
|---|---|---|
| pydantic v1 `_type` 陷阱已修复 | 独立构造卡片后实测：`comp._type == '163'`、`toDict()["data"]["type"] == '163'`、`RespondStage._component_validators[Comp.Music](comp) is True` 三者一致 | ✅ 通过 |
| 修复机制正确 | `core/cards/music_card.py` 的 `ensure_music_type` 用 `object.__setattr__` 写回实例 `__dict__`，绕过 pydantic v1 对下划线属性的丢弃 | ✅ 通过 |
| `Music` 基类确认 | `Music.__mro__ → BaseMessageComponent → pydantic.v1.main.BaseModel`（实测），故 `Music(_type=...)` 确实会静默丢字段 | ✅ 判断成立 |
| 端到端消息链通过真实校验器 | `tests/test_e2e_pipeline.py` 5 条全绿，含 `_component_validators` 真实规则、无空 Plain、Image.file 非空 | ✅ 通过 |
| 空链语义处理 | 插件保证退化后链上至少一个合法组件（Share 或非空 Plain），不依赖「非法 Music 被摘除」（该假设错误：respond stage 只在整链全非法时跳过） | ✅ 通过 |

### 维度 2：配置可调性真实性 ⚠️ **发现 1 项 medium**

对 `_conf_schema.json` 全部 **54 个叶子键**逐项核对，方法为「定位真实读取点」而非文本包含：

- **53/54 键**确认被真实读取并影响行为。抽样行为验证（非仅 grep）：

| 键 | 行为验证结果 |
|---|---|
| `group_whitelist` | `check_access` 返回 `''`(放行) vs `'group'`(拒) |
| `group_blacklist` | 命中即 `'group'` |
| `user_blacklist` | 命中即 `'user'` |
| `lyrics_render_mode` | `network/local/auto` 三值均正确传导到 `RenderOptions.mode` |
| `lyrics_t2i_endpoint` | 传导到 lyrics 与 comments 两侧的 `RenderOptions.endpoint` |
| `card_show_source` | `True` → content 非空；`False` → content `''`（卡片可见差异） |
| `search_timeout` | `core/netease/provider.py:213` 实际读取 |
| `card_attach_audio` | `core/cards/music_card.py:412,573` 实际读取 |
| `comments_page` | `core/comments_flow.py:79` 实际读取并计算页码偏移 |
| `send_delay` | `main.py:594` 实际读取并作为分段发送间隔 |

- **`fallback_to_plain` 是死键** —— 详见维度 2 的 finding。

### 维度 3：资源与并发 ✅

- `core/session.py` 提供 async contextmanager 封装，异常路径同样关闭 session。
- `core/ratelimit.py` 用 `asyncio.Lock` 保护冷却与每日限额，`daily_limit=0` 表示不限。
- t12 的并发用例（3 个不同用户）验证互不串台、冷却按用户隔离。

### 维度 4：异常与降级 ✅

- 三级降级链 `HTML → Markdown(仍是图) → 纯文本` 有测试固定；`comments` 语义区分 `None`(失败) vs 空 `CommentPage`(无评论)。
- 敏感信息脱敏：`core/logging_utils.py` 导出 `SENSITIVE_KEYS` 常量并实现 `config_value` 脱敏；t11 自报修过「先脱敏吃掉 `%s` 占位符导致 logging TypeError」的缺陷（改为先 %-格式化再脱敏）。
- **注意**：t12 的故障注入证据为 mock 端点，真实官方端点仅验证过 viewport 相关行为（见 verification-report.md）。

### 维度 5：工程可交付性 ✅

| 文件 | 检查 |
|---|---|
| `metadata.yaml` | name/display_name/short_desc/desc/version/author/repo/astrbot_version(`>=4.16,<5`)/tags 齐备 |
| `requirements.txt` | `aiohttp>=3.9` |
| `README.md` | 54 项参数表经脚本核对与 schema **逐项一致**（脚本验证：`leaves NOT mentioned in README: NONE`） |
| `main.py` ImportError | 提供中文提示 |

### 维度 6：越界检查 ✅

- 无硬编码绝对路径：插件源码 `D:\` 命中 **0**。
- `py -3.12 -m compileall -q main.py core` → exit 0。
- 仓库根 `data/` 不再由 pytest 生成（conftest 隔离 + 已验证）。

---

## 二、Findings

### F1（medium）`source.fallback_to_plain` 声明但从未被读取

| 字段 | 内容 |
|---|---|
| id | F1 |
| severity | medium |
| file | `_conf_schema.json`（source.fallback_to_plain）、`core/config.py`（`RuntimeConfig.fallback_to_plain` 字段） |
| problem | 该键在 schema 中声明、在 `RuntimeConfig` 中有字段、默认值 `true`，但**插件源码中没有任何读取点**。静态扫描：字面量 `fallback_to_plain` 仅出现在 `core/config.py`（定义处），`main.py` 与 `core/**` 的其余模块均无引用；行为验证也无法通过配置它观察到任何输出差异。用户改动此开关不会有任何效果——直接违反「参数可调」的需求。 |
| 影响 | 低风险但确定性存在：用户会误以为关掉它就能禁止纯文本兜底。实际兜底由 `lyrics_fallback_text` 与 `comments_fallback_text` 两个独立键控制，二者**确实生效**。 |
| requiredFix | 三选一：(a) **推荐** 在 `main.py` 的兜底分支真正接入它——作为 `lyrics_fallback_text`/`comments_fallback_text` 的总开关（`fallback_to_plain=False` 时整链不输出纯文本兜底）；(b) 若认为语义重复，从 `_conf_schema.json` 与 `RuntimeConfig` 中**删除**该键，避免死配置误导用户；(c) 在 schema 的 `hint` 中明确标注「预留/暂未生效」，但这只是把问题文档化，不推荐。 |

---

## 三、待裁决问题（t14 提交）

### Q1：`main.py` 的 `@register` 是否清理？→ **裁决：保留，记录为已知项**

依据 `_astrbot_ref/astrbot/core/star/register/star.py:15-19`，该装饰器自 AstrBot **v3.5.19** 起废弃，AstrBot 会自动识别 `Star` 子类。

但 t14 的权衡成立且我认为更重：**它是 `star_map` 中 `name/author` 的唯一来源**。移除后，插件专用日志器将完全依赖加载器注入的类属性；若 `metadata.yaml` 缺失或加载器行为变化，将失去兜底。代价（一条 DeprecationWarning）小于收益（元数据健壮性），且清理会连带修改 t11 已通过的断言。

**结论**：保留。建议在 `main.py` 该装饰器处加一行注释说明「保留原因 + 何时可移除（当 AstrBot 保证从 metadata.yaml 注入 name/author 时）」，并在下个 AstrBot 大版本升级时重新评估。此项记为**已知项（low）**，不阻塞交付。

### Q2：B1 修复是否真正到位？→ **是，已验证**

captain **自行构造输入**独立验证（未复跑 t14 自带用例）：

| 输入 | 结果 | 判定 |
|---|---|---|
| `\u200b` / `\ufeff` / `\u200b\u3000` / `\u200c\u200d` | `error='empty'` | ✅ 不再被当合法歌名 |
| `/点歌 晴\u200b天 周杰伦` | `kw='晴天'` `artist='周杰伦'` | ✅ 保留可见部分，参数解析正确 |
| `\ufeff/点歌 晴天` | `kw='晴天'` | ✅ BOM 前缀可识别 |
| `/点歌 😀😀` | `kw='😀😀'` | ✅ 未误伤 emoji |
| `/点歌 Love-Story` | `kw='Love-Story'` | ✅ 未误伤英文/连字符 |
| `/点歌 ../../etc/passwd` | `kw='../../etc/passwd'` | ✅ 行为不变（非安全边界，仅作歌名） |
| `/点歌 晴天` | `kw='晴天'` | ✅ 正常输入未受影响 |

语义定义为「**删除**不可见字符」而非「当分隔符」，实现与声明一致。

---

## 四、未验证项与不确定性（如实列出）

| id | 内容 |
|---|---|
| U1 | t12 的 22 组配置矩阵**未覆盖全部 54 个叶子键**。本报告已用「读取点定位 + 抽样行为验证」补足至 54/54，但并非每键都有独立的行为级观测（如 `user_agent`/`max_retries` 仅确认读取点存在）。 |
| U2 | 真实官方端点（`t2i.soulter.top`）仅验证过 viewport 与未知键兼容性；生产环境其他行为（限流、超时、模板变更）未验证。 |
| U3 | 真实网易云端点未做端到端联网验证（单测全部 mock）。官方直连的已知限制来自源码与文档分析，非实测穷举。 |
| U4 | `@register` 的移除影响评估基于源码阅读，未实测「移除后 metadata.yaml 缺失时日志器行为」。 |
| U5 | 本轮评审未经独立评审人签字（见独立性披露）。 |
| U6 | t12 的 5 种故障注入均基于 mock 端点，真实网络抖动的表现未观测。 |
| U7 | `tests/` 下三个验证文件由 verifier 编写，本评审**未逐条复核其断言强度**（如是否存在自证式断言）；建议后续由独立会话专审测试质量。 |

---

## 五、verdict

**needs_revision**

- 无 blocker / high 问题。
- 1 项 medium finding（F1：`fallback_to_plain` 死键），确定性存在且直接违反「参数可调」需求，应以最小改动修复。
- 其余 5 个维度全部通过，且关键风险点（pydantic v1 `_type` 陷阱、B1 零宽字符、viewport 留白与宽度失真）均已修复并经独立取证。

修复 F1 后建议再出一轮简短确认（无需重跑全部门禁），即可交付。
