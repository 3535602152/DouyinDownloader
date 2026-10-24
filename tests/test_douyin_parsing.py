from __future__ import annotations

import pytest

from douyin import aweme as aw
from douyin import cookies, urls
from douyin.errors import BadResponse, HttpError, PaginationError, RiskControl, is_risk
from douyin.pages import CursorGuard, Page
from tests.conftest import FakeClient, gallery_payload, video_payload


@pytest.mark.parametrize("text, kind, key, value", [
    ("https://www.douyin.com/video/123", "video", "aweme_id", "123"),
    ("https://www.douyin.com/user/MS4abc?x=1", "user", "sec_uid", "MS4abc"),
    ("https://www.douyin.com/note/456", "gallery", "aweme_id", "456"),
    ("https://www.douyin.com/collection/789", "collection", "mix_id", "789"),
    ("https://www.douyin.com/music/55", "music", "music_id", "55"),
    ("https://www.douyin.com/user/X?modal_id=999", "video", "aweme_id", "999"),
    ("https://live.douyin.com/12", "live", "url", "https://live.douyin.com/12"),
])
def test_classify(text, kind, key, value):
    link = urls.classify(text)
    assert link.kind == kind
    assert getattr(link, key) == value


def test_share_text_and_short_links():
    text = "8.88 复制打开抖音 https://v.douyin.com/abc/ 看看，"
    assert urls.extract_url(text) == "https://v.douyin.com/abc/"
    assert urls.is_short("v.douyin.com/abc")
    assert urls.normalize_short("v.douyin.com/abc") == "https://v.douyin.com/abc"
    assert urls.classify("https://example.com/video/1") is None
    assert urls.aweme_id_in("https://www.douyin.com/?vid=77") == "77"


def test_cookie_login_state():
    assert cookies.login_state({"ttwid": "x"}) == "invalid"
    assert cookies.login_state({"sessionid": "s"}) == "ok"
    assert cookies.login_state({}) == "empty"
    assert cookies.parse_header("a=1; b=2; bad name=3") == {"a": "1", "b": "2"}


def test_page_validation():
    page = Page.parse({"status_code": 0, "aweme_list": [{"aweme_id": "1"}], "has_more": 1,
                       "max_cursor": 5})
    assert page.ensure_valid() is page and page.cursor == 5
    with pytest.raises(RiskControl):
        Page.parse({}).ensure_valid()
    with pytest.raises(RiskControl):
        Page.parse({"status_code": 0, "verify_ticket": "t"}).ensure_valid()
    with pytest.raises(BadResponse):
        Page.parse({"status_code": 8, "aweme_list": []}).ensure_valid()
    with pytest.raises(PaginationError):
        Page.parse({"status_code": 0, "aweme_list": [], "has_more": 1}).ensure_valid()
    guard = CursorGuard()
    assert guard.advance(10) == 10
    with pytest.raises(PaginationError):
        guard.advance(10)


def test_risk_classification():
    assert is_risk(HttpError(403, "/x"))
    assert is_risk(RiskControl("v"))
    assert not is_risk(HttpError(500, "/x"))
    assert HttpError(403, "/x", body="Sign Invalid").signature_rejected


def test_media_type_and_completeness():
    assert aw.media_type(video_payload()) == "video"
    assert aw.media_type(gallery_payload()) == "gallery"
    assert aw.media_type({"aweme_type": 68, "video": {}}) == "gallery"
    assert aw.is_complete(video_payload())
    assert not aw.is_complete({"aweme_id": "1"})


def test_video_candidates_prefer_direct_mirrors():
    payload = video_payload()
    payload["video"]["play_addr"]["url_list"] = [
        "https://www.douyin.com/aweme/v1/play/?video_id=v0&watermark=0",
        "https://v3.douyinvod.com/a.mp4",
        "https://v9.douyinvod.com/a.mp4?watermark=1",
    ]
    urls_ = [url for url, _ in aw.video_candidates(payload, FakeClient())]
    assert urls_[0] == "https://v3.douyinvod.com/a.mp4"
    assert urls_[1].endswith("X-Bogus=1")
    assert urls_[-1].endswith("watermark=1")


