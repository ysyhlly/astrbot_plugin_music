"""aiohttp 传输层：HttpTransport（可配置 base_url / 超时 / 重试 / UA / Cookie）。

契约（本任务冻结）
------------------
- HttpTransport(...) 支持 base_url（自建 NeteaseCloudMusicApi）与官方直连两种模式；
  base_url 为空时按模式取默认主机（官方 https://music.163.com / 自建 http://127.0.0.1:3000），
  显式传入的 base_url 总是覆盖默认主机（便于自建反代与单元测试）。
- async get_json(path, params) -> dict | None：非 2xx、非 JSON、非对象 JSON、网络异常
  统一返回 None 并记日志，绝不向上抛异常。
- 重试：仅 5xx（可配置 retry_statuses）与网络异常（超时/连接失败）退避重试，
  指数退避 delay = retry_backoff * 2**attempt（上限 max_backoff）；4xx 不重试。
- 异步上下文管理器：__aenter__ 确保 session 就绪，__aexit__ 关闭「自己创建」的 session；
  外部注入的 session 不会被关闭（生命周期归调用方，符合 core.provider.Transport 约定）。
- 结构上兼容 core.provider.Transport：额外提供 get/post/request 透传方法；
  另外给出 fetch_json(transport, request, ...)，让裸 aiohttp.ClientSession 也能被
  NeteaseProvider 使用（内部临时包一层，不接管 session 生命周期）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp

from ..config import DEFAULT_USER_AGENT, ensure_runtime_config
from .endpoints import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    MODE_OFFICIAL,
    OFFICIAL_REFERER,
    EndpointRequest,
    join_url,
    normalise_base_url,
    normalise_mode,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RETRY_BACKOFF",
    "HttpTransport",
    "fetch_json",
]

DEFAULT_RETRY_BACKOFF = 0.4
"""首次重试等待秒数（之后指数增长）。"""

MAX_BACKOFF = 5.0
"""单次重试等待上限（秒）。"""

DEFAULT_RETRY_STATUSES: tuple[int, ...] = (500, 502, 503, 504)
"""需要重试的状态码（只含 5xx）。"""

SUCCESS_CODES: tuple[int, ...] = (200,)
"""业务成功码（响应体里的 code 字段）。"""


def _as_float(value: Any, default: float) -> float:
    """安全转 float。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except (TypeError, ValueError):
            return default
    else:
        return default
    if result != result or result in (float("inf"), float("-inf")) or result <= 0:
        return default
    return result


