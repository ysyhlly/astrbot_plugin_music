"""网易云真实请求契约回归；所有 HTTP 只访问 localhost。

新版评论模块固定来源：
https://github.com/nooblong/NeteaseCloudMusicApiBackup/blob/
ed28a571a6965f7164fd81b5c4b41098dbe624d4/module/comment_new.js
服务器 Cookie 头解析及默认 URL 日志：同提交 server.js:166,216,241。
"""

import asyncio
import contextlib
import json
import shutil
import subprocess
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.config import RuntimeConfig
from core.models import SongInfo
from core.netease.endpoints import MODE_OFFICIAL, MODE_SELF_HOSTED, build_comments_request
from core.netease.http import HttpTransport
from core.netease.provider import NeteaseProvider
from core.netease import provider as provider_module
from core.session import transport_session


# 原上游完整模块，注入 option/request 后验证转发参数，不下载或请求远端。
UPSTREAM_COMMENT_NEW = r"""const { resourceTypeMap } = require('../util/config.json')
// 评论

const createOption = require('../util/option.js')
module.exports = (query, request) => {
  query.type = resourceTypeMap[query.type]
  const threadId = query.type + query.id
  const pageSize = query.pageSize || 20
  const pageNo = query.pageNo || 1
  let sortType = Number(query.sortType) || 99
  if (sortType === 1) {
    sortType = 99
  }
  let cursor = ''
  switch (sortType) {
    case 99:
      cursor = (pageNo - 1) * pageSize
      break
    case 2:
      cursor = 'normalHot#' + (pageNo - 1) * pageSize
      break
    case 3:
      cursor = query.cursor || '0'
      break
    default:
      break
  }
  const data = {
    threadId: threadId,
    pageNo,
    showInner: query.showInner || true,
    pageSize,
    cursor: cursor,
    sortType: sortType, //99:按推荐排序,2:按热度排序,3:按时间排序
  }
  return request(`/api/v2/resource/comments`, data, createOption(query))
}
"""


def test_requests_reach_pinned_upstream_sorting_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("执行固定上游 JS 契约需 Node；HTTP 契约回归仍独立运行")
    hot = build_comments_request("123", mode=MODE_SELF_HOSTED, limit=10, offset=20, sort="hot")
    new = build_comments_request("123", mode=MODE_SELF_HOSTED, limit=10, offset=20, sort="new", cursor="1700000000000")
    assert hot.path == new.path == "/comment/new"
    harness = """
const vm = require('vm');
let input = ''; process.stdin.on('data', x => input += x);
process.stdin.on('end', () => {
  const fixture = JSON.parse(input), module = {exports: {}};
  vm.runInNewContext(fixture.source, {module,
    require: path => path.endsWith('config.json') ? {resourceTypeMap:{0:'R_SO_4_'}} : (()=>({}))});
  const results = fixture.queries.map(query => module.exports(query, (path, data) => ({path, data})));
  process.stdout.write(JSON.stringify(results));
});
"""
    proc = subprocess.run([node, "-e", harness], input=json.dumps({"source": UPSTREAM_COMMENT_NEW, "queries": [hot.params, new.params]}), text=True, capture_output=True, check=True)
    actual_hot, actual_new = json.loads(proc.stdout)
    assert actual_hot["data"] == {"threadId": "R_SO_4_123", "pageNo": 3, "showInner": True, "pageSize": 10, "cursor": "normalHot#20", "sortType": 2}
    assert actual_new["data"]["sortType"] == 3
    assert actual_new["data"]["cursor"] == "1700000000000"


@pytest.mark.parametrize("mode", [MODE_OFFICIAL, MODE_SELF_HOSTED])
@pytest.mark.parametrize("configured_cookie", ["MUSIC_U=DUMMY_SECRET; __csrf=DUMMY_CSRF", "MUSIC_U=DUMMY_SECRET;__csrf=DUMMY_CSRF"])
async def test_cookie_auth_uses_header_without_url_or_form_secret(mode, configured_cookie):
    sentinel = "MUSIC_U=DUMMY_SECRET; __csrf=DUMMY_CSRF"
    calls = []

    async def endpoint(request):
        body = dict(await request.post()) if request.method == "POST" else {}
        calls.append((request.raw_path, body, request.headers.get("Cookie")))
        # 上游 server.js 使用 Cookie 请求头，无需查询参数或表单复制。
        assert request.headers.get("Cookie") == sentinel
        return web.json_response({"code": 200})

    app = web.Application()
    app.router.add_route("*", "/auth", endpoint)
    async with TestServer(app, host="127.0.0.1") as server:
        cfg = RuntimeConfig.from_mapping({"netease_mode": mode, "netease_api_base": str(server.make_url("/")).rstrip("/"), "cookie": configured_cookie, "debug_log": False, "max_retries": 0})
        async with transport_session(cfg) as transport:
            assert await transport.get_json("/auth", {"id": "1"}) is not None
            assert await transport.post_json("/auth", {"id": "1"}) is not None
    assert len(calls) == 2
    assert all("DUMMY_SECRET" not in url and "cookie" not in body for url, body, _ in calls)


