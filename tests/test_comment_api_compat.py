"""Black-box comment API compatibility checks using localhost fixtures only."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.models import SongInfo
from core.netease import provider as provider_module
from core.netease.endpoints import MODE_OFFICIAL, MODE_SELF_HOSTED
from core.netease.http import HttpTransport
from core.netease.provider import NeteaseProvider

SONG = SongInfo(id="123")
CLASSIC_DOCS = {
    "code": 200,
    "endpoints": [{"path": "/comment/music"}, {"path": "/comment/hot"}],
    "login": {"loggedIn": True},
}
MODERN_DOCS = {"code": 200, "endpoints": [{"path": "/comment/new"}]}
pytestmark = pytest.mark.asyncio


def comments(prefix, count=4):
    return [
        {
            "commentId": f"{prefix}-{index}",
            "content": f"{prefix}-{index}",
            "time": 1_700_000_000_000 - index * 1000,
            "user": {"nickname": "listener"},
        }
        for index in range(count)
    ]


class CommentApi:
    def __init__(self, *, docs=CLASSIC_DOCS, layout="flat"):
        self.docs = docs
        self.layout = layout
        self.calls = []
        self.hot = comments("hot")
        self.new = comments("new")
        self.hot_decoy = False
        self.new_failure = None
        self.docs_entered = asyncio.Event()
        self.docs_gate = None

    async def handle(self, request):
        query = dict(request.query)
        self.calls.append((request.path, query, request.headers.get("Cookie", "")))
        if request.path == "/docs":
            self.docs_entered.set()
            if self.docs_gate is not None:
                await self.docs_gate.wait()
            if self.docs is None:
                return web.json_response({"code": 404}, status=404)
            return web.json_response(self.docs)
        if request.path.startswith("/api/v1/resource/comments/"):
            return web.json_response({"code": 200, "comments": self.new, "total": 4, "more": False})
        if request.path == "/comment/new":
            failure = self.new_failure
            if isinstance(failure, int):
                return web.json_response({"code": failure}, status=failure)
            if failure == "business":
                return web.json_response({"code": 403, "message": "Login required"})
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "empty":
                return web.json_response({"code": 200, "data": {"comments": [], "totalCount": 0, "hasMore": False}})
            size = int(query.get("pageSize", 2))
            start = (int(query.get("pageNo", 1)) - 1) * size
            data = {"comments": self.new[start:start + size], "totalCount": len(self.new), "hasMore": start + size < len(self.new)}
            return web.json_response({"code": 200, "data": data})
        count = int(query.get("limit", 2))
        offset = int(query.get("offset", 0))
        if request.path == "/comment/hot":
            key = "comments" if self.layout == "dedicated_comments" else "hotComments"
            payload = {key: self.hot[offset:offset + count], "total": len(self.hot), "hasMore": offset + count < len(self.hot), "topComments": comments("pinned")}
            if self.hot_decoy:
                payload["comments"] = self.new
        elif request.path == "/comment/music":
            payload = {"comments": self.new[offset:offset + count], "total": len(self.new), "more": offset + count < len(self.new)}
            if offset == 0:
                payload["hotComments"] = self.hot
        else:
            return web.json_response({"code": 404}, status=404)
        if self.layout == "nested":
            return web.json_response({"code": 200, "data": payload})
        return web.json_response({"code": 200, **payload})

    def requests(self, path):
        return [query for called_path, query, _ in self.calls if called_path == path]


@contextlib.asynccontextmanager
async def service(api):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", api.handle)
    async with TestServer(app, host="127.0.0.1") as server:
        yield str(server.make_url("/")).rstrip("/")


def config(base, **overrides):
    return {
        "netease_mode": MODE_SELF_HOSTED,
        "netease_api_base": base,
        "max_retries": 0,
        "api_timeout": 0.5,
        "comments_max_chars": 0,
        "cookie": "",
        **overrides,
    }


@contextlib.asynccontextmanager
async def transport_for(kind, cfg):
    if kind == "http_transport":
        async with HttpTransport.from_config(cfg) as transport:
            yield transport
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.5)) as session:
            yield session


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
@pytest.mark.parametrize("layout", ["flat", "nested", "dedicated_comments"])
async def test_known_classic_service_preserves_sort_paging_and_server_login(kind, layout):
    api = CommentApi(layout=layout)
    async with service(api) as base:
        cfg = config(base)
        provider = NeteaseProvider(cfg)
        async with transport_for(kind, cfg) as transport:
            for sort in ("hot", "new"):
                first = await provider.comments(SONG, transport, limit=2, offset=0, sort=sort)
                second = await provider.comments(SONG, transport, limit=2, offset=2, sort=sort)
                assert first is not None and second is not None
                assert [item.content for item in first.items] == [f"{sort}-0", f"{sort}-1"]
                assert [item.content for item in second.items] == [f"{sort}-2", f"{sort}-3"]
                assert first.total == second.total == 4
                assert first.has_more and not second.has_more
    assert api.requests("/comment/new") == []
    assert [query["offset"] for query in api.requests("/comment/hot")] == ["0", "2"]
    assert all(query["type"] == "0" for query in api.requests("/comment/hot"))
    assert [query["offset"] for query in api.requests("/comment/music")] == ["0", "2"]
    assert all(query["limit"] == "2" for path, query, _ in api.calls if path.startswith("/comment/"))
    assert all("sortType" not in query for _, query, _ in api.calls)
    assert all(cookie == "" for _, _, cookie in api.calls)


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_classic_cold_deep_page_needs_only_requested_offset(kind):
    api = CommentApi()
    api.new = comments("new", 80)
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            page = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, offset=50, sort="new")
            assert page is not None
            assert [item.content for item in page.items] == ["new-50", "new-51"]
    assert len(api.requests("/comment/music")) == 1
    assert api.requests("/comment/music")[0]["offset"] == "50"
    assert api.requests("/comment/new") == []


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_unknown_docs_keep_existing_modern_default(kind):
    api = CommentApi(docs=None)
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, sort="hot")
            assert result is not None and len(result.items) == 2
    assert len(api.requests("/comment/new")) == 1
    assert api.requests("/comment/music") == api.requests("/comment/hot") == []


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
@pytest.mark.parametrize("status", [404, 405, 501])
async def test_only_missing_http_endpoint_switches_to_classic(kind, status):
    api = CommentApi(docs=None)
    api.new_failure = status
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, sort="hot")
            assert result is not None
            assert [item.content for item in result.items] == ["hot-0", "hot-1"]
    assert len(api.requests("/comment/new")) == 1
    assert len(api.requests("/comment/hot")) == 1


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_successful_classic_fallback_is_reused_on_second_page(kind):
    api = CommentApi(docs=None)
    api.new_failure = 404
    async with service(api) as base:
        cfg = config(base)
        provider = NeteaseProvider(cfg)
        async with transport_for(kind, cfg) as transport:
            for offset in (0, 2):
                result = await provider.comments(SONG, transport, limit=2, offset=offset, sort="hot")
                assert result is not None
                assert result.items[0].content == f"hot-{offset}"
    assert len(api.requests("/comment/new")) == 1
    assert [query["offset"] for query in api.requests("/comment/hot")] == ["0", "2"]


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_empty_classic_hot_page_does_not_mix_regular_or_pinned_comments(kind):
    api = CommentApi()
    api.hot = []
    api.hot_decoy = True
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, sort="hot")
            assert result is not None and result.items == []
            assert result.total == 0 and not result.has_more
    assert len(api.requests("/comment/hot")) == 1
    assert api.requests("/comment/new") == api.requests("/comment/music") == []


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_unknown_docs_missing_modern_endpoint_can_fallback_on_cold_deep_page(kind):
    api = CommentApi(docs=None)
    api.new_failure = 404
    api.new = comments("new", 80)
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, offset=50, sort="new")
            assert result is not None
            assert [item.content for item in result.items] == ["new-50", "new-51"]
    assert len(api.requests("/comment/new")) == 1
    assert len(api.requests("/comment/music")) == 1
    assert api.requests("/comment/music")[0]["offset"] == "50"


async def test_known_modern_cold_deep_page_still_obeys_cursor_prefetch_budget():
    api = CommentApi(docs=MODERN_DOCS)
    async with service(api) as base:
        cfg = config(base)
        async with transport_for("http_transport", cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, offset=50, sort="new")
            assert result is None
    assert api.requests("/comment/new") == api.requests("/comment/music") == []


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
@pytest.mark.parametrize("failure", [403, 429, 500, "business", "timeout", "empty"])
async def test_auth_rate_errors_timeouts_and_empty_pages_do_not_switch(kind, failure):
    api = CommentApi(docs=None)
    api.new_failure = failure
    async with service(api) as base:
        cfg = config(base)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, sort="hot")
            if failure == "empty":
                assert result is not None and result.items == []
            else:
                assert result is None
    assert api.requests("/comment/hot") == api.requests("/comment/music") == []
    assert len(api.requests("/comment/new")) == 1


async def test_capability_cache_does_not_cross_service_origins():
    classic = CommentApi()
    modern = CommentApi(docs=MODERN_DOCS)
    async with service(classic) as classic_base, service(modern) as modern_base:
        provider = NeteaseProvider(config(classic_base))
        for base in (classic_base, modern_base):
            cfg = config(base)
            provider.configure(cfg)
            async with transport_for("http_transport", cfg) as transport:
                assert await provider.comments(SONG, transport, limit=2, sort="hot") is not None
    assert len(classic.requests("/comment/hot")) == 1
    assert len(modern.requests("/comment/new")) == 1
    assert modern.requests("/comment/hot") == []


async def test_expired_capabilities_are_probed_again(monkeypatch):
    monkeypatch.setattr(provider_module, "COMMENT_API_CACHE_TTL", 0)
    api = CommentApi()
    async with service(api) as base:
        cfg = config(base)
        provider = NeteaseProvider(cfg)
        async with transport_for("http_transport", cfg) as transport:
            assert await provider.comments(SONG, transport, limit=2, sort="hot") is not None
            api.docs = MODERN_DOCS
            assert await provider.comments(SONG, transport, limit=2, sort="hot") is not None
    assert len(api.requests("/docs")) == 2
    assert len(api.requests("/comment/hot")) == len(api.requests("/comment/new")) == 1


async def test_concurrent_capability_lookup_is_shared_and_cancellation_recovers():
    api = CommentApi()
    api.docs_gate = asyncio.Event()
    async with service(api) as base:
        cfg = config(base)
        provider = NeteaseProvider(cfg)
        async with transport_for("http_transport", cfg) as transport:
            first = asyncio.create_task(provider.comments(SONG, transport, limit=2, sort="hot"))
            await asyncio.wait_for(api.docs_entered.wait(), 1)
            second = asyncio.create_task(provider.comments(SONG, transport, limit=2, sort="new"))
            await asyncio.sleep(0)
            api.docs_gate.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert all(result is not None for result in results)
            assert len(api.requests("/docs")) == 1

            other_cfg = config(base, cookie="MUSIC_U=another-test-login")
            provider.configure(other_cfg)
            api.docs_entered.clear()
            api.docs_gate.clear()
            async with transport_for("http_transport", other_cfg) as other_transport:
                cancelled = asyncio.create_task(provider.comments(SONG, other_transport, limit=2, sort="hot"))
                await asyncio.wait_for(api.docs_entered.wait(), 1)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                api.docs_gate.set()
                recovered = await asyncio.wait_for(provider.comments(SONG, other_transport, limit=2, sort="hot"), 2)
                assert recovered is not None and recovered.items[0].content == "hot-0"


@pytest.mark.parametrize("kind", ["http_transport", "bare_session"])
async def test_official_resource_path_and_pagination_are_preserved(kind):
    api = CommentApi()
    async with service(api) as base:
        cfg = config(base, netease_mode=MODE_OFFICIAL)
        async with transport_for(kind, cfg) as transport:
            result = await NeteaseProvider(cfg).comments(SONG, transport, limit=2, offset=2, sort="new")
            assert result is not None
    path, query, _ = api.calls[0]
    assert path == "/api/v1/resource/comments/R_SO_4_123"
    assert query["limit"] == query["offset"] == "2"
    assert api.requests("/docs") == []
