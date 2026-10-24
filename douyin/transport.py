"""HTTP for the Douyin web API, looking like the browser it claims to be.

Douyin scores the caller of its web endpoints, and the TLS/HTTP2 handshake
is part of that score: aiohttp's handshake is plainly Python however
browser-like the headers, parameters and signatures are.  ``curl_cffi``
reproduces Chrome's handshake (``IMPERSONATE``), so the client, its
User-Agent, its query parameters and its signatures all describe the same
Chrome version.  Without ``curl_cffi`` the client falls back to aiohttp.

Media transfers from the CDN do not go through here (``engine.fetcher``).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union

from common.log import get_logger

logger = get_logger(__name__)

# One Chrome version everywhere: TLS handshake, User-Agent, query parameters.
CHROME_MAJOR = 146
CHROME_VERSION = f"{CHROME_MAJOR}.0.0.0"
IMPERSONATE = f"chrome{CHROME_MAJOR}"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"Chrome/{CHROME_VERSION} Safari/537.36")
COOKIE_DOMAIN = ".douyin.com"
# Chrome's client hints for this version, exactly as Chrome sends them.
SEC_CH_UA = (f'"Chromium";v="{CHROME_MAJOR}", "Not-A.Brand";v="24", '
             f'"Google Chrome";v="{CHROME_MAJOR}"')

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
except Exception:  # pragma: no cover - optional dependency
    _CurlSession = None

# On Windows' Proactor loop curl_cffi adds a selector thread by itself and
# says so on every session; the fallback works, the notice is noise.
warnings.filterwarnings("ignore", message=r"(?s).*Proactor event loop does not implement")


@dataclass
class Response:
    status: int
    body: bytes
    url: str
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        return str(self.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _body(data: Union[Mapping[str, Any], bytes, None]) -> Union[Dict[str, Any], bytes, None]:
    """A form as a dict, raw bytes (e.g. JSON) as they are."""
    if not data:
        return None
    return data if isinstance(data, bytes) else dict(data)


class Transport:
    """One HTTP session with a cookie jar."""

    name = "base"

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      data: Union[Mapping[str, Any], bytes, None] = None,
                      proxy: Optional[str] = None,
                      timeout: float = 30.0, allow_redirects: bool = True) -> Response:
        raise NotImplementedError

    def cookie(self, name: str) -> str:
        raise NotImplementedError

    def set_cookies(self, cookies: Mapping[str, str]) -> None:
        raise NotImplementedError

    def browser_cookies(self) -> List[Dict[str, Any]]:
        """Scoped guest cookies for handing a direct QR request to the official SDK."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class CurlTransport(Transport):
    """Chrome's TLS/HTTP2 fingerprint through curl-impersonate."""

    name = "curl_cffi"

    def __init__(self, cookies: Mapping[str, str]):
        # Only the handshake is impersonated.  curl_cffi's default headers are
        # those of a *macOS* Chrome *opening a page* (sec-ch-ua-platform
        # "macOS", Sec-Fetch-Mode navigate, Upgrade-Insecure-Requests): for a
        # Windows UA calling an API from a page they contradict themselves, so
        # the caller sends every header (``DouyinClient.api_headers``).
        self._session = _CurlSession(impersonate=IMPERSONATE, default_headers=False)
        self.set_cookies(cookies)

    async def request(self, method, url, *, headers, data=None, proxy=None, timeout=30.0,
                      allow_redirects=True) -> Response:
        response = await self._session.request(
            method.upper(), url, headers=dict(headers), data=_body(data),
            proxy=proxy or None, timeout=timeout, allow_redirects=allow_redirects)
        return Response(int(response.status_code), bytes(response.content or b""),
                        str(response.url), {k.lower(): v for k, v in response.headers.items()})

    def cookie(self, name: str) -> str:
        try:
            return str(self._session.cookies.get(name) or "")
        except Exception:   # the same name on several domains
            for item in self._session.cookies.jar:
                if item.name == name and item.value:
                    return str(item.value)
            return ""

    def set_cookies(self, cookies: Mapping[str, str]) -> None:
        for name, value in cookies.items():
            if name:
                self._session.cookies.set(str(name), str(value), domain=COOKIE_DOMAIN)

    def browser_cookies(self) -> List[Dict[str, Any]]:
        return [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                 "path": cookie.path or "/", "secure": bool(cookie.secure),
                 "httpOnly": "HttpOnly" in getattr(cookie, "_rest", {})}
                for cookie in self._session.cookies.jar if _douyin_domain(cookie.domain)]

    async def close(self) -> None:
        await self._session.close()


class AiohttpTransport(Transport):
    """Fallback when curl_cffi is missing (a plainly non-browser handshake)."""

    name = "aiohttp"

    def __init__(self, cookies: Mapping[str, str]):
        import aiohttp
        import yarl

        self._aiohttp = aiohttp
        self._yarl = yarl
        self._session = aiohttp.ClientSession(cookies=dict(cookies), raise_for_status=False)

    async def request(self, method, url, *, headers, data=None, proxy=None, timeout=30.0,
                      allow_redirects=True) -> Response:
        options: Dict[str, Any] = {
            "headers": {**headers, "Accept-Encoding": "gzip, deflate"}, "proxy": proxy,
            "allow_redirects": allow_redirects,
            "timeout": self._aiohttp.ClientTimeout(total=timeout)}
        if data is not None:
            options["data"] = _body(data)
        # encoded=True: yarl would otherwise decode %2F in signed parameters.
        exact = url.isascii() and " " not in url
        target = self._yarl.URL(url, encoded=True) if exact else url
        async with self._session.request(method.upper(), target, **options) as response:
            body = await response.read()
            return Response(response.status, body, str(response.url),
                            {k.lower(): v for k, v in response.headers.items()})

    def cookie(self, name: str) -> str:
        for morsel in self._session.cookie_jar:
            if morsel.key == name and str(morsel.value or "").strip():
                return str(morsel.value).strip()
        return ""

    def set_cookies(self, cookies: Mapping[str, str]) -> None:
        self._session.cookie_jar.update_cookies(dict(cookies))

    def browser_cookies(self) -> List[Dict[str, Any]]:
        return [{"name": cookie.key, "value": cookie.value, "domain": cookie["domain"],
                 "path": cookie["path"] or "/", "secure": bool(cookie["secure"]),
                 "httpOnly": bool(cookie["httponly"])}
                for cookie in self._session.cookie_jar if _douyin_domain(cookie["domain"])]

    async def close(self) -> None:
        await self._session.close()


def _douyin_domain(domain: str) -> bool:
    host = str(domain or "").lstrip(".").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def create(cookies: Mapping[str, str]) -> Transport:
    if _CurlSession is not None:
        return CurlTransport(cookies)
    logger.warning("curl_cffi is not installed: Douyin requests use aiohttp's non-browser "
                   "TLS fingerprint and are more likely to be refused")
    return AiohttpTransport(cookies)
