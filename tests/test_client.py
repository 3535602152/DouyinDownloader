from __future__ import annotations

from douyin.client import DouyinClient
from douyin.errors import EmptyResponse


async def test_a_gentle_detail_stops_at_the_first_risk_answer():
    client = DouyinClient()
    sent = []

    async def query(**params):
        return params

    async def request(path, params, **options):
        sent.append((params["aid"], options))
        raise EmptyResponse("empty 200", path=path)

    client.query = query
    client.request = request
    try:
        await client.aweme_detail("1", gentle=True)
    except EmptyResponse:
        pass
    else:
        raise AssertionError("the risk answer must surface")
    # One request, no retries, no page fallback, no second aid.
    assert sent == [("6383", {"attempts": 1, "page_fallback": False})]


def test_the_detail_endpoint_is_signed_like_every_other_path():
    client = DouyinClient({"UIFID": "u" * 32, "s_v_web_id": "verify_x"})
    url, _ua = client.signed_url("/aweme/v1/web/aweme/detail/", {"aweme_id": "1", "aid": "6383"})
    # a_bogus first, then the web signature over that query.
    assert "a_bogus=" in url and "x-secsdk-web-signature=" in url


def test_one_browser_identity_for_every_request(monkeypatch):
    import douyin.client as module
    from douyin.transport import CHROME_VERSION, USER_AGENT

    seen = []

    class Signer:
        def __init__(self, fp, user_agent):
            seen.append((fp, user_agent))

        def generate_abogus(self, query, body):
            return f"{query}&a_bogus=x", "x", self.__dict__, body

    monkeypatch.setattr(module, "ABogus", Signer)
    client = DouyinClient()
    for aweme_id in ("1", "2", "3"):
        client.signed_url("/aweme/v1/web/aweme/post/", {"sec_user_id": aweme_id})
    assert len({fp for fp, _ua in seen}) == 1            # the screen never changes
    assert {ua for _fp, ua in seen} == {USER_AGENT}
    query = module.default_query("")
    assert query["browser_version"] == query["engine_version"] == CHROME_VERSION
    assert f"Chrome/{CHROME_VERSION}" in USER_AGENT and "Windows" in USER_AGENT
    assert query["os_name"] == "Windows" and query["browser_platform"] == "Win32"


async def test_an_anti_bot_page_is_told_apart_from_other_failures():
    from douyin.transport import Response

    client = DouyinClient()
    reasons = []
    client.gate.note_risk = lambda path, reason, detail="": reasons.append((reason, detail)) or 0

    class Wall:
        async def request(self, method, url, **options):
            return Response(200, b"<html><script>window.__ac_nonce='x'</script></html>", url,
                            {"content-type": "text/html; charset=utf-8"})

        async def close(self):
            pass

    client._transport = Wall()
    try:
        await client.request("/aweme/v1/web/aweme/post/", {"sec_user_id": "S"}, attempts=1,
                             page_fallback=False)
    except EmptyResponse:
        pass
    else:
        raise AssertionError("the wall must not pass as data")
    assert reasons[0][0] == "ac-challenge" and "text/html" in reasons[0][1]


def test_api_requests_look_like_a_page_fetch_on_windows():
    import douyin.client as module
    from douyin.transport import CHROME_MAJOR, SEC_CH_UA

    client = DouyinClient()
    headers = client.api_headers(module._referer(module.POST_PATH, {"sec_user_id": "SEC"}))
    # Everything describes the same Windows Chrome fetching from a douyin.com page.
    assert headers["sec-ch-ua-platform"] == '"Windows"' and "Windows" in headers["User-Agent"]
    assert f'v="{CHROME_MAJOR}"' in SEC_CH_UA and headers["sec-ch-ua"] == SEC_CH_UA
    assert (headers["Sec-Fetch-Mode"], headers["Sec-Fetch-Dest"], headers["Sec-Fetch-Site"]) \
        == ("cors", "empty", "same-origin")
    assert "Upgrade-Insecure-Requests" not in headers and "Sec-Fetch-User" not in headers
    assert headers["Referer"] == "https://www.douyin.com/user/SEC"
    assert module._referer(module.DETAIL_PATH, {"aweme_id": "7"}).endswith("/video/7")
    assert module._referer(module.FOLLOW_FEED_PATH, {}).endswith("/follow")


