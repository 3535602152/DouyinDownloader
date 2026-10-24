from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest

from douyin import browser, qr, transport

IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
PAYLOAD = {"data": {"error_code": 0, "qrcode": IMAGE.split(",", 1)[1],
                    "token": "private-qr-token", "expire_time": time.time() + 300}}
COOKIES = [{"name": "passport_csrf_token", "value": "guest-csrf", "domain": ".douyin.com",
            "path": "/", "secure": True}]


class Session:
    def __init__(self, payload=PAYLOAD):
        self.payload = payload
        self.closed = False
        self.requests = []

    async def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return transport.Response(200, json.dumps(self.payload).encode(), qr.GET_QR,
                                  {"content-type": "application/json"})

    def browser_cookies(self):
        return COOKIES

    async def close(self):
        self.closed = True


def test_qr_parser_accepts_only_valid_unexpired_png_with_token():
    assert qr.parse_qr(PAYLOAD) == IMAGE
    prefixed = {"data": {**PAYLOAD["data"], "qrcode": IMAGE}}
    assert qr.parse_qr(prefixed) == IMAGE
    for payload in (None, {}, {"data": {"error_code": 4031}},
                    {"data": {**PAYLOAD["data"], "token": ""}},
                    {"data": {**PAYLOAD["data"], "qrcode": "invalid"}},
                    {"data": {**PAYLOAD["data"], "expire_time": 1}}):
        with pytest.raises(qr.DirectQrUnavailable):
            qr.parse_qr(payload)


async def test_direct_request_uses_an_isolated_guest_session(monkeypatch):
    session = Session()
    given = []
    monkeypatch.setattr(transport, "create", lambda cookies: given.append(cookies) or session)
    seed = await qr.fetch_qr(user_agent=transport.USER_AGENT, proxy="http://proxy:8080")
    assert given == [{}] and session.closed
    assert seed.payload == PAYLOAD and seed.cookies == COOKIES and seed.image == IMAGE
    args, options = session.requests[0]
    assert args[0] == "GET" and args[1].startswith(qr.GET_QR)
    assert options["proxy"] == "http://proxy:8080" and not options["allow_redirects"]
    assert options["timeout"] == qr.REQUEST_TIMEOUT
    assert "private-qr-token" not in repr(seed) and "guest-csrf" not in repr(seed)


async def test_direct_cancellation_closes_guest_session(monkeypatch):
    session = Session()
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    session.request = blocked
    monkeypatch.setattr(transport, "create", lambda cookies: session)
    task = asyncio.create_task(qr.fetch_qr())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed


async def test_direct_code_is_shown_before_browser_sdk_starts(monkeypatch):
    events = []
    seed = qr.QrBootstrap(PAYLOAD, COOKIES, IMAGE)

    async def fetch(**kwargs):
        return seed

    async def sdk(**kwargs):
        assert events == ["requesting_qr", IMAGE, "waiting"]
        assert kwargs["qr_bootstrap"] is seed and kwargs["headless"]
        assert kwargs["proxy"] == "http://proxy:8080"
        return {"sessionid": "confirmed-session"}

    monkeypatch.setattr(qr, "fetch_qr", fetch)
    monkeypatch.setattr(browser, "qr_login", sdk)
    result = await qr.qr_login(headless=True, proxy="http://proxy:8080",
                               on_qr=events.append, on_status=events.append)
    assert result == {"sessionid": "confirmed-session"}


@pytest.mark.parametrize("error", [qr.DirectQrUnavailable("官网要求验证"), asyncio.TimeoutError()])
async def test_direct_failure_falls_back_before_showing_any_code(monkeypatch, error):
    events = []

    async def fetch(**kwargs):
        raise error

    async def sdk(**kwargs):
        assert kwargs["qr_bootstrap"] is None
        assert events == ["requesting_qr", "browser_fallback"]
        kwargs["on_qr"]("browser-qr")
        return {"sessionid": "confirmed-session"}

    monkeypatch.setattr(qr, "fetch_qr", fetch)
    monkeypatch.setattr(browser, "qr_login", sdk)
    await qr.qr_login(headless=True, on_qr=events.append, on_status=events.append)
    assert events[-1] == "browser-qr"


async def test_visible_browser_mode_does_not_use_direct_request(monkeypatch):
    async def forbidden(**kwargs):
        pytest.fail("manual browser mode must not request a different QR code")

    async def sdk(**kwargs):
        assert kwargs["qr_bootstrap"] is None and not kwargs["headless"]
        return {"sessionid": "confirmed-session"}

    monkeypatch.setattr(qr, "fetch_qr", forbidden)
    monkeypatch.setattr(browser, "qr_login", sdk)
    assert await qr.qr_login(headless=False) == {"sessionid": "confirmed-session"}


def test_browser_cookie_export_keeps_domain_boundaries_and_paths():
    session = transport.CurlTransport.__new__(transport.CurlTransport)
    cookies = [SimpleNamespace(name="csrf", value="guest", domain=domain, path="/passport/",
                               secure=True, _rest={"HttpOnly": None})
               for domain in (".douyin.com", "login.douyin.com", "notdouyin.com", "example.com")]
    session._session = SimpleNamespace(cookies=SimpleNamespace(jar=cookies))
    exported = session.browser_cookies()
    assert [item["domain"] for item in exported] == [".douyin.com", "login.douyin.com"]
    assert all(item["path"] == "/passport/" and item["httpOnly"] for item in exported)


async def test_aiohttp_guest_cookie_export_is_scoped():
    from http.cookies import SimpleCookie

    from yarl import URL

    session = transport.AiohttpTransport({})
    try:
        cookie = SimpleCookie("csrf=guest; Domain=.douyin.com; Path=/; Secure; HttpOnly")
        session._session.cookie_jar.update_cookies(cookie, response_url=URL(qr.GET_QR))
        exported = session.browser_cookies()
        assert len(exported) == 1
        assert exported[0]["name"] == "csrf" and exported[0]["httpOnly"]
    finally:
        await session.close()
