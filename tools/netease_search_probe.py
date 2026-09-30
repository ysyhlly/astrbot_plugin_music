"""点歌插件搜索自检脚本（单文件，无需依赖本插件）。

用法：把本文件拷到 AstrBot 所在机器上，然后运行
    py -3.12 netease_search_probe.py ラストティーン
（若机器人跑在 Docker 里，就在容器内执行）

它会逐段告诉你：官方搜索接口通不通、返回了几首歌、插件拿到手会选哪首。
"""
import asyncio
import json
import sys

import aiohttp

OFFICIAL = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def probe_official(keyword: str) -> None:
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    print(f"[1] 官方直连搜索：{OFFICIAL}/api/cloudsearch/pc  s={keyword!r}")
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.post(
                OFFICIAL + "/api/cloudsearch/pc",
                data={"s": keyword, "type": "1", "limit": "5", "offset": "0"},
            ) as r:
                body = await r.text()
                print(f"    HTTP {r.status}  响应长度 {len(body)}")
                if r.status != 200:
                    print("    ✗ 接口未返回 200 —— 该机器的网络到网易云有问题")
                    print("    响应头:", dict(r.headers)[:0] or "见上")
                    return
                try:
                    payload = json.loads(body)
                except Exception as e:
                    print("    ✗ 返回的不是 JSON:", e, body[:200])
                    return
    except Exception as e:
        print(f"    ✗ 请求失败：{type(e).__name__}: {e}")
        print("    → 说明这台机器访问不到 music.163.com（防火墙/DNS/代理/容器网络）")
        return

    result = payload.get("result") or {}
    songs = result.get("songs")
    print(f"    code={payload.get('code')}  songCount={result.get('songCount')}  songs={len(songs) if songs else 0}")
    if not songs:
        print("    ✗ 接口没返回任何歌曲 —— 上游搜索此刻对该关键词无结果")
        return
    print("    ✓ 前 3 条：")
    for s in songs[:3]:
        artists = [a.get("name") for a in (s.get("ar") or s.get("artists") or [])]
        print(f"       id={s.get('id')}  歌名={s.get('name')!r}  歌手={artists}")
    print("    → 若这里能看到你要的歌，但机器人仍说找不到，问题出在插件侧（版本/配置），不是网络。")


async def probe_custom(base_url: str, keyword: str) -> None:
    base = base_url.rstrip("/")
    print(f"[2] 自建 API 搜索：{base}/cloudsearch  keywords={keyword!r}")
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(
                base + "/cloudsearch",
                params={"keywords": keyword, "type": 1, "limit": 5, "offset": 0, "total": "true"},
            ) as r:
                body = await r.text()
                print(f"    HTTP {r.status}  响应长度 {len(body)}")
                if r.status != 200:
                    print("    ✗ 自建 API 未返回 200 —— 服务没起来 / 地址不对")
                    return
                payload = json.loads(body)
                songs = (payload.get("result") or {}).get("songs") or []
                print(f"    songs={len(songs)}")
                for s in songs[:3]:
                    print(f"       id={s.get('id')}  歌名={s.get('name')!r}")
                if not songs:
                    print("    ✗ 自建 API 返回空结果")
    except Exception as e:
        print(f"    ✗ 请求失败：{type(e).__name__}: {e}")
        print("    → 若你配了 netease_api_base，请确认该地址在本机可访问")


async def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "ラストティーン"
    base = sys.argv[2] if len(sys.argv) > 2 else ""
    print("=" * 60)
    print(f"关键词: {keyword!r}   字节: {keyword.encode('utf-8')}")
    print("=" * 60)
    if base:
        await probe_custom(base, keyword)
    else:
        await probe_official(keyword)
        print()
        print("[提示] 若你在插件里配了 netease_api_base（自建 API），请把它作为第 2 个参数传入：")
        print("       py -3.12 netease_search_probe.py ラストティーン http://127.0.0.1:3000")


if __name__ == "__main__":
    asyncio.run(main())
