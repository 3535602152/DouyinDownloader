"""Recognize Douyin links: share text, short links, works, profiles…"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

SHORT_HOSTS = ("v.douyin.com", "v.iesdouyin.com", "iesdouyin.com")
_URL_IN_TEXT = re.compile(r"https?://[^\s　，。；！？、（）【】《》“”‘’]+", re.IGNORECASE)
_SEC_UID = re.compile(r"^[A-Za-z0-9_-]+$")

TYPE_LABELS = {
    "video": "单个视频",
    "gallery": "单个图文",
    "user": "用户主页",
    "collection": "合集",
    "music": "音乐",
    "live": "直播（暂不支持）",
    "live_replay": "直播回放（暂不支持）",
    "short": "短链接",
}


@dataclass(frozen=True)
class Link:
    """One classified link. ``kind`` is a key of :data:`TYPE_LABELS`."""

    kind: str
    url: str
    aweme_id: str = ""
    sec_uid: str = ""
    mix_id: str = ""
    music_id: str = ""

    @property
    def label(self) -> str:
        return TYPE_LABELS.get(self.kind, "无法识别")


def extract_url(text: str) -> Optional[str]:
    """The first http(s) URL in pasted share text."""
    match = _URL_IN_TEXT.search(str(text or ""))
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)]}>\"'）】》") or None


def is_short(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    for scheme in ("https://", "http://"):
        if lowered.startswith(scheme):
            lowered = lowered[len(scheme):]
            break
    return any(lowered == host or lowered.startswith(host + "/") for host in SHORT_HOSTS)


def normalize_short(url: str) -> str:
    text = str(url or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else f"https://{text}"


def _douyin_host(host: str) -> bool:
    return any(host == base or host.endswith("." + base) for base in ("douyin.com", "iesdouyin.com"))


def classify(url: str) -> Optional[Link]:
    """Classify an already-expanded URL; ``None`` when it is not a Douyin link."""
    text = str(url or "").strip()
    if is_short(text):
        return Link("short", text)
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if (_douyin_host(host) and path.startswith("/vsdetail/")) or (
        host == "webcast.amemv.com" and path.startswith("/douyin/webcast/reflow/episode/")
    ):
        return Link("live_replay", text)
    if not _douyin_host(host):
        return None
    modal = (parse_qs(parsed.query).get("modal_id") or [""])[0].strip()
    if modal.isdigit():
        return Link("video", text, aweme_id=modal)
    if host == "live.douyin.com":
        return Link("live", text)
    match = re.search(r"/video/(\d+)", path)
    if match:
        return Link("video", text, aweme_id=match.group(1))
    match = re.search(r"/user/([A-Za-z0-9_-]+)", path)
    if match:
        return Link("user", text, sec_uid=match.group(1))
    match = re.search(r"/(?:note|gallery|slides)/(\d+)", path)
    if match:
        return Link("gallery", text, aweme_id=match.group(1))
    match = re.search(r"/(?:collection|mix)/(\d+)", path)
    if match:
        return Link("collection", text, mix_id=match.group(1))
    match = re.search(r"/music/(\d+)", path)
    if match:
        return Link("music", text, music_id=match.group(1))
    if "/live/" in path:
        return Link("live", text)
    return None


def aweme_id_in(text: str) -> str:
    """A work ID from a bare ID, a work/modal URL or a ``vid=`` query."""
    raw = str(text or "").strip()
    if raw.isdigit():
        return raw
    link = classify(raw)
    if link and link.aweme_id:
        return link.aweme_id
    try:
        query = parse_qs(urlparse(raw).query)
    except ValueError:
        return ""
    for key in ("vid", "aweme_id", "item_id"):
        value = str((query.get(key) or [""])[0]).strip()
        if value.isdigit():
            return value
    return ""


def is_sec_uid(value: str) -> bool:
    return bool(_SEC_UID.fullmatch(str(value or "")))


def user_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{sec_uid}"
