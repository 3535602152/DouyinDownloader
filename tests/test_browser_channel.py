from __future__ import annotations

from douyin.browser import BrowserChannel


async def test_failures_back_off_without_opening_the_browser():
    channel = BrowserChannel()
    channel._failed("detail capture")
    assert 29 < channel.cooldown_remaining() <= 30
    # While cooling down no page is launched and nothing is requested.
    assert await channel.work_detail("1") is None
    assert await channel.request_api("/aweme/v1/web/aweme/detail/", {"aweme_id": "1"}) is None
    assert channel._page is None
    channel._failed("detail capture")
    assert channel.cooldown_remaining() > 59
    channel._succeeded()
    assert channel.cooldown_remaining() == 0


def test_qr_passport_responses():
    from douyin.browser import qr_passport_update

    base = "https://login.douyin.com/passport/web/"
    qr = qr_passport_update(base + "get_qrcode/?a=1", {"data": {"error_code": 0, "qrcode": "iVBOR"}})
    assert qr == {"image": "data:image/png;base64,iVBOR"}
    for raw, status in (("new", "waiting"), ("scanned", "scanned"), ("expired", "expired")):
        update = qr_passport_update(base + "check_qrconnect/", {"data": {"status": raw}})
        assert update == {"status": status}
    refused = {"data": {"error_code": 7, "description": "需要验证"}}
    assert qr_passport_update(base + "check_qrconnect/", refused) == {"error": "需要验证"}
    assert qr_passport_update(base + "check_qrconnect/", {"message": "error"}) == {}
