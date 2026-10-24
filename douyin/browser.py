"""The logged-in browser page, used only when the HTTP API cannot be.

:class:`BrowserChannel` keeps one Playwright page (in a persistent,
app-owned profile) and serializes every use of it:

* ``request_api`` replays a protected API request from inside the page, where
  the site's own signature SDK is active;
* ``work_detail`` opens a work page and captures the detail response the
  player itself requested (fresh signed media URLs);
* ``screenshot`` saves a creator homepage.

:func:`qr_login` logs in by QR code, in a hidden page (the QR image is handed
back to the app) or in a visible browser window.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from common.log import get_logger
from douyin import cookies as cookie_tools
from douyin.client import BASE_URL, DETAIL_PATH
from douyin.pages import to_int
from douyin.qr import QrBootstrap

logger = get_logger(__name__)

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--no-sandbox"]
_REQUEST_TIMEOUT = 45.0
_NAVIGATION_TIMEOUT_MS = 30000
_IDLE_CLOSE_SECONDS = 300.0
# After the page itself gets an empty/blocked answer, stop asking for a while:
# 30 s, doubling per consecutive failure, at most 10 min.
_BACKOFF_BASE = 30.0
_BACKOFF_MAX = 600.0


class BrowserUnavailable(RuntimeError):
    """Playwright (or a Chromium-family browser) is not installed."""


async def _launch(playwright, *, headless: bool, profile_dir: Optional[Path],
                  user_agent: str):
    """A context in the persistent profile, else a temporary one.

    Bundled Chromium first, then an installed Edge/Chrome (source checkouts
    often have Playwright without its downloaded browser).
    """
    # The same screen the API client's parameters and a_bogus fingerprint describe.
    options = {"user_agent": user_agent, "locale": "zh-CN",
               "viewport": {"width": 1600, "height": 900},
               "screen": {"width": 1920, "height": 1080}}
    errors = []
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        for channel in (None, "msedge", "chrome"):
            try:
                extra = {"channel": channel} if channel else {}
                context = await playwright.chromium.launch_persistent_context(
                    str(profile_dir), headless=headless, args=_LAUNCH_ARGS, **options, **extra)
                return None, context
            except Exception as exc:
                errors.append(exc)
    for channel in (None, "msedge", "chrome"):
        try:
            extra = {"channel": channel} if channel else {}
            browser = await playwright.chromium.launch(headless=headless, args=_LAUNCH_ARGS,
                                                       **extra)
            return browser, await browser.new_context(**options)
        except Exception as exc:
            errors.append(exc)
    raise BrowserUnavailable(f"无法启动浏览器：{errors[-1] if errors else '未知错误'}")


async def _match_client_hints(context, page, user_agent: str) -> None:
    """Make sec-ch-ua / navigator.userAgentData name the User-Agent's version.

    The context's ``user_agent`` only replaces the UA string; the client hints
    would still report the engine's real version (or "HeadlessChrome"), and a
    UA that disagrees with its own client hints is an obvious automation sign.
    """
    match = re.search(r"Chrome/(\d+)\.[\d.]+", user_agent or "")
    if not match:
        return
    major, full = match.group(1), match.group(0).split("/", 1)[1]
    # The same brand list (and GREASE entry) the API client's sec-ch-ua names.
    brands = [{"brand": "Chromium", "version": major},
              {"brand": "Not-A.Brand", "version": "24"},
              {"brand": "Google Chrome", "version": major}]
    metadata = {"brands": brands,
                "fullVersionList": [{**brand, "version": full if brand["version"] == major
                                     else "24.0.0.0"} for brand in brands],
                "fullVersion": full, "platform": "Windows", "platformVersion": "10.0.0",
                "architecture": "x86", "bitness": "64", "model": "", "mobile": False,
                "wow64": False}
    try:
        session = await context.new_cdp_session(page)
        await session.send("Emulation.setUserAgentOverride",
                           {"userAgent": user_agent, "acceptLanguage": "zh-CN,zh;q=0.9",
                            "platform": "Win32", "userAgentMetadata": metadata})
    except Exception as exc:   # not Chromium, or an older CDP
        logger.info("Client hints override failed: %s", exc)


class BrowserChannel:
    """One shared, serialized logged-in page (created lazily, closed when idle)."""

    def __init__(self, *, profile_dir: Optional[Path] = None, headless: bool = True,
                 user_agent: str = "", on_captcha=None):
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.headless = headless
        self.user_agent = user_agent
        self.on_captcha = on_captcha
        self.enabled = True
        self._cookies: Dict[str, str] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._origin_ready = False
        self._idle_task: Optional[asyncio.Task] = None
        self._failures = 0
        self._cooldown_until = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _failed(self, what: str) -> None:
        self._failures += 1
        delay = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** (self._failures - 1)))
        self._cooldown_until = time.monotonic() + delay
        logger.warning("Browser channel failed (%s); pausing it for %.0fs", what, delay)

    def _succeeded(self) -> None:
        self._failures = 0
        self._cooldown_until = 0.0

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        self._cookies = cookie_tools.sanitize(cookies)
        if self._context is not None:
            asyncio.ensure_future(self._push_cookies())

    async def _push_cookies(self) -> None:
        if self._context is None or not self._cookies:
            return
        try:
            await self._context.add_cookies([
                {"name": name, "value": value, "url": f"{BASE_URL}/"}
                for name, value in self._cookies.items() if name
            ])
        except Exception as exc:
            logger.debug("Could not push cookies to the browser: %s", exc)

    async def _ensure(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        await self._shutdown()
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise BrowserUnavailable("未安装 Playwright，浏览器兜底不可用") from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser, self._context = await _launch(
                self._playwright, headless=self.headless, profile_dir=self.profile_dir,
                user_agent=self.user_agent)
        except Exception:
            await self._shutdown()
            raise
        await self._push_cookies()
        pages = list(getattr(self._context, "pages", []) or [])
        self._page = pages[0] if pages else await self._context.new_page()
        for extra in pages[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        await _match_client_hints(self._context, self._page, self.user_agent)
        self._origin_ready = False
        return self._page

    async def _shutdown(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), 5)
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), 5)
            except Exception:
                pass
        self._playwright = self._browser = self._context = self._page = None
        self._origin_ready = False

    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        async with self._guard():
            await self._shutdown()

    def _guard(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _touch(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()

        async def idle_close() -> None:
            await asyncio.sleep(_IDLE_CLOSE_SECONDS)
            async with self._guard():
                await self._shutdown()

        self._idle_task = asyncio.ensure_future(idle_close())

    async def _check_captcha(self, page) -> bool:
        try:
            title = await page.title()
        except Exception:
            return False
        if "验证码" in title:
            if callable(self.on_captcha):
                self.on_captcha()
            return True
        return False

    async def request_api(self, path: str, params: Dict[str, Any], *, method: str = "GET",
                          data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Replay one API request from the logged-in page."""
        if not self.enabled or self.cooldown_remaining() > 0:
            return None
        if str(path).startswith(DETAIL_PATH):
            # Opening the work page is the only detail request the page makes
            # on its own; replaying the API from it again only adds traffic.
            detail = await self.work_detail(str(params.get("aweme_id") or ""))
            return {"status_code": 0, "aweme_detail": detail} if detail else None
        async with self._guard():
            page = await self._ensure()
            self._touch()
            if not self._origin_ready:
                await page.goto(BASE_URL, wait_until="domcontentloaded",
                                timeout=_NAVIGATION_TIMEOUT_MS)
                self._origin_ready = True
                if await self._check_captcha(page):
                    return None
            url = f"{BASE_URL}{path}?{urlencode([(str(k), str(v)) for k, v in params.items()])}"
            body = urlencode([(str(k), str(v)) for k, v in (data or {}).items()])
            result = await asyncio.wait_for(page.evaluate(
                """async ({url, method, body, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const init = {method, credentials: 'include', signal: controller.signal};
                    if (method === 'POST') {
                      init.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
                      init.body = body;
                    }
                    const response = await fetch(url, init);
                    return {status: response.status, text: await response.text()};
                  } finally { clearTimeout(timer); }
                }""",
                {"url": url, "method": str(method or "GET").upper(), "body": body,
                 "timeoutMs": int(_REQUEST_TIMEOUT * 1000)},
            ), _REQUEST_TIMEOUT + 5)
        status = int((result or {}).get("status") or 0)
        text = str((result or {}).get("text") or "")
        if status != 200:
            self._failed(f"HTTP {status}")
            raise RuntimeError(f"页面通道 HTTP {status}：{text[:160]}")
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            self._failed("non-JSON answer")
            raise RuntimeError(f"页面通道返回非 JSON：{text[:160]}") from exc
        self._succeeded()
        return decoded if isinstance(decoded, dict) else None

    async def work_detail(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        """Open the work page and capture the player's own detail response."""
        if not self.enabled or not aweme_id or self.cooldown_remaining() > 0:
            return None
        async with self._guard():
            if self.cooldown_remaining() > 0:   # another caller just failed
                return None
            page = await self._ensure()
            self._touch()
            try:
                async with page.expect_response(
                    lambda response: DETAIL_PATH in str(response.url)
                    and aweme_id in str(response.url),
                    timeout=_NAVIGATION_TIMEOUT_MS,
                ) as captured:
                    await page.goto(f"{BASE_URL}/video/{aweme_id}",
                                    wait_until="domcontentloaded",
                                    timeout=_NAVIGATION_TIMEOUT_MS)
                response = await captured.value
                if int(getattr(response, "status", 0) or 0) != 200:
                    self._failed(f"detail HTTP {getattr(response, 'status', 0)}")
                    return None
                payload = json.loads(await asyncio.wait_for(response.text(), 10))
            except Exception as exc:
                await self._check_captcha(page)
                logger.info("Browser detail capture failed for %s: %s", aweme_id, exc)
                self._failed("detail capture")
                return None
            finally:
                self._origin_ready = True
        detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and str(detail.get("aweme_id") or "") == aweme_id:
            self._succeeded()
            return detail
        return None

    async def screenshot(self, sec_uid: str, target: Path) -> bool:
        """Save the first screen of a creator homepage."""
        if not self.enabled:
            return False
        async with self._guard():
            try:
                page = await self._ensure()
                self._touch()
                await page.goto(f"{BASE_URL}/user/{sec_uid}", wait_until="domcontentloaded",
                                timeout=_NAVIGATION_TIMEOUT_MS)
                self._origin_ready = True
                if await self._check_captcha(page):
                    return False
                await page.wait_for_timeout(1500)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.tmp.png")
                await page.screenshot(path=str(temp), type="png", full_page=False)
                temp.replace(target)
                return True
            except Exception as exc:
                logger.info("Homepage screenshot failed for %s: %s", sec_uid, exc)
                return False


# The QR image as the login dialog shows it (fallback when the passport
# responses cannot be read).
_QR_IMAGE_JS = r"""() => {
  const img = document.querySelector('#douyin_login_comp_scan_code img[src^="data:image"]')
    || document.querySelector('#animate_qrcode_container img[src^="data:image"]');
  return img ? img.src : '';
}"""
_QR_WAIT_SECONDS = 45.0
_QR_PASSPORT_PATHS = ("/passport/web/get_qrcode/", "/passport/web/check_qrconnect/")
_QR_STATUSES = {"new": "waiting", "scanned": "scanned", "confirmed": "scanned",
                "expired": "expired"}


def _douyin_jar(raw: List[Dict[str, Any]]) -> Dict[str, str]:
    return cookie_tools.sanitize({
        item["name"]: item["value"] for item in raw
        if str(item.get("domain") or "").lstrip(".").endswith("douyin.com")
    })


def qr_passport_update(url: str, payload: Any) -> Dict[str, str]:
    """What one passport QR response says: an ``image``, a ``status`` or an ``error``.

    ``get_qrcode`` carries the QR code as base64 PNG; ``check_qrconnect`` is
    the page's own poll (``new`` / ``scanned`` / ``confirmed`` / ``expired``).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    if to_int(data.get("error_code")):
        return {"error": str(data.get("description") or f"错误码 {data.get('error_code')}")}
    path = urlparse(url).path
    if path.endswith(_QR_PASSPORT_PATHS[0]):
        image = str(data.get("qrcode") or "")
        return {"image": f"data:image/png;base64,{image}"} if image else {}
    if path.endswith(_QR_PASSPORT_PATHS[1]):
        status = _QR_STATUSES.get(str(data.get("status") or ""))
        return {"status": status} if status else {}
    return {}


async def qr_login(*, timeout_seconds: int = 300, headless: bool = False, user_agent: str = "",
                   proxy: str = "", qr_bootstrap: Optional[QrBootstrap] = None,
                   on_qr: Optional[Callable[[str], None]] = None,
                   on_status: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Log in by QR code at douyin.com and return the cookies.

    With ``headless`` the page stays hidden: every (refreshed) QR code goes to
    ``on_qr`` as a ``data:`` URL and scan progress to ``on_status``
    (``waiting`` / ``scanned`` / ``expired``), so the app can show them itself.
    Otherwise a visible browser window opens and the user scans it there.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，无法打开扫码登录窗口。请先执行：pip install playwright") \
            from exc
    loop = asyncio.get_running_loop()
    requested_at = loop.time()
    if on_status is not None:
        on_status("starting_browser")
    async with async_playwright() as playwright:
        browser, errors = None, []
        for channel in ("msedge", "chrome", None):
            try:
                extra = {"channel": channel} if channel else {}
                if headless:
                    extra["args"] = _LAUNCH_ARGS
                if proxy:
                    extra["proxy"] = {"server": proxy}
                browser = await playwright.chromium.launch(headless=headless, timeout=15000,
                                                            **extra)
                break
            except Exception as exc:
                errors.append(f"{channel or 'chromium'}: {exc}")
        if browser is None:
            raise RuntimeError("无法启动抖音登录窗口。请确认已安装 Microsoft Edge 或 Google Chrome。\n"
                               + "\n".join(errors[-3:]))
        logger.info("QR login: browser ready in %.1f s", loop.time() - requested_at)
        try:
            options: Dict[str, Any] = {}
            if headless:
                # The headless default UA says "HeadlessChrome"; look like the app's client.
                options = {"locale": "zh-CN", "viewport": {"width": 1600, "height": 900},
                           "screen": {"width": 1920, "height": 1080}}
                if user_agent:
                    options["user_agent"] = user_agent
            context = await browser.new_context(**options)
            if qr_bootstrap is not None:
                await context.add_cookies(qr_bootstrap.cookies)
            page = await context.new_page()
            if headless and user_agent:
                await _match_client_hints(context, page, user_agent)
            seen_tokens: List[str] = []
            updates: List[Dict[str, str]] = []
            shown_src = qr_bootstrap.image if qr_bootstrap is not None else ""
            shown_status = "waiting" if shown_src else ""
            pending_bootstrap = qr_bootstrap

            def show_qr(src: str) -> None:
                nonlocal shown_src, shown_status
                if src and src != shown_src:
                    if not shown_src:
                        logger.info("QR login: first code ready in %.1f s",
                                    loop.time() - requested_at)
                    shown_src, shown_status = src, "waiting"
                    if on_qr is not None:
                        on_qr(src)

            def show_status(status: str) -> None:
                nonlocal shown_status
                if status and status != shown_status:
                    shown_status = status
                    if on_status is not None:
                        on_status(status)

            def on_request(request: Any) -> None:
                try:
                    parsed = urlparse(request.url or "")
                    if parsed.path.endswith(_QR_PASSPORT_PATHS[0]):
                        logger.info("QR login: code requested at %.1f s", loop.time() - requested_at)
                        if not shown_src:
                            show_status("requesting_qr")
                    elif parsed.path.endswith(("/passport/general/login_guiding_strategy/",
                                                "/passport/web/challenge/")):
                        logger.info("QR login: official SDK step %s at %.1f s",
                                    parsed.path, loop.time() - requested_at)
                    token = (parse_qs(parsed.query).get("msToken") or [""])[0]
                    token = token or cookie_tools.ms_token_in(
                        (request.headers or {}).get("cookie", ""))
                    if token:
                        seen_tokens.append(token.strip())
                except Exception:
                    return

            async def on_response(response: Any) -> None:
                if not urlparse(response.url or "").path.endswith(_QR_PASSPORT_PATHS):
                    return
                try:
                    update = qr_passport_update(response.url, await response.json())
                except Exception:
                    return
                if update:
                    # Deliver the code immediately, even while navigation or a
                    # DOM operation is still pending; do not wait for polling.
                    show_qr(update.get("image") or "")
                    show_status(update.get("status") or "")
                    if update.get("error") or update.get("status") == "expired":
                        updates.append(update)

            page.on("request", on_request)
            if headless:
                page.on("response", on_response)
                # The official SDK receives the exact QR/token already shown
                # in the app. It must not create a different code behind it.
                async def login_route(route: Any) -> None:
                    nonlocal pending_bootstrap
                    parsed = urlparse(route.request.url or "")
                    if (pending_bootstrap is not None and parsed.scheme == "https"
                            and parsed.hostname in ("login.douyin.com", "www.douyin.com")
                            and parsed.path == _QR_PASSPORT_PATHS[0]):
                        bootstrap, pending_bootstrap = pending_bootstrap, None
                        await route.fulfill(status=200, content_type="application/json",
                                            body=json.dumps(bootstrap.payload))
                        logger.info("QR login: direct code attached to official SDK at %.1f s",
                                    loop.time() - requested_at)
                        return
                    await _skip_media(route)

                await page.route("**/*", login_route)
            show_status("loading_page")
            try:
                # The login dialog comes up long before the feed finishes loading.
                await page.goto(BASE_URL, wait_until="commit", timeout=30000)
            except Exception as exc:
                if "closed" in str(exc).lower():
                    raise RuntimeError("登录窗口已关闭，未完成登录。") from exc
                if headless:
                    raise RuntimeError(f"抖音登录页打开失败：{exc}") from exc
            started = loop.time()
            deadline = requested_at + max(30, int(timeout_seconds))
            opened, next_open_attempt = False, 0.0
            if not shown_src:
                show_status("loading_login_sdk")
            while loop.time() < deadline:
                src = ""
                while updates:
                    update = updates.pop(0)
                    if update.get("error"):
                        raise RuntimeError(f"抖音提示：{update['error']}。可改用浏览器窗口登录。")
                    if update.get("status") == "expired":
                        await _refresh_qr(page)
                if not shown_src:
                    try:
                        src = str(await asyncio.wait_for(page.evaluate(_QR_IMAGE_JS), 1.0) or "")
                    except Exception:
                        src = ""
                    show_qr(src)
                if (not shown_src or pending_bootstrap is not None) and not opened \
                        and loop.time() >= next_open_attempt:
                    # Also open the SDK when a direct QR is already visible:
                    # it still owns protected polling and login confirmation.
                    next_open_attempt = loop.time() + 1.0
                    opened = await _open_login_dialog(page)
                    if opened:
                        logger.info("QR login: dialog opened at %.1f s",
                                    loop.time() - requested_at)
                if headless and not shown_src and loop.time() - started > _QR_WAIT_SECONDS:
                    raise RuntimeError("没有取到登录二维码，请改用浏览器窗口登录。")
                # Looking for cookies before the code is ready only adds a
                # browser round-trip to every iteration of the critical path.
                if shown_src or not headless:
                    try:
                        jar = _douyin_jar(await context.cookies())
                    except Exception as exc:
                        raise RuntimeError("登录窗口已关闭，未完成登录。") from exc
                    if cookie_tools.is_logged_in(jar):
                        await asyncio.sleep(1.2)
                        jar = _douyin_jar(await context.cookies())
                        if not jar.get("msToken") and seen_tokens:
                            jar["msToken"] = seen_tokens[-1]
                        return jar
                await asyncio.sleep(0.2 if not shown_src else 0.5)
            raise TimeoutError("扫码登录等待超时，请重新点击扫码登录再试。")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _skip_media(route: Any) -> None:
    # Keep authentication/verification images: blocking those can prevent the
    # login dialog from becoming ready. Feed images, videos and fonts are unused.
    url = str(route.request.url or "").lower()
    authentication = any(part in url for part in ("passport", "qrcode", "captcha", "verify"))
    if route.request.resource_type in ("image", "media", "font") and not authentication:
        await route.abort()
    else:
        await route.continue_()


async def _open_login_dialog(page: Any) -> bool:
    for selector in ("button:has-text('登录')", "[role='button']:has-text('登录')",
                     "text=登录"):
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                await locator.click(timeout=500)
                return True
        except Exception:
            continue
    return False


async def _refresh_qr(page: Any) -> None:
    box = page.locator("#douyin_login_comp_scan_code")
    for target in (box.locator("text=刷新").first, box.locator("img").first):
        try:
            await target.click(timeout=2000)
            return
        except Exception:
            continue
