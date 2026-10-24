"""The Douyin web API client.

One client instance serves the whole application: it owns the HTTP
session (``douyin.transport``: Chrome's TLS fingerprint through curl_cffi),
the cookie jar, the request signatures and the :class:`RiskGate`.

Everything a request reveals about the caller describes one browser: the
TLS handshake, the User-Agent and the query parameters name the same
Chrome version, and the a_bogus signature always carries the same screen
fingerprint (a real browser does not change its screen between requests).
Every list endpoint returns a :class:`~douyin.pages.Page`; callers decide
whether a suspicious page is fatal via ``Page.ensure_valid``.

When Douyin rejects the pure-Python web signature (``Sign Invalid``,
Argus) or answers with an empty body, a request can be replayed through the
logged-in browser page (``page_channel``), which owns the site's own
signature SDK.  The client never opens a browser itself; the application
plugs the channel in.

Several accounts can be logged in at once.  Each keeps its own session
(cookie jar, msToken); signing, pacing and the risk gate are shared.  A
task picks the account for all its requests once (:func:`use_account`);
without it requests go out as the *main* account.  Only the main account
has the browser channel.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlencode, urlsplit

from common.log import get_logger, safe_url
from douyin import cookies as cookie_tools
from douyin.errors import (
    BadResponse,
    DouyinError,
    EmptyResponse,
    HttpError,
    LoginRequired,
    NetworkError,
    PaginationError,
    is_risk,
)
from douyin.pages import CursorGuard, Page, first_url, to_int
from douyin.risk import RiskGate, RiskListener
from douyin.signing import websign
from douyin.signing.ms_token import MsTokenManager
from douyin.signing.xbogus import XBogus
from douyin.transport import CHROME_VERSION, SEC_CH_UA, USER_AGENT, Transport
from douyin.transport import create as create_transport

try:  # a_bogus needs gmssl; X-Bogus remains available without it
    from douyin.signing.abogus import ABogus
except Exception:  # pragma: no cover - optional dependency
    ABogus = None

logger = get_logger(__name__)

BASE_URL = "https://www.douyin.com"
# The screen the query parameters and the a_bogus fingerprint describe: a
# maximized Chrome on a 1920x1080 Windows screen (taskbar 40px).
SCREEN = (1920, 1080)
BROWSER_FP = "1920|937|1920|1040|0|0|0|0|1920|1080|1920|1040|1920|937|24|24|Win32"
# Bodies of a 200 that are the anti-bot wall, not the API.
_AC_MARKERS = ("__ac_nonce", "__ac_signature", "byted_acrawler", "_$jsvmprt")
_VERIFY_MARKERS = ("验证码", "captcha", "verifycenter", "verify_ticket")
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
POST_PATH = "/aweme/v1/web/aweme/post/"
# A valid post page can be empty while ``has_more`` and an older cursor say
# the list goes on: a gap in the timeline, each empty page stepping back a
# week or two (one creator had 8 in a row).  This many such pages are skipped.
MAX_EMPTY_POST_PAGES = 30
FOLLOW_FEED_PATH = "/aweme/v1/web/follow/feed/"
COMMENT_PATH = "/aweme/v1/web/comment/list/"
COMMENT_REPLY_PATH = "/aweme/v1/web/comment/list/reply/"
EMOJI_PATH = "/aweme/v1/web/emoji/list"
# Paths that answer an a_bogus-signed request with an empty 200 but accept
# X-Bogus over the same query (reply pages, checked live 2026-10).
_XBOGUS_PATHS = {COMMENT_REPLY_PATH}
_MS_TOKEN_REFRESH_SECONDS = 15 * 60
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_RENEW_SECONDS = 6 * 3600     # at most one device-id renewal per session this often
_RETRY_DELAYS = (1, 2, 5)
_DETAIL_AIDS = ("6383", "1128")
_LOGIN_STATUS_CODES = {2483}
_INACCESSIBLE_FILTERS = {
    "status_deleted": "作品已删除或不再可见",
    "status_self_see": "作品权限限制或已删除",
    "status_friend_see": "作品受隐私设置限制",
    "author_secret": "作品受作者隐私设置限制",
    "story_25_filter": "作品属于限时内容",
}

PageChannel = Callable[..., Awaitable[Optional[Dict[str, Any]]]]


def default_query(ms_token: str) -> Dict[str, Any]:
    """The webapp fingerprint Douyin expects on every signed request."""
    return {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "update_version_code": "170400", "pc_client_type": "1", "pc_libra_divert": "Windows",
        "version_code": "290100", "version_name": "29.1.0", "cookie_enabled": "true",
        "screen_width": str(SCREEN[0]), "screen_height": str(SCREEN[1]),
        "browser_language": "zh-CN", "browser_platform": "Win32", "browser_name": "Chrome",
        "browser_version": CHROME_VERSION, "browser_online": "true", "engine_name": "Blink",
        "engine_version": CHROME_VERSION,
        "os_name": "Windows", "os_version": "10", "cpu_core_num": "16", "device_memory": "8",
        "platform": "PC", "downlink": "10", "effective_type": "4g", "round_trip_time": "200",
        "support_h265": "1", "support_dash": "1", "msToken": str(ms_token or ""),
    }


@asynccontextmanager
async def _no_slot():
    yield


def _login_required(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    message = str(data.get("status_msg") or "")
    return (to_int(data.get("status_code")) in _LOGIN_STATUS_CODES
            or "请先登录" in message or "用户未登录" in message)


def _classify_body(body: bytes) -> str:
    """What a non-JSON 200 is: the anti-bot wall, a verification page, or other."""
    text = body[:4096].decode("utf-8", "replace")
    lowered = text.lower()
    if any(marker in text for marker in _AC_MARKERS):
        return "ac-challenge"
    if any(marker in lowered for marker in _VERIFY_MARKERS):
        return "verify-page"
    return "non-json-200"


def _describe_body(response: Any, body: bytes) -> str:
    kind = getattr(response, "content_type", "") or "unknown"
    return f"content-type={kind}, {len(body)} bytes"


def _referer(path: str, params: Dict[str, Any]) -> str:
    """The page a browser would be on when it makes this request."""
    normalised = _normalise_path(path)
    sec_uid = str(params.get("sec_user_id") or "")
    # Comment lists name the work aweme_id, their reply pages item_id.
    aweme_id = str(params.get("aweme_id") or params.get("item_id") or "")
    if normalised == DETAIL_PATH or (aweme_id and "/comment/" in normalised):
        return f"{BASE_URL}/video/{aweme_id}" if aweme_id else f"{BASE_URL}/"
    if normalised == FOLLOW_FEED_PATH:
        return f"{BASE_URL}/follow"
    if sec_uid:
        return f"{BASE_URL}/user/{sec_uid}"
    return f"{BASE_URL}/"


def _navigation_headers() -> Dict[str, str]:
    """Chrome opening a link (a short URL is followed like a navigation)."""
    return {
        "sec-ch-ua": SEC_CH_UA, "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1", "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,image/apng,*/*;q=0.8",
        "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document", "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9", "Priority": "u=0, i",
    }


def _normalise_path(path: str) -> str:
    path = "/" + str(path or "").lstrip("/")
    return path if path.endswith("/") else path + "/"


_account: contextvars.ContextVar[str] = contextvars.ContextVar("douyin_account", default="")


def use_account(sec_uid: str) -> None:
    """Every request made by the current task from now on logs in as ``sec_uid``.

    ``""`` (the default) is the main account.
    """
    _account.set(str(sec_uid or ""))


def current_account() -> str:
    return _account.get()


class _Session:
    """One account's login: its cookies, cookie jar and msToken."""

    def __init__(self, cookies: Optional[Dict[str, str]] = None):
        self.source = cookie_tools.sanitize(cookies or {})
        self.cookies = dict(self.source)
        self.transport: Optional[Transport] = None
        self.ms_token = (self.cookies.get("msToken") or "").strip()
        self.ms_token_generated = False
        self.ms_token_checked = time.monotonic()
        self.ttwid_renewed: Optional[float] = None

    async def close(self) -> None:
        if self.transport is not None:
            transport, self.transport = self.transport, None
            await transport.close()