class SortedApi:
    def __init__(self):
        self.calls = []
        self.headers = []
        self.fail_page = None
        self.omit_cursor = False
        self.delay = 0

    async def handle(self, request):
        query = dict(request.query)
        self.calls.append(query)
        self.headers.append(dict(request.headers))
        assert query["type"] == "0"
        assert "offset" not in query and "limit" not in query
        if self.delay:
            await asyncio.sleep(self.delay)
        page = int(query["pageNo"])
        size = int(query["pageSize"])
        if page == self.fail_page:
            return web.json_response({"code": 500}, status=500)
        shift = 10_000_000 if request.headers.get("Cookie") == "MUSIC_U=B" else 0
        items = [{"commentId": i, "content": f"comment-{i}", "likedCount": i,
                  "time": 1_700_000_000_000 + shift - i * 1000,
                  "user": {"nickname": "test"}} for i in range(1, 201)]
        if query["sortType"] == "2":
            items.reverse()
            start = (page - 1) * size
        else:
            cursor = query.get("cursor", "0")
            if cursor == "0":
                if page != 1:
                    return web.json_response({"code": 400}, status=400)
                start = 0
            else:
                matches = [i for i, item in enumerate(items) if str(item["time"]) == cursor]
                if not matches:
                    return web.json_response({"code": 400}, status=400)
                start = matches[0] + 1
        selected = items[start:start + size]
        cursor = str(selected[-1]["time"]) if selected else "0"
        if self.omit_cursor:
            selected = [{key: val for key, val in item.items() if key != "time"} for item in selected]
        data = {"comments": selected, "totalCount": len(items), "hasMore": start + size < len(items)}
        if not self.omit_cursor:
            data["cursor"] = cursor
        return web.json_response({"code": 200, "data": data})


@contextlib.asynccontextmanager
async def sorted_api():
    api = SortedApi()
    app = web.Application()

    async def docs(request):
        return web.json_response({
            "code": 200, "endpoints": [{"path": "/comment/new"}],
        })

    app.router.add_get("/docs", docs)
    app.router.add_get("/comment/new", api.handle)
    async with TestServer(app, host="127.0.0.1") as server:
        api.base_url = str(server.make_url("/")).rstrip("/")
        yield api


def config(base, cookie=""):
    return RuntimeConfig.from_mapping({"netease_mode": MODE_SELF_HOSTED, "netease_api_base": base, "cookie": cookie, "max_retries": 0})


async def test_hot_jumps_to_requested_page_and_new_uses_time_boundaries():
    async with sorted_api() as api:
        cfg = config(api.base_url)
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            hot = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="hot")
            assert [c.content for c in hot.items] == ["comment-197", "comment-196", "comment-195"]
            assert len(api.calls) == 1 and api.calls[0]["pageNo"] == "2"
            api.calls.clear()
            newest = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=6, sort="new")
            assert [c.content for c in newest.items] == ["comment-7", "comment-8", "comment-9"]
            assert [q["pageNo"] for q in api.calls] == ["1", "2", "3"]
            assert [q["cursor"] for q in api.calls] == ["0", "1699999997000", "1699999994000"]
            assert newest.total == 200 and newest.has_more
            api.calls.clear()
            next_page = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=9, sort="new")
            assert next_page.items[0].content == "comment-10"
            assert len(api.calls) == 1 and api.calls[0]["cursor"] == "1699999991000"


@pytest.mark.parametrize("sort,expected", [("hot", [196, 195, 194]), ("new", [5, 6, 7])])
async def test_non_aligned_offset_preserves_requested_slice(sort, expected):
    async with sorted_api() as api:
        cfg = config(api.base_url)
        async with transport_session(cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SongInfo(id="1"), transport, limit=3, offset=4, sort=sort)
        assert [c.content for c in result.items] == [f"comment-{i}" for i in expected]


async def test_new_deep_cold_page_fails_without_unbounded_requests():
    async with sorted_api() as api:
        cfg = config(api.base_url)
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            assert await provider.comments(SongInfo(id="1"), transport, limit=1, offset=999, sort="new") is None
            assert api.calls == []
            page21 = await provider.comments(SongInfo(id="1"), transport, limit=1, offset=20, sort="new")
            assert page21.items[0].content == "comment-21"
            assert len(api.calls) == 21
            api.calls.clear()
            page22 = await provider.comments(SongInfo(id="1"), transport, limit=1, offset=21, sort="new")
            assert page22.items[0].content == "comment-22" and len(api.calls) == 1