async def test_curl_does_not_add_its_own_navigation_headers():
    import pytest

    pytest.importorskip("curl_cffi")
    from douyin.transport import CurlTransport

    transport = CurlTransport({})
    try:
        # curl_cffi's defaults are a macOS Chrome opening a page.
        assert not transport._session.default_headers
    finally:
        await transport.close()


def _posts_client(answers):
    client = DouyinClient()
    sent = []

    async def query(**params):
        return params

    async def request(path, params, **options):
        sent.append(params["max_cursor"])
        return answers.pop(0)

    client.query = query
    client.request = request
    return client, sent


def _empty(cursor, **extra):
    return {"status_code": 0, "aweme_list": [], "has_more": 1, "max_cursor": cursor, **extra}


async def test_posts_step_over_empty_pages_that_go_on():
    work = {"aweme_id": "1"}
    # A real gap: eight empty pages in a row, each a couple of weeks older.
    gap = [_empty(100 - 10 * step) for step in range(1, 9)]
    client, sent = _posts_client([*gap, {"status_code": 0, "aweme_list": [work],
                                         "has_more": 1, "max_cursor": 5}])
    page = await client.user_posts("sec", 100)
    assert sent == [100, 90, 80, 70, 60, 50, 40, 30, 20]
    assert page.items == [work] and page.cursor == 5


async def test_posts_give_up_after_too_many_empty_pages():
    from douyin.client import MAX_EMPTY_POST_PAGES
    from douyin.errors import PaginationError

    answers = [_empty(1000 - index) for index in range(MAX_EMPTY_POST_PAGES + 1)]
    client, sent = _posts_client(answers)
    page = await client.user_posts("sec", 2000)
    assert len(sent) == MAX_EMPTY_POST_PAGES + 1
    try:
        page.ensure_valid("作品列表")
    except PaginationError:
        pass
    else:
        raise AssertionError("an endless run of empty pages must stop the scan")


async def test_posts_refuse_an_empty_page_with_a_stuck_cursor():
    from douyin.errors import PaginationError

    for cursor in (100, 0, 150):        # same, none, or newer instead of older
        client, sent = _posts_client([_empty(cursor)])
        try:
            await client.user_posts("sec", 100)
        except PaginationError:
            pass
        else:
            raise AssertionError("a cursor that does not move must not be followed")
        assert sent == [100]


async def test_posts_never_skip_a_suspicious_empty_page():
    from douyin.errors import RiskControl

    client, sent = _posts_client([_empty(90, not_login_module={"guide_login_tip_exist": 1})])
    page = await client.user_posts("sec", 100)
    assert sent == [100]
    try:
        page.ensure_valid("作品列表")
    except RiskControl:
        pass
    else:
        raise AssertionError("a login prompt is not an empty page")


async def test_comment_replies_and_emojis_ask_like_the_web_player():
    client = DouyinClient()
    sent = []
    options_seen = options = []

    async def query(**params):
        return params

    async def request(path, params, **options):
        sent.append((path, params))
        options_seen.append(options)
        if path.endswith("emoji/list"):
            return {"status_code": 0, "emoji_list": [
                {"display_name": "[微笑]", "emoji_url": {"url_list": ["https://p.example/s.png"]}},
                {"display_name": "", "emoji_url": {"url_list": ["https://p.example/x.png"]}}]}
        return {"status_code": 0, "comments": [], "has_more": 0}

    client.query = query
    client.request = request
    try:
        await client.comment_replies("1", "2", 0, 3, gentle=True, page_fallback=True)
        assert sent[-1][1]["item_type"] == "0" and sent[-1][1]["count"] == 3
        assert options[-1] == {"attempts": 1, "page_fallback": True}
        assert await client.emoji_list() == {"[微笑]": "https://p.example/s.png"}
    finally:
        await client.close()


def test_reply_pages_are_requested_from_the_video_page():
    from douyin.client import COMMENT_REPLY_PATH, _referer

    assert _referer(COMMENT_REPLY_PATH, {"item_id": "7"}).endswith("/video/7")