def _as_int(value: Any, default: int) -> int:
    """安全转 int。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _clean_params(params: Any) -> dict[str, Any]:
    """去掉值为 None 的查询参数，返回普通字典。"""
    if not isinstance(params, Mapping):
        return {}
    return {str(key): value for key, value in params.items() if value is not None}


def _coerce_request(request: Any) -> EndpointRequest | None:
    """把 EndpointRequest / 映射 / 2~5 元组统一成 EndpointRequest。"""
    if isinstance(request, EndpointRequest):
        return request
    if isinstance(request, Mapping):
        return EndpointRequest(
            path=str(request.get("path") or ""),
            params=_clean_params(request.get("params")),
            method=str(request.get("method") or "GET"),
            data=_clean_params(request.get("data")),
            timeout=request.get("timeout"),
        )
    if isinstance(request, (tuple, list)):
        parts = list(request)
        if not parts:
            return None
        path = str(parts[0] or "")
        if len(parts) == 1:
            return EndpointRequest(path=path)
        if len(parts) == 2 and isinstance(parts[1], Mapping):
            return EndpointRequest(path=path, params=_clean_params(parts[1]))
        method = str(parts[1] or "GET")
        params = _clean_params(parts[2]) if len(parts) > 2 else {}
        data = _clean_params(parts[3]) if len(parts) > 3 else {}
        timeout = parts[4] if len(parts) > 4 else None
        return EndpointRequest(
            path=path, params=params, method=method, data=data, timeout=timeout
        )
    return None


class HttpTransport:
    """基于 aiohttp.ClientSession 的异步 HTTP 客户端（失败返回 None，不抛异常）。"""

    def __init__(
        self,
        *,
        base_url: Any = "",
        mode: Any = MODE_OFFICIAL,
        timeout: Any = DEFAULT_TIMEOUT,
        max_retries: Any = DEFAULT_MAX_RETRIES,
        user_agent: Any = "",
        cookie: Any = "",
        headers: Mapping[str, Any] | None = None,
        session: Any = None,
        retry_backoff: Any = DEFAULT_RETRY_BACKOFF,
        max_backoff: Any = MAX_BACKOFF,
        retry_statuses: Any = DEFAULT_RETRY_STATUSES,
        sleep: Any = None,
        check_code: bool = True,
    ) -> None:
        """创建传输层。

        session 参数用于注入外部 aiohttp.ClientSession（测试/复用连接池），
        注入后 HttpTransport 不会关闭它。
        sleep 参数用于注入等待函数（默认 asyncio.sleep，测试可替换以避免真实等待）。
        """
        self._mode = normalise_mode(mode)
        self._explicit_base = bool(str(base_url or "").strip())
        self._base_url = normalise_base_url(base_url, mode=self._mode)
        self._timeout = _as_float(timeout, DEFAULT_TIMEOUT)
        self._max_retries = max(0, _as_int(max_retries, DEFAULT_MAX_RETRIES))
        self._user_agent = str(user_agent or "").strip() or DEFAULT_USER_AGENT
        self._cookie = str(cookie or "").strip()
        self._extra_headers = {
            str(key): str(value)
            for key, value in (headers or {}).items()
            if value is not None
        }
        self._retry_backoff = max(0.0, _as_float(retry_backoff, DEFAULT_RETRY_BACKOFF)) if retry_backoff else 0.0
        self._max_backoff = max(0.0, _as_float(max_backoff, MAX_BACKOFF)) if max_backoff else 0.0
        self._retry_statuses = tuple(
            _as_int(item, 0) for item in (retry_statuses or ())
        )
        self._sleep = sleep or asyncio.sleep
        self._check_code = bool(check_code)
        self._injected = session
        self._session: Any = session
        self._owns_session = session is None

    # ------------------------------------------------------------ 只读属性

    @property
    def mode(self) -> str:
        """official_direct 或 self_hosted_api。"""
        return self._mode

    @property
    def base_url(self) -> str:
        """请求主机（不含尾部斜杠）。"""
        return self._base_url

    @property
    def explicit_base_url(self) -> bool:
        """是否由调用方显式指定了 base_url。"""
        return self._explicit_base

    @property
    def timeout(self) -> float:
        """默认请求超时（秒）。"""
        return self._timeout

    @property
    def max_retries(self) -> int:
        """最大重试次数。"""
        return self._max_retries

    @property
    def cookie(self) -> str:
        """已配置的 Cookie（日志中绝不输出明文）。"""
        return self._cookie

    @property
    def user_agent(self) -> str:
        """当前 User-Agent。"""
        return self._user_agent

    @property
    def session(self) -> Any:
        """底层 aiohttp session（可能为 None，访问前会惰性创建）。"""
        return self._session

    @property
    def closed(self) -> bool:
        """自身创建的 session 是否已关闭。"""
        if self._injected is not None:
            return bool(getattr(self._injected, "closed", False))
        if self._session is None:
            return True
        return bool(getattr(self._session, "closed", False))

    @property
    def is_official(self) -> bool:
        """是否官方直连模式。"""
        return self._mode == MODE_OFFICIAL

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, cfg: Any) -> HttpTransport:
        """由 RuntimeConfig / 配置映射构造（主要供 main.py 使用）。"""
        config = ensure_runtime_config(cfg)
        return cls(
            base_url=config.netease_api_base,
            mode=config.netease_mode,
            timeout=config.api_timeout,
            max_retries=config.max_retries,
            user_agent=config.user_agent,
            cookie=config.cookie,
        )

    def default_headers(self) -> dict[str, str]:
        """请求头：浏览器 UA + Referer（官方直连必需）+ 可选 Cookie。"""
        headers = {
            "User-Agent": self._user_agent,
            "Referer": OFFICIAL_REFERER,
            "Accept": "application/json, text/plain, */*",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        headers.update(self._extra_headers)
        return headers

    def build_url(self, path: Any) -> str:
        """把相对路径拼到 base_url 上（绝对 URL 原样返回）。"""
        return join_url(self._base_url, path)

    def _cookie_param(self) -> dict[str, Any]:
        """自建 API 支持用 cookie 查询参数注入登录态；官方直连走请求头。"""
        if self._cookie and self._mode != MODE_OFFICIAL:
            return {"cookie": self._cookie}
        return {}

    def _ensure_session(self) -> Any:
        """惰性创建/复用 session（外部注入的 session 原样返回）。"""
        if self._injected is not None:
            return self._injected
        session = self._session
        if session is None or getattr(session, "closed", False):
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
            self._session = session
        return session

    async def _backoff(self, attempt: int) -> None:
        """指数退避等待（sleep 可注入，默认 asyncio.sleep）。"""
        if self._retry_backoff <= 0:
            return
        delay = min(self._retry_backoff * (2 ** attempt), self._max_backoff)
        if delay <= 0:
            return
        logger.debug("第 %d 次重试前等待 %.2fs", attempt + 1, delay)
        try:
            await self._sleep(delay)
        except Exception as exc:  # pragma: no cover - 极端情况下的等待失败
            logger.debug("退避等待异常：%s", exc)

    async def _read_json(self, response: Any) -> dict[str, Any] | None:
        """读取并校验 JSON 响应体（失败返回 None）。"""
        try:
            payload = await response.json(content_type=None)
        except Exception as exc:
            snippet = ""
            try:
                text = await response.text()
                snippet = str(text or "")[:200]
            except Exception:  # pragma: no cover - body 已被消费
                snippet = ""
            logger.warning("响应不是合法 JSON（%s）：%s", exc, snippet)
            return None
        if not isinstance(payload, Mapping):
            logger.warning("响应 JSON 不是对象（%s），已忽略", type(payload).__name__)
            return None
        return dict(payload)

    @staticmethod
    def _validate_code(payload: dict[str, Any]) -> dict[str, Any] | None:
        """校验响应体里的业务 code（code 缺失或不可解释时视为成功）。"""
        code = payload.get("code")
        if code is None:
            return payload
        try:
            number = int(code)
        except (TypeError, ValueError):
            return payload
        if number in SUCCESS_CODES:
            return payload
        message = payload.get("msg") or payload.get("message") or ""
        logger.warning("接口返回 code=%s：%s", code, message)
        return None

    async def request_json(
        self,
        method: Any,
        path: Any,
        *,
        params: Any = None,
        data: Any = None,
        timeout: Any = None,
        check_code: bool | None = None,
    ) -> dict[str, Any] | None:
        """发起请求并返回 JSON 对象；任何失败都返回 None（不抛异常）。"""
        text_path = str(path or "").strip()
        if not text_path:
            logger.warning("请求路径为空，已忽略")
            return None
        url = self.build_url(text_path)
        verb = str(method or "GET").upper()
        query = _clean_params(params)
        if not data:
            query.update(self._cookie_param())
        body = _clean_params(data) if data else None
        if body is not None:
            body.update(self._cookie_param())
        verify_code = self._check_code if check_code is None else bool(check_code)
        per_request_timeout = _as_float(timeout, 0.0)
        attempt = 0
        while True:
            try:
                session = self._ensure_session()
                kwargs: dict[str, Any] = {"headers": self.default_headers()}
                if query:
                    kwargs["params"] = query
                if body is not None:
                    kwargs["data"] = body
                if per_request_timeout > 0:
                    kwargs["timeout"] = aiohttp.ClientTimeout(total=per_request_timeout)
                async with session.request(verb, url, **kwargs) as response:
                    status = _as_int(getattr(response, "status", 0), 0)
                    if status in self._retry_statuses and attempt < self._max_retries:
                        attempt += 1
                        await self._backoff(attempt - 1)
                        continue
                    if not 200 <= status < 300:
                        logger.warning("请求 %s %s 返回 HTTP %s，已放弃", verb, url, status)
                        return None
                    payload = await self._read_json(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await self._backoff(attempt - 1)
                    continue
                logger.warning("请求 %s %s 失败：%r", verb, url, exc)
                return None
            if payload is None:
                return None
            return self._validate_code(payload) if verify_code else payload

    async def get_json(
        self,
        path: Any,
        params: Any = None,
        *,
        timeout: Any = None,
        check_code: bool | None = None,
    ) -> dict[str, Any] | None:
        """GET 请求并解析 JSON 对象（失败返回 None）。"""
        return await self.request_json(
            "GET", path, params=params, timeout=timeout, check_code=check_code
        )

    async def post_json(
        self,
        path: Any,
        data: Any = None,
        *,
        params: Any = None,
        timeout: Any = None,
        check_code: bool | None = None,
    ) -> dict[str, Any] | None:
        """POST 表单请求并解析 JSON 对象（失败返回 None）。"""
        return await self.request_json(
            "POST", path, params=params, data=data, timeout=timeout, check_code=check_code
        )

    async def fetch(self, request: Any) -> dict[str, Any] | None:
        """执行 EndpointRequest（或映射 / 元组描述）描述的请求。"""
        parsed = _coerce_request(request)
        if parsed is None:
            logger.warning("无法识别的请求描述：%r", type(request).__name__)
            return None
        return await self.request_json(
            parsed.method or "GET",
            parsed.path,
            params=parsed.params,
            data=parsed.data,
            timeout=parsed.timeout,
        )

    async def close(self) -> None:
        """关闭自身创建的 session（外部注入的 session 不动）。"""
        if self._injected is not None:
            return
        session, self._session = self._session, None
        if session is None:
            return
        try:
            if not getattr(session, "closed", False):
                await session.close()
        except Exception as exc:  # pragma: no cover - 关闭异常不影响调用方
            logger.debug("关闭 session 失败：%s", exc)

    async def __aenter__(self) -> HttpTransport:
        """进入上下文：确保 session 就绪。"""
        self._ensure_session()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """退出上下文：关闭自身创建的 session。"""
        await self.close()
        return False

    # ------------------------------------------------------------ Transport 兼容

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """透传给底层 aiohttp session（不做 base_url 拼接与重试）。"""
        return self._ensure_session().request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        """透传给底层 aiohttp session（不做 base_url 拼接与重试）。"""
        return self._ensure_session().get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """透传给底层 aiohttp session（不做 base_url 拼接与重试）。"""
        return self._ensure_session().post(url, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return (
            f"HttpTransport(mode={self._mode!r}, base_url={self._base_url!r}, "
            f"timeout={self._timeout}, max_retries={self._max_retries})"
        )


async def fetch_json(
    transport: Any,
    request: Any,
    *,
    mode: Any = "",
    base_url: Any = "",
    timeout: Any = None,
    max_retries: Any = 0,
) -> dict[str, Any] | None:
    """用任意 transport 执行一次接口请求，返回 JSON 对象（失败返回 None）。

    支持三种 transport 形态（provider 因此既能收到 HttpTransport，也能收到
    core.provider.Transport 约定的裸 aiohttp.ClientSession，甚至只有
    get_json/post_json 的鸭子类型对象）：
    1. 带 fetch 的对象（HttpTransport）：直接调用；
    2. 带 request_json 的对象：按其签名调用；
    3. 只有 get_json/post_json，或干脆是 aiohttp.ClientSession：
       临时包一层 HttpTransport（不接管 session 生命周期，close 为 no-op），
       主机取显式 base_url，否则按 mode 取默认主机。
    """
    if transport is None:
        return None
    parsed = _coerce_request(request)
    if parsed is None:
        logger.warning("无法识别的请求描述：%r", type(request).__name__)
        return None
    fetch = getattr(transport, "fetch", None)
    if callable(fetch):
        try:
            return await fetch(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("transport.fetch 失败：%r", exc)
            return None
    request_json = getattr(transport, "request_json", None)
    if callable(request_json):
        try:
            return await request_json(
                parsed.method or "GET",
                parsed.path,
                params=parsed.params,
                data=parsed.data,
                timeout=parsed.timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("transport.request_json 失败：%r", exc)
            return None
    getter = getattr(transport, "get_json", None)
    poster = getattr(transport, "post_json", None)
    if callable(getter) and callable(poster):
        try:
            if (parsed.method or "GET").upper() == "POST":
                return await poster(parsed.path, data=parsed.data, params=parsed.params)
            return await getter(parsed.path, parsed.params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("transport.get_json 失败：%r", exc)
            return None
    adapter = HttpTransport(
        session=transport,
        mode=normalise_mode(mode) if str(mode or "").strip() else MODE_OFFICIAL,
        base_url=base_url,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        max_retries=max_retries,
    )
    try:
        return await adapter.fetch(
            EndpointRequest(
                path=parsed.path,
                params=parsed.params,
                method=parsed.method,
                data=parsed.data,
                timeout=parsed.timeout or timeout,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("裸 session 请求失败：%r", exc)
        return None