async def test_cursor_cache_isolates_host_and_login_cookie():
    async with sorted_api() as first, sorted_api() as second:
        cfg = config(first.base_url, "MUSIC_U=A")
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            await provider.comments(SongInfo(id="1"), transport, limit=3, sort="new")
        other_cfg = config(second.base_url, "MUSIC_U=A")
        provider.configure(other_cfg)
        async with transport_session(other_cfg) as transport:
            result = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new")
        assert result.items[0].content == "comment-4"
        assert [q["pageNo"] for q in second.calls] == ["1", "2"]
        second.calls.clear()
        other_cookie = config(second.base_url, "MUSIC_U=B")
        provider.configure(other_cookie)
        async with transport_session(other_cookie) as transport:
            result = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new")
        assert result.items[0].content == "comment-4"
        assert [q["pageNo"] for q in second.calls] == ["1", "2"]
        assert second.calls[-1]["cursor"] == "1700009997000"


async def test_failed_cursor_walk_does_not_commit_partial_cache():
    async with sorted_api() as api:
        cfg = config(api.base_url)
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            api.fail_page = 3
            assert await provider.comments(SongInfo(id="1"), transport, limit=3, offset=6, sort="new") is None
            api.fail_page = None
            api.calls.clear()
            result = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=6, sort="new")
        assert result.items[0].content == "comment-7"
        assert [q["pageNo"] for q in api.calls] == ["1", "2", "3"]


async def test_expired_cursor_relocates_from_first_page(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(provider_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async with sorted_api() as api:
        cfg = config(api.base_url)
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new")
            api.calls.clear()
            await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new")
            assert [q["pageNo"] for q in api.calls] == ["2"]
            clock[0] += 121
            api.calls.clear()
            result = await provider.comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new")
        assert result.items[0].content == "comment-4"
        assert [q["pageNo"] for q in api.calls] == ["1", "2"]


async def test_cancelled_walk_releases_lock_without_committing_partial_cursor():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Transport:
        mode = MODE_SELF_HOSTED
        base_url = "http://offline.invalid"
        cookie = ""

        def __init__(self):
            self.pause = True
            self.calls = []

        async def fetch(self, request):
            page = request.params["pageNo"]
            self.calls.append(page)
            if self.pause and page == 2:
                entered.set()
                await release.wait()
            start = (page - 1) * 2
            comments = [{"commentId": i, "content": f"comment-{i}", "time": 1700000000000 - i * 1000} for i in range(start + 1, start + 3)]
            return {"code": 200, "data": {"comments": comments, "totalCount": 20, "hasMore": True}}

    transport = Transport()
    provider = NeteaseProvider(config(transport.base_url))
    task = asyncio.create_task(provider.comments(SongInfo(id="1"), transport, limit=2, offset=4, sort="new"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    transport.pause = False
    transport.calls.clear()
    result = await provider.comments(SongInfo(id="1"), transport, limit=2, offset=4, sort="new")
    assert result.items[0].content == "comment-5"
    assert transport.calls == [1, 2, 3]


async def test_missing_timestamp_refuses_incorrect_new_page():
    async with sorted_api() as api:
        api.omit_cursor = True
        cfg = config(api.base_url)
        async with transport_session(cfg) as transport:
            assert await NeteaseProvider(cfg).comments(SongInfo(id="1"), transport, limit=3, offset=3, sort="new") is None
        assert len(api.calls) == 1


async def test_concurrent_new_page_requests_share_consistent_cursor_walk():
    async with sorted_api() as api:
        api.delay = 0.005
        cfg = config(api.base_url)
        provider = NeteaseProvider(cfg)
        async with transport_session(cfg) as transport:
            results = await asyncio.gather(*(provider.comments(SongInfo(id="1"), transport, limit=3, offset=6, sort="new") for _ in range(2)))
        assert all(result.items[0].content == "comment-7" for result in results)
        assert [q["pageNo"] for q in api.calls] == ["1", "2", "3", "3"]


async def test_bare_client_session_keeps_configured_cookie_and_user_agent():
    async with sorted_api() as api:
        cfg = RuntimeConfig.from_mapping({"netease_mode": MODE_SELF_HOSTED, "netease_api_base": api.base_url, "cookie": "MUSIC_U=B", "user_agent": "ConfiguredUA/1.0", "max_retries": 0})
        async with aiohttp.ClientSession() as session:
            result = await NeteaseProvider(cfg).comments(SongInfo(id="1"), session, limit=3, sort="new")
            assert not session.closed
        assert result.items[0].time
        assert api.calls[0]["cursor"] == "0"
        assert api.headers[0]["Cookie"] == "MUSIC_U=B"
        assert api.headers[0]["User-Agent"] == "ConfiguredUA/1.0"
