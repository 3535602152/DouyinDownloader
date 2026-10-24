"""Fast QR acquisition, with the official browser SDK completing authentication.

Only QR acquisition uses HTTP directly. Passport's protected status polling,
confirmation and cookie exchange still run in the official login page. No
credentials, QR tokens or callback URLs are logged or persisted here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from common.log import get_logger
from douyin import transport

logger = get_logger(__name__)
GET_QR = "https://login.douyin.com/passport/web/get_qrcode/"
REQUEST_TIMEOUT = 2.5
_PARAMS = {
    "passport_jssdk_version": "3.4.9",
    "passport_jssdk_type": "normal",
    "is_from_ttaccountsdk": "1",
    "aid": "6383",
    "language": "zh",
    "account_app_language": "zh-CN",
    "need_short_url": "true",
    "need_logo": "false",
    "is_new_login": "1",
    "is_from_iesaccountsaas": "1",
    "account_sdk_source": "web",
    "device_platform": "web_app",
    "next": "https://www.douyin.com/",
}


class DirectQrUnavailable(RuntimeError):
    """The short HTTP attempt failed; fall back to the official page."""


@dataclass
class QrBootstrap:
    payload: Dict[str, Any] = field(repr=False)
    cookies: List[Dict[str, Any]] = field(repr=False)
    image: str = field(repr=False)


def parse_qr(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise DirectQrUnavailable("二维码接口未返回有效数据")
    if data.get("error_code") not in (None, 0, "0"):
        raise DirectQrUnavailable("二维码接口要求官网验证")
    if not data.get("token"):
        raise DirectQrUnavailable("二维码接口未返回登录令牌")
    encoded = data.get("qrcode")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 1024 * 1024:
        raise DirectQrUnavailable("二维码接口未返回有效图片")
    prefix = "data:image/png;base64,"
    encoded = encoded[len(prefix):] if encoded.startswith(prefix) else encoded
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise DirectQrUnavailable("二维码图片格式异常") from exc
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DirectQrUnavailable("二维码图片不是 PNG")
    try:
        expires = float(data.get("expire_time") or 0)
    except (ValueError, TypeError) as exc:
        raise DirectQrUnavailable("二维码有效期异常") from exc
    if expires and expires <= time.time():
        raise DirectQrUnavailable("接口返回的二维码已过期")
    return prefix + encoded


async def fetch_qr(*, user_agent: str = "", proxy: str = "") -> QrBootstrap:
    """Use an isolated guest session; never reuse the current account's cookies."""
    session = transport.create({})
    started = time.monotonic()
    headers = {
        "User-Agent": user_agent or transport.USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json",
        "sec-ch-ua": transport.SEC_CH_UA,
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        response = await session.request("GET", GET_QR + "?" + urlencode(_PARAMS),
                                         headers=headers, proxy=proxy or None,
                                         timeout=REQUEST_TIMEOUT, allow_redirects=False)
        if response.status != 200 or len(response.body) > 2 * 1024 * 1024:
            raise DirectQrUnavailable("二维码接口暂不可用")
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DirectQrUnavailable("二维码接口未返回 JSON") from exc
        image = parse_qr(payload)
        bootstrap = QrBootstrap(payload, session.browser_cookies(), image)
        logger.info("QR login: direct code ready in %.2f s", time.monotonic() - started)
        return bootstrap
    except DirectQrUnavailable:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Transport exceptions may contain the full request URL. Do not log them.
        raise DirectQrUnavailable("二维码接口请求失败") from exc
    finally:
        await session.close()


async def qr_login(*, timeout_seconds: int = 300, headless: bool = False,
                   user_agent: str = "", proxy: str = "",
                   on_qr: Optional[Callable[[str], None]] = None,
                   on_status: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    from douyin.browser import qr_login as browser_login

    bootstrap = None
    if headless:
        if on_status is not None:
            on_status("requesting_qr")
        try:
            bootstrap = await asyncio.wait_for(
                fetch_qr(user_agent=user_agent, proxy=proxy), REQUEST_TIMEOUT)
        except (DirectQrUnavailable, asyncio.TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, DirectQrUnavailable) else "二维码接口请求超时"
            logger.info("QR login: %s; using official browser page", reason)
            if on_status is not None:
                on_status("browser_fallback")
        if bootstrap is not None:
            if on_qr is not None:
                on_qr(bootstrap.image)
            if on_status is not None:
                on_status("waiting")
    return await browser_login(timeout_seconds=timeout_seconds, headless=headless,
                               user_agent=user_agent, proxy=proxy, qr_bootstrap=bootstrap,
                               on_qr=on_qr, on_status=on_status)
