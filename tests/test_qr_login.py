from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from douyin import browser as module


@pytest.fixture
def login_browser(monkeypatch):
    state = SimpleNamespace(mode="click", images=[], statuses=[], sleeps=[], clicked=False,
                            cookies_read=0, closed=False, during_navigation=False)

    class Response:
        url = "https://login.douyin.com/passport/web/get_qrcode/"

        async def json(self):
            return {"data": {"qrcode": "test-code"}}

    class Page:
        def __init__(self):
            self.handlers = {}
            self.first = self

        def on(self, name, handler):
            self.handlers[name] = handler

        async def route(self, pattern, handler):
            state.route = handler

        async def goto(self, url, **kwargs):
            assert kwargs["wait_until"] == "commit"
            if state.mode == "response":
                state.during_navigation = True
                await self.handlers["response"](Response())
                assert state.images == ["data:image/png;base64,test-code"]
                state.during_navigation = False

        async def evaluate(self, script):
            return "data:image/png;base64,test-code" if state.mode == "dom" else ""

        def locator(self, selector):
            return self

        async def is_visible(self):
            return True

        async def click(self, **kwargs):
            state.clicked = True
            state.sleeps_before_click = list(state.sleeps)
            assert state.cookies_read == 0
            if state.mode == "handoff":
                state.intercepted = []
                state.continued = 0

                async def fulfill(**options):
                    import json

                    state.intercepted.append(json.loads(options["body"]))

                async def proceed():
                    state.continued += 1

                route = SimpleNamespace(
                    request=SimpleNamespace(url=Response.url, resource_type="xhr"),
                    fulfill=fulfill, continue_=proceed)
                await state.route(route)
                await state.route(route)  # Refreshes must get a new official QR.
            self.handlers["request"](SimpleNamespace(url=Response.url, headers={}))
            await self.handlers["response"](Response())

    page = Page()

    class Context:
        async def add_cookies(self, cookies):
            state.seed_cookies = cookies

        async def new_page(self):
            return page

        async def cookies(self):
            state.cookies_read += 1
            return [{"name": "sessionid", "value": "test-session", "domain": ".douyin.com"}]

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self):
            state.closed = True

    async def launch(**kwargs):
        assert kwargs["timeout"] == 15000
        return Browser()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *args):
            pass

    original_sleep = asyncio.sleep

    async def sleep(seconds):
        state.sleeps.append(seconds)
        await original_sleep(0)

    # These regressions run offline even without the optional browser extra.
    playwright = ModuleType("playwright")
    async_api = ModuleType("playwright.async_api")
    async_api.async_playwright = Manager
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    return state


async def run_login(state):
    return await module.qr_login(headless=True, on_qr=state.images.append,
                                 on_status=state.statuses.append)


async def test_login_opens_immediately_without_fixed_sleep(login_browser):
    state = login_browser
    jar = await run_login(state)
    assert jar["sessionid"] == "test-session"
    assert state.clicked and state.sleeps_before_click == []
    assert state.images == ["data:image/png;base64,test-code"]
    assert state.statuses == ["starting_browser", "loading_page", "loading_login_sdk",
                              "requesting_qr"]
    assert state.closed


async def test_qr_response_is_delivered_before_navigation_finishes(login_browser):
    state = login_browser
    state.mode = "response"
    await run_login(state)
    assert state.images == ["data:image/png;base64,test-code"]
    assert not state.clicked
    assert "requesting_qr" not in state.statuses
    assert state.closed


async def test_qr_dom_fallback_does_not_click_an_existing_code(login_browser):
    state = login_browser
    state.mode = "dom"
    await run_login(state)
    assert state.images == ["data:image/png;base64,test-code"]
    assert not state.clicked and state.closed


async def test_direct_qr_is_handed_to_sdk_once_without_replacing_image(login_browser):
    from douyin.qr import QrBootstrap

    state = login_browser
    state.mode = "handoff"
    image = "data:image/png;base64,test-code"
    payload = {"data": {"qrcode": "test-code", "token": "same-token"}}
    cookies = [{"name": "csrf", "value": "guest", "domain": ".douyin.com", "path": "/"}]
    seed = QrBootstrap(payload, cookies, image)
    await module.qr_login(headless=True, qr_bootstrap=seed,
                          on_qr=state.images.append, on_status=state.statuses.append)
    assert state.clicked and state.seed_cookies == cookies
    assert state.intercepted == [payload] and state.continued == 1
    assert state.images == []  # The wrapper already showed this exact code.
    assert state.closed


@pytest.mark.parametrize("resource, url, blocked", [
    ("media", "https://cdn.example/video.mp4", True),
    ("image", "https://cdn.example/cover.jpg", True),
    ("font", "https://cdn.example/font.woff", True),
    ("image", "https://login.douyin.com/passport/qr.png", False),
    ("image", "https://cdn.example/captcha/image", False),
    ("script", "https://cdn.example/login.js", False),
    ("xhr", "https://login.douyin.com/passport/web/get_qrcode/", False),
])
async def test_login_resource_filter_preserves_authentication(resource, url, blocked):
    called = []

    async def abort():
        called.append("abort")

    async def proceed():
        called.append("continue")

    route = SimpleNamespace(request=SimpleNamespace(resource_type=resource, url=url),
                            abort=abort, continue_=proceed)
    await module._skip_media(route)
    assert called == ["abort" if blocked else "continue"]
