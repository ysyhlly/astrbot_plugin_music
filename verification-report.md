# t12 独立验证报告：全链路、配置可调性与降级路径

- 任务：t12（kind=verification，attempt 2，attempt_id 144c3990-e44d-491b-9f5d-35d17fac324c）
- 验证者：verifier（独立验证，非实现者）
- 对象：t8/t9/t10/t11 交付物（main.py、core/**、_conf_schema.json、metadata.yaml）
- 环境：cwd = D:\项目\astrbot_plugin_music；ASTRBOT_REF = D:\项目\_astrbot_ref（AstrBot 4.28.1）；Python = py -3.12（aiohttp 3.14.3 / pydantic 2.13.4 / jinja2 3.1.6 / pillow 12.3.0 / pytest 9.1.1）；全部单测只访问 127.0.0.1 假服务，无真实联网。
- 总体结论：**needs_revision**。7 类验证全部执行；全量 628 用例中 627 passed / 1 failed，唯一红灯是真实实现缺陷 B1（零宽空格被当成合法歌名），按任务书要求保留失败并记为 finding（见第 2 节）。

---

## 1. 七类验证逐条留证

| # | 验证项 | 命令 | 实际结果（真实输出片段） | 结论 |
|---|--------|------|--------------------------|------|
| 1 | 全量套件（跑前先删仓库 data/） | py -3.12 -m pytest tests -q | 1 failed, 627 passed, 3 warnings in 22.79s（exit=1）；收尾打印 data exists after full suite: False | 通过（唯一失败为 finding B1） |
| 2 | 真实 AstrBot 组件契约 | py -3.12 -m pytest tests/test_e2e_pipeline.py -q | 5 passed（含真实 RespondStage._component_validators、toDict()["data"]["type"]、_type 回写三处断言） | 通过 |
| 3 | 端到端 HTTP 全链路 | 同文件 test_end_to_end_self_hosted_full_chain | 假 NeteaseCloudMusicApi 记录到 /cloudsearch → /lyric → /comment/music 顺序；模板数据 max_lines=4、items=3、viewport_width=900/height=100 | 通过 |
| 4 | 配置可调性矩阵（22 组，要求 >= 12） | py -3.12 -m pytest tests/test_config_matrix.py -q | 22 passed | 通过（逐组差异见第 3 节） |
| 5 | 故障注入 5 种 | py -3.12 -m pytest tests/test_failure_modes.py -q | 1 failed, 13 passed（失败即 B1；5 种故障场景全绿） | 通过（结论见第 4 节） |
| 6 | 反例/边界与并发 | 同第 5 项文件 | 空/空白/超长/emoji/特殊字符/纯空格歌手全部不抛异常；并发 3 用户互不串台、冷却按用户隔离 | 通过 |
| 7 | 静态检查 | py -3.12 -m compileall -q . ；py -3.12 -c "import json; json.load(open('_conf_schema.json',encoding='utf-8'))" ；仓库内 D:\ 硬编码扫描 | compileall exit=0；schema json ok；hardcoded D hits: 0 | 通过 |

补充（任务书第 2 条要求「构建 main.py 在典型配置下产出的完整消息链」）：

- tests/test_e2e_pipeline.py::test_production_card_passes_real_respond_stage_validators 走**生产构造路径**（build_card_result → 默认工厂 → astrbot.api.message_components），同时断言：① getattr(comp, "_type", "") == "163"；② comp.toDict()["data"]["type"] == "163"；③ 真实校验器 RespondStage._component_validators[Comp.Music](comp) 为真；④ 插件自身复刻规则与之一致。
- test_full_message_chain_passes_real_stage_checks 复刻 respond/stage.py:109-128 的 _is_empty_message_chain 逻辑（使用真实规则表）断言整链非空，并断言**不出现全空白 Plain**、**Image.file 非空**。
- test_music_without_type_is_rejected_by_real_validator 固定了 _type 陷阱：用 Music(_type="163", ...) 造出的固件 hasattr(comp, "_type") == False、toDict() 无 type 字段、真实校验器**抛 AttributeError**（会被 respond/stage.py:246 吞掉）；生产路径对 163/custom/share 三态均不产出这种组件。

---

## 2. 未通过项（finding）

### B1（medium）零宽空格 / BOM 被当作合法歌名，用户拿到错误引导

- 现象：parse_command_text("/点歌 \u200b\u3000") 返回 keyword='\u200b'、error=''（判定为合法请求）；"/点歌 \ufeff" 同样返回 keyword='\ufeff'、error=''。用户不会收到「请告诉我歌名」，而是收到「没有找到『​』这首歌」。
- 复现命令与输出（脚本 %TEMP%\verify_b1_repro.py，cwd = 仓库根）：

      input=''                       keyword=''       artist=''     error='empty'
      input='/点歌'                  keyword=''       artist=''     error='missing_keyword'
      input='/点歌 晴天 周杰伦'       keyword='晴天'   artist='周杰伦' error=''
      input='/点歌 \u200b\u3000'     keyword='\u200b' artist=''     error=''     <- 缺陷
      input='/点歌 \ufeff'           keyword='\ufeff' artist=''     error=''     <- 缺陷

- 红灯用例：tests/test_failure_modes.py::test_parse_command_text_边界（1 failed，按队长要求**不改测试、保留失败**）。
- 位置：main.py:221-223（keyword = parts[0].strip()[:MAX_KEYWORD_CHARS] 之后仅判 if not keyword）；建议的归一化入口也包括 main.py:125-127 的 _collapse()。
- requiredFix：在 _collapse() 或 parse_command_text 判空前剔除零宽/不可见字符（\u200b、\u200c、\u200d、\ufeff，必要时含 \u2060）再判空；补一条 "/点歌 \u200b" → error="missing_keyword" 的单测。
- 严重度理由：触发条件苛刻（需粘贴零宽字符），但后果是**静默的错误引导**而不是可读提示；同类归一化已在 core/search.py::normalise_keyword 存在，此处属遗漏。

---

## 3. 配置可调性矩阵（22 组：改了哪个键 → 观察到什么变化）

全部证据来自 tests/test_config_matrix.py（22 passed）：HTTP 层用本地假 NeteaseCloudMusicApi 记录请求参数，组件层用真实 AstrBot 组件与真实校验器。

| 配置键 | 改法 | 观测到的差异 | 判定 |
|--------|------|--------------|------|
| search_limit | 2 与 5 | 假 API 收到的 /cloudsearch?limit= 分别为 2 / 5 | 真可调 |
| pick_strategy | top 与 first | 同歌手两候选（700001「晴天 (Live)」先出现、700002「晴天」完全同名）：top→700001，first→700002 | 真可调 |
| card_enable | True 与 False | True→components=[Music]（reason=ok）；False→components=[]（reason=card_disabled，改走纯文本） | 真可调 |
| card_type=163 | 163 | Music._type='163' + toDict()["data"]["type"]='163' + 真实校验器 True | 真可调 |
| card_type=custom | custom（provider 给 audio） | Music._type='custom' 且 url/audio/title 全非空 + 真实校验器 True；audio_url() 被调用 1 次 | 真可调 |
| card_type=custom（无 audio） | custom + card_fallback=share | 退化为 Share 组件（reason 含 custom_no_audio），**不产出会被静默丢弃的非法 custom** | 真可调 |
| card_type=share | share | 产出 Share(url,title)，无 Music，真实 Share 校验器 True | 真可调 |
| card_attach_audio | False 与 True | [Music] 与 [Music, Record]，Record.file == 音频直链 | 真可调 |
| card_show_cover | True 与 False | Music.image = 封面 URL 与空串 | 真可调 |
| card_show_source | True 与 False | 卡片正文含/不含「网易云音乐」 | 真可调 |
| lyrics_enable | True 与 False | True→取歌词 1 次并产出消息；False→**不取数**、流程返回 None | 真可调 |
| lyrics_t2i | True 与 False | True→图（image）；False→纯文本（render_failed），渲染器 0 调用 | 真可调 |
| lyrics_render_mode | network / local / auto | html 成功→网络图且不碰本地；html 失败→回落本地 Markdown **图**；Markdown 也失败→纯文本兜底 | 真可调（语义细节见第 6 节 U1） |
| lyrics_max_lines | 4 / 0 / 8 | 模板 max_lines 与 HTML 中 div class="line" 计数分别为 4/8/8；4 行时含「仅显示前 4 行，已省略 4 行」 | 真可调 |
| lyrics_width | 900 与 600 | 注入渲染器的 screenshot_options()["viewport_width"] 为 900 与 600（viewport_height=100 恒定） | 真可调（t11 修复点） |
| comments_enable | True 与 False | True→取评论 1 次并给「还没有评论」提示；False→**不取数**、返回 None | 真可调 |
| comments_count | 3 与 8 | 请求 limit=3/8，解析出的条目数 3/8 | 真可调 |
| comments_sort | hot 与 new | 请求 sortType=2 与 3；首条评论「热门评论1」与「最新评论1」 | 真可调 |
| comments_max_chars | 3 与 0 | 正文「热门评…」与「热门评论1」 | 真可调 |
| comments_page | 2（count=3） | 请求 offset=3 | 真可调 |
| cooldown_seconds | 60 与 0 | 60→同 key 第二次被拒（提示含「秒」）；0→连续放行；超过窗口后恢复 | 真可调 |
| daily_limit | 2 与 0 | 2→[True, True, False]，第 3 次提示含「上限」；0→连发 5 次全放行 | 真可调 |
| reply_with_at | True 与 False | 首条 chain 前置 At(qq=…)；False 不加；user_id="not-a-number" 时 At 仍合法保留且不抛异常 | 真可调 |

另有 test_matrix_covers_schema_keys 断言：矩阵 22 个键**全部存在于 _conf_schema.json**（防止自说自话）。

---

## 4. 故障注入结论（5 + 1）

| 场景 | 构造 | 观测结果 | 结论 |
|------|------|----------|------|
| (a) 搜索端点 500 | 假 API 对 /cloudsearch 返 500 | status="not_found"，**恰好 1 条**中文提示「没有找到…」；max_retries=0 时 1 次请求，max_retries=2 时 1<=请求<=3；warning <= 3 条 | 无未捕获异常、无刷屏 |
| (b) 搜索返回空 songs | {"code":200,"result":{"songCount":0,"songs":[]}} | 同上单条提示，1 次请求 | 无未捕获异常、无刷屏 |
| (c) 歌词端点超时 | 假 API sleep(1.0) + api_timeout=0.3 | 歌词流程 has_image=False，输出**恰好 1 条**歌词纯文本兜底；warning <= 6 | 无未捕获异常、无刷屏 |
| (d) t2i 端点非 200 | /text2img/generate 返 500（另有「200 但缺 id」变体） | 渲染返回 None → 纯文本兜底；**2 次 POST**：第 1 次带 viewport_width/height、第 2 次不带（t11 兼容性兜底，符合设计） | 无未捕获异常、无刷屏 |
| (e) base_url 指向未监听端口 | 临时占用后释放的端口 | status="not_found" 单条提示，耗时 < 5s 断言通过 | 无未捕获异常、无卡死 |
| (f) 端点拒绝 viewport 键 | 「带 viewport→500、不带→200」的假端点 | 第 1 轮 2 次 POST（带→失败，不带→成功，返回图片 URL）；第 2 轮**只 1 次 POST 且不带 viewport**（拒绝缓存生效，不再重复试探） | 兜底与缓存均按设计工作 |

---

## 5. 反例 / 边界 / 并发

| 用例 | 断言要点 | 结果 |
|------|----------|------|
| 空串 / 纯空格 / None / 只有指令名 | error 属于 {empty, missing_keyword} 且带中文提示 | 通过 |
| 超长歌名（300 字） | 解析截断到 80 字；HTTP keywords 参数 <= 80 字 | 通过 |
| emoji + HTML 标签歌名 | 解析不抛异常、关键词保留 emoji；模板渲染被 autoescape 转义（<script>alert(1)</script> 变成 &lt;script&gt;…） | 通过 |
| 歌手为纯空格 | artist == "" | 通过 |
| 特殊字符 ../../etc/passwd | 视为普通关键词，不抛异常 | 通过 |
| 零宽空格 + 全角空格 | **失败（finding B1）** | 不通过 |
| 并发 3 用户（不同 rate_key） | 各自选到正确歌曲（186016/999/5097785，/song/detail 已按 ids 回显）、都拿到消息；已消费用户被冷却拒绝、新用户仍可点歌 | 通过 |
| 同用户并发 3 次（cooldown=60） | 恰好 1 次 ok、2 次 rate_limited，拒绝消息含「秒」 | 通过 |
| 全量测试后仓库 data/ | 跑前删除 → data exists after full suite: False；test_astrbot_root_is_redirected_outside_repo 断言 ASTRBOT_ROOT 在仓库外 | 通过 |

---

## 6. 不确定项与残留风险（交由 t13 判定）

| 编号 | 内容 | 证据 | 影响 |
|------|------|------|------|
| U1 | lyrics_render_mode="network" 无法阻止 core/t2i 三级链的**本地 Markdown 兜底**：html 失败后仍会调用 text_to_image 得到图片 | test_lyrics_render_mode_network_local_auto：network + html 失败 → image='https://img.example.com/md.png'（star.md_calls == 1） | 与 core/renderer.py:11-14「network：只走网络渲染」的字面语义不完全一致；可能是 t10 的有意设计（三级链固定），但用户无法通过 mode 完全禁用本地渲染 |
| U2 | @register 已废弃 | 全量运行告警：main.py:464: DeprecationWarning: The 'register_star' decorator is deprecated…；依据 _astrbot_ref/astrbot/core/star/register/star.py:15-19（v3.5.19 之后 AstrBot 自动识别 Star 子类） | low：不影响当前运行，未来大版本可能移除；清理属 t13 决策（不在本任务 inScope） |
| U3 | 测试命名空间陷阱：混用 core.* 与 astrbot_plugin_music.core.* 会生成两个模块实例，ensure_runtime_config() 因类不匹配而**静默回落全部默认配置** | 本次 t12 首轮 5 个用例假失败/假通过（例如 lyrics_render_mode="network" 被当成 auto）；统一改用包命名空间导入后消失 | medium（仅测试面）：会制造假红/假绿；建议后续测试统一从 astrbot_plugin_music.* 导入 |
| U4 | 部署版 t2i 服务与本机实测的上游 main 分支可能存在差异（viewport_width/height 支持以实测端点为准） | 见第 7 节：官方端点对未知键返回 200（忽略），对 viewport_height 生效 | low：旧服务忽略未知键，最差退化为修复前行为 |
| U5 | 官方 t2i 服务对**默认 Python UA** 的 GET 会 403（WAF），浏览器 UA 才 200 | 默认 urllib UA → 403；Chrome UA → 200 image/jpeg 21548 bytes | AstrBot core/utils/io.py:113 download_image_by_url 未设 UA，仅在 return_url=False 路径使用；插件 schema 无该键（默认 True），当前无影响 |
| U6 | 非 pytest 入口仍会写仓库 data/ | 本轮观测 3 次（19:40 / 19:43 / 19:46），均发生在「cwd=仓库根 + import astrbot」的脚本；pytest 写入被 conftest 重定向到 %TEMP%\astrbot-test-root-* | 已知副作用，非 pytest 缺陷；.gitignore 已忽略 data/ |
| U7 | 官方直连模式未做真实网络验证 | 任务书禁止单测联网；netease_mode=official_direct 的评论/歌词依赖用户 Cookie | 不确定：真实环境行为（风控/空评论）未在本轮覆盖 |

---

## 7. 补充证据（套件外、真实联网的只读探测，不影响单测禁网约束）

为定位「底部留白」缺陷而做，正好覆盖 t11 的 viewport 修复点（官方端点 https://t2i.soulter.top/text2img/generate，payload 与插件 DefaultRenderer 一致）：

| 请求 options | 出图 | 底部空白 |
|--------------|------|----------|
| {full_page, type:jpeg, quality:80}（修复前） | 800x720 | **360px（50%）** |
| + viewport_height=100 | 800x384 | 24px（= body 下内边距） |
| + viewport_width=900 与 viewport_height=100 | 900x384 | 24px |
| 30 行长歌词 + viewport_height=100 | 800x1542 | 21px（**不截断**） |
| 未知键（definitely_unknown_key） | 800x720 | 与不带未知键逐项一致（HTTP 200，未 500） |

协议与 URL 形态（对照 core/renderer.py::_render_via_endpoint）：POST {base}/generate → {"code":0,"message":"success","data":{"id":"data/rendered_….jpeg"}}；GET {base}/{id} → 200 image/jpeg（与 AstrBot network_strategy.py:189 的拼法一致）。

脚本与产物（均在 %TEMP%，仓库外）：verify_fullpage_probe.py、verify_t2i_viewport_sim.py、verify_remote_protocol_probe.py、verify_remote_url_probe.py、verify_b1_repro.py；出图在 %TEMP%\music_t2i_probe\。

---

## 8. 变更范围与「未修改实现文件」声明

本任务只新增/修改 inScope 的 4 个文件：

| 文件 | 说明 |
|------|------|
| tests/test_e2e_pipeline.py | 5 用例：真实 AstrBot 组件契约、完整消息链、_type 陷阱、端到端全链路、条数受控 |
| tests/test_config_matrix.py | 22 用例：配置可调性矩阵 + 矩阵覆盖度自检 |
| tests/test_failure_modes.py | 14 用例：5 种故障注入 + viewport 拒绝缓存 + 边界/并发 + ASTRBOT_ROOT 隔离 |
| verification-report.md | 本报告 |

实现文件未被本任务修改：main.py / core/** / _conf_schema.json / metadata.yaml 的最后修改时间分别为 19:59:12 / 不晚于 20:01:02 / 19:24:07 / 19:51:51，均**早于**本任务写入窗口（20:09:01–20:16:16）；本任务从未对上述文件执行写操作。仓库当前无 .git，故以 mtime + 写入清单双重佐证。

---

## 9. 复现命令汇总

    cd D:\项目\astrbot_plugin_music
    py -3.12 -m pytest tests -q                                  # 1 failed, 627 passed（失败=B1）
    py -3.12 -m pytest tests/test_e2e_pipeline.py -q             # 5 passed
    py -3.12 -m pytest tests/test_config_matrix.py -q            # 22 passed
    py -3.12 -m pytest tests/test_failure_modes.py -q            # 13 passed, 1 failed(B1)
    py -3.12 -m pytest tests/test_failure_modes.py::test_parse_command_text_边界 -q   # B1 红灯
    py -3.12 -m compileall -q .
    py -3.12 -c "import json; json.load(open('_conf_schema.json',encoding='utf-8')); print('schema json ok')"