async def test_reply_pages_are_signed_with_xbogus():
    from douyin.client import COMMENT_PATH, COMMENT_REPLY_PATH

    client = DouyinClient()
    forced = []

    def signed_url(path, params, *, body=None, force_xbogus=False):
        forced.append((path, force_xbogus))
        raise RuntimeError("stop before the wire")

    async def session():
        return None

    client.signed_url = signed_url
    client.session = session
    for path in (COMMENT_REPLY_PATH, COMMENT_PATH):
        try:
            await client.request(path, {}, attempts=1, page_fallback=False)
        except RuntimeError:
            pass
    # An a_bogus-signed reply page comes back as an empty 200.
    assert forced == [(COMMENT_REPLY_PATH, True), (COMMENT_PATH, False)]


def test_a_path_in_risk_cooldown_does_not_hold_back_other_paths():
    from douyin.risk import RiskGate

    gate = RiskGate(2.0)
    reply = "/aweme/v1/web/comment/list/reply/"
    for _ in range(10):
        gate.note_risk(reply, "empty-200")
    assert gate.reserve(reply) >= 59
    assert gate.reserve("/aweme/v1/web/user/following/list/") < 2


async def test_each_account_has_its_own_session():
    import asyncio

    from douyin.client import use_account
    from douyin.errors import LoginRequired
    from douyin.transport import Response

    client = DouyinClient({"sessionid": "main", "msToken": "m"})
    await client.sync_accounts("ME", {"sessionid": "main", "msToken": "m"},
                               {"ME": {"sessionid": "main"}, "B": {"sessionid": "b",
                                                                   "msToken": "b"}})
    lost, notified = [], []
    client.on_login_lost = lost.append
    client.gate.observers.append(lambda what, path, value: notified.append(what))

    class Expired:
        async def request(self, method, url, **options):
            return Response(200, br'{"status_code": 8, "status_msg": "\u7528\u6237\u672a\u767b\u5f55"}',
                            url, {"content-type": "application/json"})

        async def close(self):
            pass

    async def as_account(sec_uid):
        use_account(sec_uid)
        cookies = dict(client.cookies)
        client._transport = Expired()
        try:
            await client.request("/aweme/v1/web/aweme/post/", {}, attempts=1,
                                 page_fallback=False)
        except LoginRequired:
            pass
        return cookies

    assert client.cookies["sessionid"] == "main"          # no account named: the main one
    assert (await asyncio.create_task(as_account("B")))["sessionid"] == "b"
    assert lost == ["B"] and "login" not in notified    # only B stops; nothing is paused
    assert (await asyncio.create_task(as_account("ME")))["sessionid"] == "main"
    assert lost == ["B"] and "login" in notified        # the main login pauses the background

    async def unknown():
        use_account("GONE")
        return client.cookies

    try:
        await asyncio.create_task(unknown())
    except LoginRequired:
        pass
    else:
        raise AssertionError("an account outside the library has no login")
    # A changed library: B's session is replaced, unchanged ones are kept.
    main = client._main
    await client.sync_accounts("ME", {"sessionid": "main", "msToken": "m"},
                               {"B": {"sessionid": "b2"}})
    assert client._main is main and client._others["B"].cookies["sessionid"] == "b2"
    await client.sync_accounts("ME", {"sessionid": "main", "msToken": "m"}, {})
    assert not client._others


async def test_renewing_the_device_id_swaps_ttwid_and_reports_it(monkeypatch):
    import douyin.client as client_module
    from douyin.transport import Response

    class Registrar:
        def __init__(self, cookies):
            pass

        async def request(self, method, url, **kwargs):
            assert method == "POST" and url == client_module.TTWID_REGISTER_URL
            return Response(200, b"{}", url, {})

        def cookie(self, name):
            return "fresh" if name == "ttwid" else ""

        async def close(self):
            pass

    monkeypatch.setattr(client_module, "create_transport", Registrar)
    client = DouyinClient({"sessionid": "s", "ttwid": "scaled"})
    saved = []
    client.on_cookies_renewed = lambda account, cookies: saved.append((account, cookies))
    assert await client.renew_device_id()
    assert client.cookies["ttwid"] == "fresh"
    assert saved == [("", {"sessionid": "s", "ttwid": "fresh"})]
    assert not await client.renew_device_id()       # at most once per interval