class DouyinClient:
    """Signed, paced access to Douyin's web endpoints."""

    BASE_URL = BASE_URL

    def __init__(
        self,
        cookies: Optional[Dict[str, str]] = None,
        *,
        proxy: str = "",
        rate: float = 2.0,
        risk_listener: Optional[RiskListener] = None,
    ):
        self._main = _Session(cookies)
        self._others: Dict[str, _Session] = {}
        self.main_sec_uid = ""
        # Called with an account's sec_uid when Douyin says that (not the
        # main) account's login has expired.
        self.on_login_lost: Optional[Callable[[str], None]] = None
        # Called with (account sec_uid, "" for the main one; cookies) after
        # renew_device_id() changed that account's cookies, to save them.
        self.on_cookies_renewed: Optional[Callable[[str, Dict[str, str]], None]] = None
        self.proxy = str(proxy or "").strip() or None
        self.user_agent = USER_AGENT
        self.gate = RiskGate(rate, risk_listener)
        self.page_channel: Optional[PageChannel] = None
        # engine.scheduler.Scheduler; every API request holds one of its slots.
        self.scheduler: Any = None
        self._xbogus = XBogus(self.user_agent)
        self._ms_tokens = MsTokenManager(user_agent=self.user_agent)
        self._abogus = ABogus is not None

    # ── accounts ────────────────────────────────────────────────────

    def _account_key(self) -> str:
        key = _account.get()
        return "" if key == self.main_sec_uid else key

    def _current(self) -> _Session:
        key = self._account_key()
        if not key:
            return self._main
        session = self._others.get(key)
        if session is None:
            raise LoginRequired("该账号未登录或已从账号库移除")
        return session

    def on_main(self) -> bool:
        """Whether the running task's requests go out as the main account."""
        return not self._account_key()

    async def sync_accounts(self, main_sec_uid: str, main_cookies: Dict[str, str],
                            others: Dict[str, Dict[str, str]]) -> None:
        """Adopt the account library; unchanged logins keep their sessions."""
        main_cookies = cookie_tools.sanitize(main_cookies or {})
        self.main_sec_uid = str(main_sec_uid or "")
        if main_cookies != self._main.source:
            await self.set_cookies(main_cookies)
        wanted = {str(key): cookie_tools.sanitize(value) for key, value in others.items()
                  if key and key != self.main_sec_uid}
        for key in list(self._others):
            if key not in wanted or wanted[key] != self._others[key].source:
                await self._others.pop(key).close()
        for key, cookies in wanted.items():
            if key not in self._others:
                self._others[key] = _Session(cookies)

    async def profile_with(self, cookies: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """The profile of whoever ``cookies`` log in (a login not in the library yet)."""
        key = f"probe:{id(cookies)}:{time.monotonic()}"
        self._others[key] = _Session(cookies)
        token = _account.set(key)
        try:
            return await self.self_profile()
        finally:
            _account.reset(token)
            await self._others.pop(key).close()

    # The running task's account session, attribute by attribute.

    @property
    def cookies(self) -> Dict[str, str]:
        return self._current().cookies

    @cookies.setter
    def cookies(self, value: Dict[str, str]) -> None:
        self._current().cookies = value

    @property
    def _transport(self) -> Optional[Transport]:
        return self._current().transport

    @_transport.setter
    def _transport(self, value: Optional[Transport]) -> None:
        self._current().transport = value

    @property
    def _ms_token(self) -> str:
        return self._current().ms_token

    @_ms_token.setter
    def _ms_token(self, value: str) -> None:
        self._current().ms_token = value

    @property
    def _ms_token_generated(self) -> bool:
        return self._current().ms_token_generated

    @_ms_token_generated.setter
    def _ms_token_generated(self, value: bool) -> None:
        self._current().ms_token_generated = value

    @property
    def _ms_token_checked(self) -> float:
        return self._current().ms_token_checked

    @_ms_token_checked.setter
    def _ms_token_checked(self, value: float) -> None:
        self._current().ms_token_checked = value

    # ── session & cookies ───────────────────────────────────────────

    @property
    def headers(self) -> Dict[str, str]:
        return self.api_headers()

    def api_headers(self, referer: str = "", *, post: bool = False) -> Dict[str, str]:
        """What Chrome sends with a fetch() made by a douyin.com page (in Chrome's order)."""
        headers = {
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": self.user_agent,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "Accept": "application/json, text/plain, */*",
        }
        if post:
            headers["Origin"] = BASE_URL
        headers.update({
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": referer or f"{BASE_URL}/",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Priority": "u=1, i",
        })
        return headers

    def media_headers(self, user_agent: str = "") -> Dict[str, str]:
        """Headers for CDN media requests."""
        return {
            "User-Agent": user_agent or self.user_agent,
            "Referer": f"{BASE_URL}/",
            "Origin": BASE_URL,
            "Accept": "*/*",
        }

    async def session(self) -> Transport:
        if self._transport is None:
            self._transport = create_transport(self.cookies)
        return self._transport

    async def set_cookies(self, cookies: Dict[str, str]) -> None:
        """Replace the main account's login without recreating the client."""
        previous, self._main = self._main, _Session(cookies)
        await previous.close()

    async def renew_device_id(self) -> bool:
        """Give the running task's account a fresh ``ttwid`` (device id).

        Douyin buckets web clients by ``ttwid``; since 2026-09-30 one bucket
        gets clean gallery pictures scaled down (``tplv-dy-aweme-images-v2``,
        e.g. 1440x1920) and full size only watermarked, while fresh ids get
        the full-size clean ones.  Renews at most every TTWID_RENEW_SECONDS
        per account; returns whether the id changed.
        """
        session = self._current()
        now = time.monotonic()
        if session.ttwid_renewed is not None                 and now - session.ttwid_renewed < TTWID_RENEW_SECONDS:
            return False
        session.ttwid_renewed = now
        body = json.dumps({"region": "cn", "aid": 1768, "needFid": False,
                           "service": "www.ixigua.com",
                           "migrate_info": {"ticket": "", "source": "node"},
                           "cbUrlProtocol": "https", "union": True}).encode()
        probe = create_transport({})
        try:
            response = await probe.request(
                "POST", TTWID_REGISTER_URL, data=body, proxy=self.proxy, timeout=15.0,
                headers={"Content-Type": "application/json", "User-Agent": self.user_agent})
            ttwid = probe.cookie("ttwid")
        except Exception as exc:
            logger.info("ttwid registration failed: %s", exc)
            return False
        finally:
            await probe.close()
        if response.status != 200 or not ttwid or ttwid == session.cookies.get("ttwid"):
            logger.info("ttwid registration gave no new id (HTTP %s)", response.status)
            return False
        session.cookies = {**session.cookies, "ttwid": ttwid}
        session.source = cookie_tools.sanitize({**session.source, "ttwid": ttwid})
        if session.transport is not None:
            session.transport.set_cookies({"ttwid": ttwid})
        logger.info("Renewed the ttwid device id")
        if self.on_cookies_renewed:
            self.on_cookies_renewed(self._account_key(), dict(session.source))
        return True

    def set_rate(self, rate: float) -> None:
        self.gate.set_rate(rate)

    async def close(self) -> None:
        for session in (self._main, *self._others.values()):
            await session.close()

    # ── signing ─────────────────────────────────────────────────────

    async def _current_ms_token(self) -> str:
        if self._ms_token:
            if time.monotonic() - self._ms_token_checked < _MS_TOKEN_REFRESH_SECONDS:
                return self._ms_token
            self._ms_token_checked = time.monotonic()
            rotated = self._jar_ms_token()
            if rotated and rotated != self._ms_token:
                self._ms_token, self._ms_token_generated = rotated, False
                self.cookies["msToken"] = rotated
                return rotated
            if not self._ms_token_generated:
                return self._ms_token
            self.cookies.pop("msToken", None)
        token = (await asyncio.to_thread(self._ms_tokens.ensure_ms_token, self.cookies)).strip()
        self._ms_token, self._ms_token_generated = token, bool(token)
        self._ms_token_checked = time.monotonic()
        if token:
            self.cookies["msToken"] = token
            if self._transport is not None:
                self._transport.set_cookies({"msToken": token})
        return token

    def _jar_ms_token(self) -> str:
        if self._transport is None:
            return ""
        try:
            return self._transport.cookie("msToken").strip()
        except Exception:
            return ""

    async def query(self, **extra: Any) -> Dict[str, Any]:
        params = default_query(await self._current_ms_token())
        params.update(extra)
        return params

    def sign_url(self, url: str) -> Tuple[str, str]:
        """Append X-Bogus to a full URL; returns ``(url, user_agent)``."""
        signed, _value, ua = self._xbogus.build(url)
        return signed, ua

    def signed_url(self, path: str, params: Dict[str, Any], *, body: Optional[Dict] = None,
                   force_xbogus: bool = False) -> Tuple[str, str]:
        """A fully signed request URL for ``path`` (a_bogus/X-Bogus + web signature)."""
        normalised = _normalise_path(path)
        if normalised in websign.DOUYIN_SIGNED_PATHS:
            query = websign.encode_pairs((str(k), str(v)) for k, v in params.items())
        else:
            query = urlencode(params)
        endpoint = f"{BASE_URL}{path}"
        url, ua = "", ""
        # The webSign signature is computed over the a_bogus-signed query, so
        # every path (the detail endpoint included) is signed with a_bogus first.
        if not force_xbogus and self._abogus:
            try:
                signer = ABogus(fp=BROWSER_FP, user_agent=self.user_agent)
                signed_query, _ab, ua, _body = signer.generate_abogus(query, urlencode(body or {}))
                url = f"{endpoint}?{signed_query}"
            except Exception as exc:
                logger.warning("a_bogus failed, using X-Bogus: %s", exc)
        if not url:
            url, ua = self.sign_url(f"{endpoint}?{query}")
        return self._web_signature(normalised, url), ua

    def _web_signature(self, normalised_path: str, url: str) -> str:
        if normalised_path not in websign.DOUYIN_SIGNED_PATHS:
            return url
        uifid = websign.pick_uifid(self.cookies)
        if not uifid:
            return url
        parts = urlsplit(url)
        pairs, seen_uifid = [], False
        for raw in parts.query.split("&"):
            if not raw:
                continue
            name, sep, value = raw.partition("=")
            name, value = unquote(name), unquote(value if sep else "")
            if name == websign.UIFID_PARAM:
                if seen_uifid:
                    continue
                value, seen_uifid = uifid, True
            pairs.append((name, value))
        verify_fp = str(self.cookies.get(websign.VERIFY_FP_COOKIE) or "").strip()
        if verify_fp:
            pairs.extend((name, verify_fp) for name in websign.VERIFY_FP_PARAMS)
        signed_query, _signature, _headers = websign.sign(pairs, uifid)
        return parts._replace(query=signed_query).geturl()

    def _web_signature_headers(self, url: str) -> Dict[str, str]:
        values = {}
        for raw in urlsplit(url).query.split("&"):
            name, sep, value = raw.partition("=")
            if sep:
                values[unquote(name)] = unquote(value)
        signature = values.get(websign.SIGNATURE_PARAM)
        uifid = websign.pick_uifid(self.cookies)
        stamp = values.get(websign.TIMESTAMP_PARAM, "")
        if not signature or not uifid or not stamp:
            return {}
        return {websign.UIFID_PARAM: uifid, websign.SIGNATURE_PARAM: signature,
                websign.EXPIRE_HEADER: stamp}

    # ── transport ───────────────────────────────────────────────────

    async def request(
        self,
        path: str,
        params: Dict[str, Any],
        *,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        attempts: int = 3,
        page_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Signed JSON request with typed failures, retries and risk backoff.

        A scheduler slot is held only while a request is on the wire: waiting
        out a path's cooldown or a retry delay must not block other paths.
        """
        return await self._request_with_fallback(path, params, method=method, data=data,
                                                 headers=headers, attempts=attempts,
                                                 page_fallback=page_fallback)

    def slot(self):
        """The scheduler slot for a request made outside :meth:`request`."""
        if self.scheduler is None:
            return _no_slot()
        return self.scheduler.slot()

    async def _request_with_fallback(self, path, params, *, method, data, headers, attempts,
                                     page_fallback) -> Dict[str, Any]:
        try:
            return await self._request(path, params, method=method, data=data,
                                       headers=headers, attempts=attempts)
        except (HttpError, EmptyResponse) as exc:
            usable = isinstance(exc, EmptyResponse) or exc.signature_rejected
            if not (page_fallback and usable and self.page_channel is not None
                    and self.on_main()):
                raise
            logger.info("Retrying %s through the logged-in page: %s", path, exc)
            try:
                async with self.slot():
                    result = await self.page_channel(path, params, method=method, data=data)
            except Exception as channel_exc:
                logger.warning("Page channel failed for %s: %s", path, channel_exc)
                raise exc
            if not isinstance(result, dict) or not result:
                raise exc
            if _login_required(result):
                raise LoginRequired(path=path)
            return result

    async def _request(self, path, params, *, method, data, headers, attempts) -> Dict[str, Any]:
        session = await self.session()
        method = method.upper()
        attempts = max(1, int(attempts))
        force_xbogus = _normalise_path(path) in _XBOGUS_PATHS
        last_error: Optional[DouyinError] = None
        attempt = 0
        while attempt < attempts:
            wait = self.gate.reserve(path)
            if wait > 0:
                await asyncio.sleep(wait)
            url, ua = self.signed_url(path, params, body=data if method == "POST" else None,
                                      force_xbogus=force_xbogus)
            request_headers = {**self.api_headers(_referer(path, params),
                                                  post=method == "POST"),
                               **(headers or {}), **self._web_signature_headers(url),
                               "User-Agent": ua}
            try:
                async with self.slot():
                    response = await session.request(
                        method, url, headers=request_headers,
                        data=(data or {}) if method == "POST" else None, proxy=self.proxy)
                body, status = response.body, response.status
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = NetworkError(f"{type(exc).__name__}: {exc}", path=path)
            else:
                if status == 200:
                    if not body:
                        self.gate.note_risk(path, "empty-200",
                                            _describe_body(response, body))
                        last_error = EmptyResponse(f"empty 200 at {path}", path=path)
                    else:
                        try:
                            result = json.loads(body)
                        except ValueError:
                            reason = _classify_body(body)
                            self.gate.note_risk(path, reason, _describe_body(response, body))
                            last_error = (EmptyResponse if reason == "ac-challenge"
                                          else BadResponse)(f"{reason} at {path}", path=path)
                        else:
                            return self._accept(path, result if isinstance(result, dict) else {})
                else:
                    text = body.decode("utf-8", "replace")[:4096]
                    error = HttpError(status, path, body=text)
                    if status == 429:
                        self.gate.note_risk(path, "http-429")
                    elif status == 403:
                        self.gate.note_risk(path, "signature-rejected"
                                            if error.signature_rejected else "http-403")
                    logger.warning("Douyin HTTP %s at %s: %r", status, path, text[:300])
                    last_error = error
                    if status == 403 and not force_xbogus and "sign invalid" in text.lower():
                        # Some deployments refuse the bundled a_bogus but accept
                        # X-Bogus over the same query: one extra attempt.
                        force_xbogus = True
                        if attempt + 1 >= attempts:
                            attempts += 1
                        attempt += 1
                        continue
                    if not error.retryable:
                        raise error
            attempt += 1
            if attempt < attempts:
                await asyncio.sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
        assert last_error is not None
        raise last_error

    def _accept(self, path: str, result: Dict[str, Any]) -> Dict[str, Any]:
        if _login_required(result):
            # Only the main account's expired login pauses background work;
            # another account just stops being used until it logs in again.
            account = self._account_key()
            self.gate.report("login_required", "diagnostic", path, http_status=200,
                             api_status=to_int(result.get("status_code")) or None,
                             reason=str(result.get("status_msg") or "Login required"),
                             notify=not account)
            if account and not account.startswith("probe:") and self.on_login_lost:
                self.on_login_lost(account)
            raise LoginRequired(path=path, status=to_int(result.get("status_code")))
        if result.get("verify_ticket"):
            self.gate.note_risk(path, "verify-page")
            return result
        self.gate.note_ok(path)
        code = to_int(result.get("status_code"))
        if code:
            self.gate.report(f"api_status_{code}", "diagnostic", path, http_status=200,
                             api_status=code,
                             reason=str(result.get("status_msg") or "Unclassified API status"))
        return result

    def detail_cooldown(self) -> float:
        return self.gate.cooldown_remaining(DETAIL_PATH)

    async def resolve_short_url(self, url: str, *, timeout: float = 10.0) -> Optional[str]:
        """Follow a short link's redirects; ``None`` on failure."""
        try:
            session = await self.session()
            response = await session.request("GET", url, headers=_navigation_headers(),
                                             proxy=self.proxy, timeout=timeout)
            if response.status >= 400:
                logger.warning("Short URL %s -> HTTP %s", safe_url(url), response.status)
                return None
            return response.url
        except Exception as exc:
            logger.warning("Short URL %s failed: %s", safe_url(url), exc)
            return None

    # ── works ───────────────────────────────────────────────────────

    async def aweme_detail(self, aweme_id: str, *, gentle: bool = False
                           ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """``(detail, reason, kind)``; kind is ``""``, ``inaccessible`` or ``empty``.

        ``inaccessible`` is Douyin's explicit verdict (deleted, private, …);
        ``empty`` is an unexplained null that may be transient.

        ``gentle`` (background downloads): one attempt per request, no page
        fallback, and a risk answer is not retried with the other ``aid`` —
        a rejected work costs one request instead of up to eight.
        """
        for index, aid in enumerate(_DETAIL_AIDS):
            params = await self.query(aweme_id=aweme_id, aid=aid)
            try:
                data = await (self.request(DETAIL_PATH, params, attempts=1, page_fallback=False)
                              if gentle else self.request(DETAIL_PATH, params))
            except LoginRequired:
                raise
            except DouyinError as exc:
                if index < len(_DETAIL_AIDS) - 1 and not (gentle and is_risk(exc)):
                    continue
                raise
            detail = data.get("aweme_detail")
            if isinstance(detail, dict) and str(detail.get("aweme_id") or "") == str(aweme_id):
                return detail, "", ""
            info = data.get("filter_detail")
            if isinstance(info, dict) and info.get("filter_reason"):
                code = str(info.get("filter_reason") or "").strip()
                message = str(info.get("detail_msg") or info.get("notice") or "").strip()
                lowered = f"{code} {message}".lower()
                if code in _INACCESSIBLE_FILTERS:
                    return None, message or _INACCESSIBLE_FILTERS[code], "inaccessible"
                if any(token in lowered for token in
                       ("deleted", "self_see", "friend_see", "secret", "private")):
                    return None, message or f"作品不可访问（{code}）", "inaccessible"
        # status_code 0 without a detail: the request "worked" but carried
        # nothing (a signature/parameter problem rather than rate limiting).
        self.gate.report("detail_empty", "diagnostic", DETAIL_PATH, http_status=200,
                         reason="status_code=0 but no aweme_detail")
        return None, "作品详情为空", "empty"

    # ── users ───────────────────────────────────────────────────────

    async def user_profile(self, sec_uid: str) -> Optional[Dict[str, Any]]:
        data = await self.request("/aweme/v1/web/user/profile/other/",
                                  await self.query(sec_user_id=sec_uid))
        user = data.get("user")
        return user if isinstance(user, dict) and user else None

    async def self_profile(self) -> Optional[Dict[str, Any]]:
        data = await self.request("/aweme/v1/web/user/profile/self/", await self.query(),
                                  page_fallback=False)
        user = data.get("user")
        return user if isinstance(user, dict) and user else None

    async def _user_page(self, path: str, sec_uid: str, cursor: int, count: int,
                         **extra: Any) -> Dict[str, Any]:
        params = await self.query(sec_user_id=sec_uid, max_cursor=int(cursor or 0),
                                  count=int(count), locate_query="false", **extra)
        return await self.request(path, params)

    async def user_posts(self, sec_uid: str, cursor: int = 0, count: int = 18) -> Page:
        """One page of a user's posts, stepping over empty pages that go on.

        Only a trustworthy empty page (status 0, list present, no login or
        verify prompt) whose cursor moves back in time is skipped; after
        :data:`MAX_EMPTY_POST_PAGES` the empty page is returned for
        :meth:`Page.ensure_valid` to refuse.
        """
        guard = CursorGuard(cursor)
        skipped = 0
        while True:
            raw = await self._user_page(
                POST_PATH, sec_uid, cursor, count, show_live_replay_strategy="1",
                need_time_list="1", time_list_query="0", whale_cut_token="", cut_version="1",
                publish_video_strategy_type="2", from_user_page="1",
            )
            page = Page.parse(raw, ("aweme_list",))
            if (page.items or not page.has_more or page.suspicious
                    or skipped >= MAX_EMPTY_POST_PAGES):
                return page
            if cursor and page.cursor >= cursor:
                raise PaginationError(f"作品列表空页的游标没有向前推进（{page.cursor}），未能读完")
            cursor = guard.advance(page.cursor, "作品列表")
            skipped += 1
            logger.info("Empty post page with has_more for %s; skipping to cursor %s (%d/%d)",
                        sec_uid[-12:], cursor, skipped, MAX_EMPTY_POST_PAGES)

    async def user_likes(self, sec_uid: str, cursor: int = 0, count: int = 20) -> Page:
        # The parameters the 喜欢 tab of a profile page sends.
        raw = await self._user_page("/aweme/v1/web/aweme/favorite/", sec_uid, cursor, count,
                                    min_cursor="0", whale_cut_token="", cut_version="1",
                                    publish_video_strategy_type="2")
        return Page.parse(raw, ("aweme_list",))

    async def user_mixes(self, sec_uid: str, cursor: int = 0, count: int = 20) -> Page:
        raw = await self._user_page("/aweme/v1/web/mix/list/", sec_uid, cursor, count)
        return Page.parse(raw, ("mix_infos", "mix_list"))

    async def user_music(self, sec_uid: str, cursor: int = 0, count: int = 20) -> Page:
        raw = await self._user_page("/aweme/v1/web/music/list/", sec_uid, cursor, count)
        return Page.parse(raw, ("music_list",))

    async def self_collection(self, cursor: int = 0, count: int = 20) -> Page:
        """The logged-in account's default 收藏 feed."""
        params = await self.query(publish_video_strategy_type="2", version_code="170400",
                                  version_name="17.4.0")
        raw = await self.request(
            "/aweme/v1/web/aweme/listcollection/", params, method="POST",
            data={"count": int(count), "cursor": int(cursor or 0)},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{BASE_URL}/user/self?showTab=favorite_collection"},
        )
        return Page.parse(raw, ("aweme_list",))

    async def self_collect_folders(self, cursor: int = 0, count: int = 10) -> Page:
        params = await self.query(cursor=int(cursor or 0), count=int(count),
                                  version_code="170400", version_name="17.4.0")
        raw = await self.request("/aweme/v1/web/collects/list/", params)
        return Page.parse(raw, ("collects_list",))

    async def collect_folder_works(self, collects_id: str, cursor: int = 0,
                                   count: int = 10) -> Page:
        params = await self.query(collects_id=collects_id, cursor=int(cursor or 0),
                                  count=int(count), version_code="170400",
                                  version_name="17.4.0")
        raw = await self.request("/aweme/v1/web/collects/video/list/", params)
        return Page.parse(raw, ("aweme_list",))

    async def self_collected_mixes(self, cursor: int = 0, count: int = 12) -> Page:
        params = await self.query(cursor=int(cursor or 0), count=int(count),
                                  version_code="170400", version_name="17.4.0")
        raw = await self.request("/aweme/v1/web/mix/listcollection/", params)
        return Page.parse(raw, ("mix_infos",))

    async def follow_feed(self, cursor: int = 0, count: int = 20) -> Page:
        """The logged-in account's 关注 feed: new works of everyone it follows.

        Entries arrive as ``data[].aweme``; the page's ``aweme_list`` holds the
        unwrapped works.  One request covers every followed creator.
        """
        params = await self.query(cursor=int(cursor or 0), level=1, count=int(count),
                                  pull_type="")
        raw = await self.request(FOLLOW_FEED_PATH, params)
        entries = raw.get("data") if isinstance(raw, dict) else None
        if isinstance(entries, list):
            raw = {**raw, "aweme_list": [entry["aweme"] for entry in entries
                                         if isinstance(entry, dict)
                                         and isinstance(entry.get("aweme"), dict)]}
        return Page.parse(raw, ("aweme_list",))

    async def following_page(self, sec_uid: str, max_time: int = 0, count: int = 20) -> Page:
        params = await self.query(
            user_id=sec_uid, sec_user_id=sec_uid, offset=0, count=min(20, max(1, count)),
            source_type="1", gps_access="0", address_book_access="0", min_change="0",
        )
        if max_time > 0:
            params["max_time"] = int(max_time)
        path = "/aweme/v1/web/user/following/list/"
        raw = await self.request(path, params)
        page = Page.parse(raw, ("followings", "follow_list", "user_list"), cursor_key="min_time")
        if page.suspicious and self.page_channel is not None and self.on_main():
            fallback = await self.page_channel(path, params)
            if isinstance(fallback, dict) and fallback:
                if _login_required(fallback):
                    raise LoginRequired(path=path)
                page = Page.parse(fallback, ("followings", "follow_list", "user_list"),
                                  source="browser", cursor_key="min_time")
        return page

    # ── collections & music ─────────────────────────────────────────

    async def mix_detail(self, mix_id: str) -> Optional[Dict[str, Any]]:
        data = await self.request("/aweme/v1/web/mix/detail/", await self.query(mix_id=mix_id))
        detail = data.get("mix_info") or data.get("mix_detail")
        return detail if isinstance(detail, dict) else None

    async def mix_works(self, mix_id: str, cursor: int = 0, count: int = 20) -> Page:
        raw = await self.request("/aweme/v1/web/mix/aweme/",
                                 await self.query(mix_id=mix_id, cursor=int(cursor or 0),
                                                  count=int(count)))
        return Page.parse(raw, ("aweme_list",))

    async def music_detail(self, music_id: str) -> Optional[Dict[str, Any]]:
        data = await self.request("/aweme/v1/web/music/detail/",
                                  await self.query(music_id=music_id))
        detail = data.get("music_info") or data.get("music_detail")
        return detail if isinstance(detail, dict) else None

    async def music_works(self, music_id: str, cursor: int = 0, count: int = 20) -> Page:
        raw = await self.request("/aweme/v1/web/music/aweme/",
                                 await self.query(music_id=music_id, cursor=int(cursor or 0),
                                                  count=int(count)))
        return Page.parse(raw, ("aweme_list",))

    # ── comments ────────────────────────────────────────────────────

    async def comments(self, aweme_id: str, cursor: int = 0, count: int = 20, *,
                       gentle: bool = False) -> Page:
        """One page of a work's comments.

        ``gentle`` (the background comment sync): one attempt, no page
        channel; the caller paces and backs off itself.
        """
        params = await self.query(aweme_id=aweme_id, cursor=int(cursor or 0), count=int(count),
                                  item_type="0", insert_ids="", whale_cut_token="",
                                  cut_version="1", rcFT="")
        raw = await self.request(COMMENT_PATH, params, attempts=1 if gentle else 3,
                                 page_fallback=not gentle)
        return Page.parse(raw, ("comments",))

    async def comment_replies(self, aweme_id: str, comment_id: str, cursor: int = 0,
                              count: int = 20, *, gentle: bool = False,
                              page_fallback: Optional[bool] = None) -> Page:
        """One page of a comment's replies.

        ``page_fallback`` overrides ``gentle``'s "no page channel": a person
        expanding replies may be answered through the logged-in page when
        the direct request comes back empty.
        """
        params = await self.query(item_id=aweme_id, comment_id=comment_id,
                                  cursor=int(cursor or 0), count=int(count), item_type="0")
        raw = await self.request(COMMENT_REPLY_PATH, params, attempts=1 if gentle else 3,
                                 page_fallback=(not gentle) if page_fallback is None
                                 else page_fallback)
        return Page.parse(raw, ("comments",))

    async def emoji_list(self) -> Dict[str, str]:
        """Comment emoji codes (``[微笑]``) → picture URL, as the web player shows them."""
        data = await self.request(EMOJI_PATH, await self.query(), attempts=1,
                                  page_fallback=False)
        found: Dict[str, str] = {}
        for item in data.get("emoji_list") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or "").strip()
            url = first_url(item.get("emoji_url"))
            if name and url:
                found[name] = url
        return found

    # ── media URLs ──────────────────────────────────────────────────

    def play_url(self, video_id: str, ratio: str) -> Tuple[str, Dict[str, str]]:
        """A signed ``/aweme/v1/play/`` URL for a video URI at ``ratio``."""
        url, ua = self.signed_url("/aweme/v1/play/", {
            "video_id": video_id, "ratio": ratio, "line": "0", "is_play_url": "1",
            "watermark": "0", "source": "PackSourceEnum_PUBLISH",
        })
        return url, self.media_headers(ua)

    def sign_play_candidate(self, url: str) -> Tuple[str, Dict[str, str]]:
        """Sign a douyin.com play URL that lacks X-Bogus."""
        if "X-Bogus=" in url:
            return url, self.media_headers()
        signed, ua = self.sign_url(url)
        return signed, self.media_headers(ua)

    async def probe_size(self, url: str, headers: Dict[str, str]) -> int:
        """Total bytes of a media URL (one-byte Range request); -1 when unknown."""
        try:
            session = await self.session()
            response = await session.request("GET", url, headers={**headers, "Range": "bytes=0-0"},
                                             proxy=self.proxy)
            if response.status not in (200, 206):
                return -1
            content_range = response.headers.get("content-range") or ""
            if "/" in content_range:
                return to_int(content_range.rsplit("/", 1)[1], -1)
            return to_int(response.headers.get("content-length"), -1)
        except Exception:
            return -1

    def browser_cookies(self) -> List[Dict[str, str]]:
        return [{"name": name, "value": value, "url": f"{BASE_URL}/"}
                for name, value in self.cookies.items() if name]
