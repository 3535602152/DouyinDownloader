from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from douyin.pages import Page  # noqa: E402
from engine.fetcher import FetchError  # noqa: E402
from storage.db import Database  # noqa: E402


def video_payload(aweme_id: str = "7000000000000000001", *, sec_uid: str = "SEC_A",
                  nickname: str = "作者A", create_time: int = 1700000000, desc: str = "作品",
                  size: int = 1000, pinned: bool = False) -> Dict[str, Any]:
    return {
        "aweme_id": aweme_id, "desc": desc, "create_time": create_time, "aweme_type": 0,
        "is_top": int(pinned),
        "author": {"sec_uid": sec_uid, "nickname": nickname, "uid": "42",
                   "avatar_thumb": {"url_list": ["https://p3.example/avatar.jpg"]}},
        "statistics": {"digg_count": 5},
        "video": {
            "play_addr": {"uri": "v0", "url_list": [f"https://v3.douyinvod.com/{aweme_id}.mp4"],
                          "data_size": size},
            "cover": {"url_list": [f"https://p3.example/{aweme_id}_cover.jpeg"]},
            "duration": 12000,
        },
        "music": {"id": 1, "title": "原声", "play_url": {"url_list": ["https://m.example/a.mp3"]}},
    }


def gallery_payload(aweme_id: str = "7000000000000000002", *, sec_uid: str = "SEC_A",
                    images: int = 2, live_at: Tuple[int, ...] = (1,)) -> Dict[str, Any]:
    items = []
    for index in range(images):
        item = {"url_list": [f"https://p3.example/{aweme_id}_{index}.jpeg"],
                "width": 1080, "height": 1440}
        if index in live_at:
            item["video"] = {"play_addr": {"url_list": [f"https://v.example/{aweme_id}_{index}.mp4"]}}
        items.append(item)
    return {
        "aweme_id": aweme_id, "desc": "图文", "create_time": 1700000100, "aweme_type": 68,
        "author": {"sec_uid": sec_uid, "nickname": "作者A", "uid": "42"},
        "images": items,
        "music": {"id": 2, "play_url": {"url_list": ["https://m.example/g.mp3"]}},
    }


class _NoSlot:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """The parts of DouyinClient the engine and services use."""

    user_agent = "UA"

    def __init__(self):
        self.details: Dict[str, Any] = {}
        self.pages: Dict[str, List[Page]] = {}
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, Any]] = []
        self.cooldown = 0.0
        self.renewable = False

    def slot(self):
        return _NoSlot()

    async def sync_accounts(self, main_sec_uid, main_cookies, others):
        self.accounts = {"main": main_sec_uid, "others": dict(others)}

    async def profile_with(self, cookies):
        return self.profiles.get("login") or self.profiles.get("self")

    async def renew_device_id(self) -> bool:
        self.calls.append(("renew_device_id", None))
        return self.renewable

    def media_headers(self, user_agent: str = "") -> Dict[str, str]:
        return {"User-Agent": user_agent or self.user_agent}

    def sign_play_candidate(self, url: str):
        return url + "&X-Bogus=1", self.media_headers()

    def play_url(self, video_id: str, ratio: str):
        return f"https://www.douyin.com/aweme/v1/play/?video_id={video_id}&ratio={ratio}", {}

    def detail_cooldown(self) -> float:
        return self.cooldown

    async def probe_size(self, url, headers) -> int:
        return -1

    async def aweme_detail(self, aweme_id: str, *, gentle: bool = False):
        self.calls.append(("detail", aweme_id))
        value = self.details.get(aweme_id)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, tuple):
            return value
        if value is None:
            return None, "作品详情为空", "empty"
        return value, "", ""

    def _page(self, key: str, cursor: int) -> Page:
        self.calls.append((key, cursor))
        pages = self.pages.get(key) or []
        value = pages.pop(0) if pages else Page.parse({"status_code": 0, "aweme_list": [],
                                                        "has_more": 0})
        if isinstance(value, BaseException):
            raise value
        return value

    async def user_posts(self, sec_uid, cursor=0, count=18):
        return self._page(f"posts:{sec_uid}", cursor)

    async def user_likes(self, sec_uid, cursor=0, count=20):
        return self._page(f"likes:{sec_uid}", cursor)

    async def self_collection(self, cursor=0, count=20):
        return self._page("collect", cursor)

    async def user_profile(self, sec_uid):
        return self.profiles.get(sec_uid)

    async def self_profile(self):
        return self.profiles.get("self")

    async def music_detail(self, music_id):
        return None


class FakeFetcher:
    """Writes a small file for every fetch; URLs in ``fail`` raise."""

    def __init__(self):
        self.fail: set = set()
        self.fetched: List[str] = []

    async def fetch(self, candidates, dest: Path, *, segmented=True, image_suffix=False):
        for url, _headers in candidates:
            self.fetched.append(url)
            if any(marker in url for marker in self.fail):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff data " + url.encode())
            return dest
        raise FetchError(f"{dest.name}: all failed", [403])

    def close(self):
        pass


def page_of(items, *, has_more=False, cursor=0) -> Page:
    return Page.parse({"status_code": 0, "aweme_list": list(items), "has_more": int(has_more),
                       "max_cursor": cursor})


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "app.db")
    yield database
    database.close()