def test_video_quality_selection():
    video = {"bit_rate": [
        {"bit_rate": 100, "play_addr": {"width": 720, "height": 1280, "url_list": ["a"]}},
        {"bit_rate": 300, "play_addr": {"width": 1080, "height": 1920, "url_list": ["b"]}},
    ]}
    assert aw.play_addr_by_quality(video, "highest")["url_list"] == ["b"]
    assert aw.play_addr_by_quality(video, "720p")["url_list"] == ["a"]
    assert aw.play_addr_by_quality(video, "lowest")["url_list"] == ["a"]


def test_gallery_assets_and_sidecars():
    payload = gallery_payload(images=3, live_at=(0, 2))
    assets = aw.gallery_assets(payload)
    assert [index for index, _stills, _clips in assets] == [0, 1, 2]
    assert [bool(clips) for _i, _s, clips in assets] == [True, False, True]
    assert aw.cover_urls(payload)[0].endswith("_0.jpeg")
    assert aw.music_urls(payload) == ["https://m.example/g.mp3"]
    assert aw.image_ext("https://x/y.webp?z=1") == ".webp"


def test_video_cover_prefers_the_original_scale_cover():
    payload = video_payload("1")
    payload["video"]["cover"] = {"url_list": ["https://p3.example/cut~tplv-dy-resize-origshort-autoq-75:330.jpeg"]}
    payload["video"]["cover_original_scale"] = {"url_list": ["https://p3.example/1~tplv-dy-360p.jpeg"]}
    payload["video"]["origin_cover"] = {"url_list": ["https://p3.example/1_first_frame.jpeg"]}
    assert aw.cover_urls(payload) == ["https://p3.example/1~tplv-dy-360p.jpeg",
                                      "https://p3.example/cut~tplv-dy-resize-origshort-autoq-75:330.jpeg",
                                      "https://p3.example/1_first_frame.jpeg"]


def test_slim_drops_signed_media_urls():
    slim = aw.slim(video_payload())
    assert "play_addr" not in str(slim)
    assert slim["author"]["sec_uid"] == "SEC_A"
    assert slim["video"]["cover"]["url_list"]


def test_merge_detail_never_restores_stale_media():
    detail = {"aweme_id": "1", "desc": "", "video": {"play_addr": {"url_list": ["fresh"]}}}
    merged = aw.merge_detail(detail, {"desc": "旧标题", "video": {"play_addr": {"url_list": ["old"]}}})
    assert merged["desc"] == "旧标题"
    assert merged["video"]["play_addr"]["url_list"] == ["fresh"]


def test_belongs_to_rejects_foreign_works():
    assert aw.belongs_to(video_payload(sec_uid="A"), "A")
    assert not aw.belongs_to(video_payload(sec_uid="B"), "A")
    assert not aw.belongs_to({"aweme_id": "1"}, "A")


def test_gallery_pictures_shown_online_skip_the_watermarked_copy():
    from services.views import remote_media

    host = "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/abc~"
    item = {"width": 2160, "height": 2880,
            "url_list": [host + "tplv-dy-aweme-images:q75.webp"],
            "download_url_list": [host + "tplv-dy-water-v2:5oqW6Z-z5Y-3:2160:2880.webp"]}
    kind, sources, cover, _clips = remote_media({"aweme_id": "1", "images": [item]})
    assert kind == "image" and "dy-water" not in sources[0] and cover == sources[0]


def test_a_gallery_picture_prefers_its_original_from_the_cover():
    uri = "tos-cn-i-0813c000-ce/oPIC1"
    original = f"https://p3-pc-sign.douyinpic.com/{uri}~noop.jpeg?x-signature=a%3D"
    scaled = f"https://p3-pc-sign.douyinpic.com/{uri}~tplv-dy-aweme-images-v2:1440:1920:q75.jpeg"
    marked = f"https://p3-pc-sign.douyinpic.com/{uri}~tplv-dy-water-v10:eA==:3000:3000:q75.jpeg"
    payload = {
        "aweme_type": 68,
        "video": {"cover": {"url_list": [original]}, "origin_cover": {"url_list": [original]}},
        "images": [{"uri": uri, "url_list": [scaled], "download_url_list": [marked]},
                   {"uri": "tos-cn-i-0813c000-ce/oPIC2",
                    "url_list": [scaled.replace("oPIC1", "oPIC2")]}],
    }
    (_i, first, _c), (_j, second, _d) = aw.gallery_assets(payload)
    assert first == [original, scaled, marked]
    assert second == [scaled.replace("oPIC1", "oPIC2")]
