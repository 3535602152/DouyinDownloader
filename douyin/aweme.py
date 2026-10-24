"""Reading Douyin work ("aweme") payloads.

Pure functions only: media type, the ranked download candidates for videos,
gallery pictures, live photos, covers and soundtracks, the slim descriptive
subset worth storing, and small display helpers.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from douyin.pages import to_int, url_list

GALLERY_AWEME_TYPES = {2, 68, 150}
PLAY_ADDR_KEYS = ("play_addr_h264", "play_addr_265", "play_addr_256", "play_addr")
QUALITY_TARGETS = {"1440p": 2560, "1080p": 1920, "720p": 1280, "540p": 960,
                   "480p": 854, "360p": 640}
QUALITY_VALUES = ("original", "highest", "lowest", *QUALITY_TARGETS)
_SCALED_IMAGE = "tplv-dy-aweme-images-v2:"
_WATERMARK_HINTS = ("tplv-dy-water", "dy-water", "owner_watermark", "watermark_image",
                    "watermark=1", "playwm")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

Candidate = Tuple[str, Dict[str, str]]


# ── identity ────────────────────────────────────────────────────────


def aweme_id(payload: Any) -> str:
    return str(payload.get("aweme_id") or "").strip() if isinstance(payload, Mapping) else ""


def author(payload: Any) -> Dict[str, Any]:
    value = payload.get("author") if isinstance(payload, Mapping) else None
    return value if isinstance(value, dict) else {}


def author_sec_uid(payload: Any) -> str:
    value = author(payload)
    return str(value.get("sec_uid") or value.get("sec_uid_v2")
               or (payload.get("author_sec_uid") if isinstance(payload, Mapping) else "")
               or "").strip()


def belongs_to(payload: Any, sec_uid: str) -> bool:
    """True when the payload's embedded author is ``sec_uid``.

    Profile lists occasionally include related/recommended works; those
    never enter a creator's archive.
    """
    owner = author_sec_uid(payload)
    return bool(owner) and owner == str(sec_uid or "").strip()


def extract(item: Any) -> Optional[Dict[str, Any]]:
    """The aweme payload inside a list row (some lists wrap it)."""
    if not isinstance(item, dict):
        return None
    if item.get("aweme_id"):
        return item
    for key in ("aweme", "aweme_info", "aweme_detail"):
        value = item.get(key)
        if isinstance(value, dict) and value.get("aweme_id"):
            return value
    return None


PIN_KEYS = ("is_top", "is_pinned", "is_pin", "top")


def has_pin_state(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in PIN_KEYS)


def is_pinned(payload: Mapping[str, Any]) -> bool:
    for key in PIN_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
        elif value:
            return True
    return False


def title(payload: Mapping[str, Any]) -> str:
    return str(payload.get("desc") or payload.get("title") or "").strip()


def create_time(payload: Mapping[str, Any]) -> int:
    return to_int(payload.get("create_time"))


def publish_date(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d") if timestamp else ""
    except (OSError, OverflowError, ValueError):
        return ""


def stat(payload: Mapping[str, Any], key: str, *aliases: str) -> int:
    stats = payload.get("statistics") or payload.get("stats") or {}
    for source in (stats if isinstance(stats, dict) else {}, payload):
        for name in (key, *aliases):
            value = to_int(source.get(name))
            if value:
                return value
    return 0


def hashtags(payload: Mapping[str, Any]) -> List[str]:
    tags: List[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip().lstrip("#")
        if text and text not in tags:
            tags.append(text)

    for item in payload.get("text_extra") or []:
        if isinstance(item, dict):
            add(item.get("hashtag_name"))
    for item in payload.get("cha_list") or []:
        if isinstance(item, dict):
            add(item.get("cha_name"))
    for value in re.findall(r"#([^\s#]+)", str(payload.get("desc") or "")):
        add(value)
    return tags


# ── media type ──────────────────────────────────────────────────────


def gallery_items(payload: Mapping[str, Any]) -> List[Any]:
    info = payload.get("image_post_info")
    if isinstance(info, dict):
        for key in ("images", "image_list"):
            value = info.get(key)
            if isinstance(value, list) and value:
                return value
    value = payload.get("images") or payload.get("image_list") or []
    return value if isinstance(value, list) else []


def _video(payload: Mapping[str, Any]) -> Dict[str, Any]:
    value = payload.get("video")
    return value if isinstance(value, dict) else {}


def has_video_source(video: Mapping[str, Any]) -> bool:
    if preferred_play_addr(video):
        return True
    download = video.get("download_addr")
    return bool(video.get("vid") or (isinstance(download, dict) and download.get("uri")))


def media_type(payload: Mapping[str, Any]) -> str:
    """``gallery`` or ``video`` (a gallery-typed note with a real video is a video)."""
    if gallery_items(payload):
        return "gallery"
    kind = payload.get("aweme_type")
    if isinstance(kind, int) and kind in GALLERY_AWEME_TYPES:
        return "video" if has_video_source(_video(payload)) else "gallery"
    if str(payload.get("media_type") or "").lower() in {"gallery", "image", "note"}:
        return "gallery"
    if to_int(payload.get("image_count")) > 0:
        return "gallery"
    return "video"


def has_downloadable_media(payload: Mapping[str, Any]) -> bool:
    if media_type(payload) == "gallery":
        return bool(gallery_assets(payload))
    video = _video(payload)
    if any(url_list(video.get(key)) for key in PLAY_ADDR_KEYS + ("download_addr",)):
        return True
    return has_video_source(video)


def is_complete(payload: Any) -> bool:
    """Enough identity/author/media data to download and archive the work."""
    if not isinstance(payload, dict) or "desc" not in payload or create_time(payload) <= 0:
        return False
    owner = author(payload)
    if not (owner.get("uid") or owner.get("sec_uid") or owner.get("nickname")):
        return False
    return has_downloadable_media(payload)


def merge_detail(detail: Dict[str, Any], fallback: Mapping[str, Any]) -> Dict[str, Any]:
    """Fresh detail wins; the fallback only fills fields the detail omitted.

    Media fields are never copied back: a listing's signed URLs are the part
    most likely to have expired.
    """
    media_keys = {"video", "images", "image_list", "image_post_info", "image_post"}
    merged = dict(detail)
    for key, value in (fallback or {}).items():
        if key in media_keys:
            continue
        current = merged.get(key)
        if key not in merged or current in (None, "", [], {}):
            merged[key] = value
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_detail(current, value)
    return merged


# ── video candidates ────────────────────────────────────────────────


def _resolution(entry: Mapping[str, Any], play_addr: Mapping[str, Any]) -> Tuple[int, int]:
    width = to_int(play_addr.get("width") or entry.get("width"))
    height = to_int(play_addr.get("height") or entry.get("height"))
    if width > 0 and height > 0:
        return min(width, height), width * height
    long_edge = max(width, height)
    if long_edge <= 0:
        return 0, 0
    nearest = min(QUALITY_TARGETS.items(), key=lambda item: abs(item[1] - long_edge))[0]
    short = int(nearest[:-1])
    return short, long_edge * short


def play_addr_by_quality(video: Mapping[str, Any], quality: str = "highest") -> Optional[Dict]:
    rates = video.get("bit_rate") if isinstance(video, Mapping) else None
    if not isinstance(rates, list):
        return None
    entries = []
    for entry in rates:
        if isinstance(entry, dict) and isinstance(entry.get("play_addr"), dict):
            addr = entry["play_addr"]
            short, pixels = _resolution(entry, addr)
            entries.append((to_int(entry.get("bit_rate")), short, pixels, addr))
    if not entries:
        return None
    wanted = str(quality or "highest").strip().lower()
    if wanted == "lowest":
        entries.sort(key=lambda item: (item[0], item[2]))
    elif wanted in QUALITY_TARGETS:
        target = int(wanted[:-1])
        entries.sort(key=lambda item: (abs(item[1] - target), -item[0], -item[2]))
    else:
        entries.sort(key=lambda item: (-item[2], -item[0], -item[1]))
    return entries[0][3]


def preferred_play_addr(video: Mapping[str, Any], quality: str = "highest") -> Optional[Dict]:
    chosen = play_addr_by_quality(video, quality)
    if chosen:
        return chosen
    if not isinstance(video, Mapping):
        return None
    primary = video.get("play_addr")
    if isinstance(primary, dict) and primary.get("uri"):
        return primary
    for key in PLAY_ADDR_KEYS:
        candidate = video.get(key)
        if isinstance(candidate, dict) and (url_list(candidate) or candidate.get("uri")):
            return candidate
    return None


def is_watermarked(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(hint in lowered for hint in _WATERMARK_HINTS)


def video_candidates(payload: Mapping[str, Any], client: Any, quality: str = "highest"
                     ) -> List[Candidate]:
    """Ranked video URLs: direct CDN mirrors, then the signed play endpoint.

    Direct mirrors (``douyinvod.com`` …) come first because the play endpoint
    may redirect to an unreachable PCDN node.  Watermarked URLs are a last
    resort.  Without any usable URL, a play URL is built from the video URI.
    """
    video = _video(payload)
    addr = preferred_play_addr(video, quality) or {}
    headers = client.media_headers()
    direct: List[Candidate] = []
    endpoints: List[str] = []
    marked: List[Candidate] = []
    lists = [addr, video.get("download_addr")]
    for source in lists:
        urls = sorted(url_list(source), key=lambda url: 0 if "watermark=0" in url else 1)
        endpoint_taken = False
        for url in urls:
            watermarked = is_watermarked(url)
            if urlparse(url).netloc.endswith("douyin.com"):
                if watermarked:
                    marked.append(client.sign_play_candidate(url))
                elif not endpoint_taken:
                    endpoints.append(url)
                    endpoint_taken = True
            elif watermarked:
                marked.append((url, headers))
            else:
                direct.append((url, headers))
    result: List[Candidate] = []
    seen = set()
    for candidate in (*direct, *(client.sign_play_candidate(url) for url in endpoints)):
        if candidate[0] not in seen:
            seen.add(candidate[0])
            result.append(candidate)
    if not result:
        uri = addr.get("uri") or video.get("vid") or (video.get("download_addr") or {}).get("uri")
        if uri:
            entry = next((e for e in video.get("bit_rate") or ()
                          if isinstance(e, dict) and e.get("play_addr") is addr), {})
            short, _ = _resolution(entry, addr)
            ratio = f"{short}p" if f"{short}p" in QUALITY_TARGETS else {
                "highest": "1080p", "lowest": "540p"}.get(quality, quality
                                                         if quality in QUALITY_TARGETS
                                                         else "1080p")
            result.append(client.play_url(str(uri), ratio))
    for candidate in marked[:1]:
        if candidate[0] not in seen:
            result.append(candidate)
    return result


def original_video_candidate(video: Mapping[str, Any], client: Any, quality: str = "highest"
                             ) -> Optional[Candidate]:
    """The ``ratio=default`` (original upload) URL for a video, unprobed."""
    addr = preferred_play_addr(video, quality) or {}
    uri = addr.get("uri") or video.get("vid") or (video.get("download_addr") or {}).get("uri")
    return client.play_url(str(uri), "default") if uri else None


def estimated_video_bytes(payload: Mapping[str, Any], quality: str = "highest") -> int:
    if media_type(payload) != "video":
        return 0
    video = _video(payload)
    chosen = preferred_play_addr(video, quality) or {}
    sizes = [chosen.get("data_size")] + [
        (entry.get("play_addr") or {}).get("data_size")
        for entry in video.get("bit_rate") or () if isinstance(entry, dict)
    ]
    return next((to_int(size) for size in sizes if to_int(size) > 0), 0)


# ── gallery ─────────────────────────────────────────────────────────


def _pixels(source: Any) -> int:
    if not isinstance(source, dict):
        return 0
    width = to_int(source.get("width") or source.get("w"))
    height = to_int(source.get("height") or source.get("h"))
    return width * height if width and height else (width or height)


def ranked_image_urls(item: Mapping[str, Any]) -> List[str]:
    """One gallery picture's URLs, watermark-free and largest first."""
    entries = []
    sources = (
        (item.get("watermark_free_download_url_list"), item, 0),
        (item.get("origin_image"), item.get("origin_image"), 1),
        (item.get("display_image"), item.get("display_image"), 2),
        (item, item, 3),
        (item.get("download_url"), item.get("download_url"), 4),
        (item.get("download_addr"), item.get("download_addr"), 5),
        (item.get("download_url_list"), item, 6),
        (item.get("owner_watermark_image"), item.get("owner_watermark_image"), 7),
    )
    for source, meta, rank in sources:
        for url in url_list(source):
            marked = 1 if rank >= 4 or is_watermarked(url) else 0
            webp = 1 if ".webp" in (urlparse(url).path or "").lower() else 0
            entries.append(((marked, -_pixels(meta), rank, webp), url))
    urls: List[str] = []
    for _key, url in sorted(entries, key=lambda entry: entry[0]):
        if url not in urls:
            urls.append(url)
    return urls


def scaled_gallery(payload: Mapping[str, Any]) -> bool:
    """Whether a gallery's clean pictures are the scaled ``-v2`` copies.

    Douyin serves them to some ``ttwid`` buckets (see
    ``DouyinClient.renew_device_id``); others get the full-size clean ones.
    """
    return any(_SCALED_IMAGE in url for item in gallery_items(payload) if isinstance(item, dict)
               for url in url_list(item.get("url_list")))


def original_image_urls(payload: Mapping[str, Any], item: Mapping[str, Any]) -> List[str]:
    """The untouched original (``~noop``) of a gallery picture, when listed.

    Since 2026-09-30 the clean ``url_list`` copies are scaled down (e.g.
    1440x1920 for a 2160x2880 upload); full size comes only watermarked.  A
    gallery's cover is its first picture's original, signed for that one
    URL, so that picture can still be saved full size without a watermark.
    """
    uri = str(item.get("uri") or "").strip("/")
    if not uri:
        return []
    video = _video(payload)
    return [url for key in ("origin_cover", "cover") for url in url_list(video.get(key))
            if f"/{uri}~noop." in url and not is_watermarked(url)]


def _live_urls(item: Mapping[str, Any], quality: str) -> List[str]:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    urls: List[str] = []
    for source in (preferred_play_addr(video, quality), video.get("play_addr_h264"),
                   video.get("play_addr_265"), video.get("play_addr_256"),
                   video.get("play_addr"), video.get("download_addr"),
                   item.get("video_play_addr"), item.get("video_download_addr")):
        for url in url_list(source):
            if url not in urls:
                urls.append(url)
    return urls


def gallery_assets(payload: Mapping[str, Any], quality: str = "highest"
                   ) -> List[Tuple[int, List[str], List[str]]]:
    """Per gallery position: ``(index, still image urls, live clip urls)``.

    Positions with neither a picture nor a clip are skipped.
    """
    assets = []
    for index, item in enumerate(gallery_items(payload)):
        if not isinstance(item, dict):
            continue
        stills = list(dict.fromkeys(original_image_urls(payload, item) + ranked_image_urls(item)))
        clips = _live_urls(item, quality)
        if stills or clips:
            assets.append((index, stills, clips))
    return assets


def image_ext(url: str) -> str:
    path = (urlparse(str(url or "")).path or "").lower()
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_EXTS:
        return suffix
    matches = re.findall(r"\.(?:jpe?g|png|webp|gif)(?=[^a-z0-9]|$)", path)
    return matches[-1].lower() if matches else ".jpg"


# ── sidecars ────────────────────────────────────────────────────────


def _urls_of(payload: Any, keys: Tuple[str, ...]) -> List[str]:
    if not isinstance(payload, Mapping):
        return []
    return list(dict.fromkeys(url for key in keys for url in url_list(payload.get(key))))


def cover_urls(payload: Mapping[str, Any], quality: str = "highest") -> List[str]:
    # ``cover`` from the detail API is a 4:3 landscape cut of the author's
    # cover; ``cover_original_scale`` is that cover at the video's own aspect,
    # the image Douyin's web grid centre-crops to 3:4. ``origin_cover`` is the
    # first frame, so it stays behind both.
    keys = ("cover", "origin_cover", "dynamic_cover")
    if media_type(payload) != "gallery":
        keys = ("cover_original_scale", *keys)
    urls = (_urls_of(_video(payload), keys)
            + _urls_of(payload, ("cover",))
            + _urls_of(payload.get("image_post_info"), ("cover",)))
    if media_type(payload) == "gallery":
        urls += next((stills for _i, stills, _c in gallery_assets(payload, quality) if stills), [])
    return list(dict.fromkeys(urls))


def avatar_urls(user: Any) -> List[str]:
    return _urls_of(user, ("avatar_larger", "avatar_medium", "avatar_thumb"))


def music_urls(payload: Mapping[str, Any]) -> List[str]:
    """The soundtrack: direct music URLs plus the original song, if exposed."""
    music = payload.get("music") if isinstance(payload.get("music"), dict) else {}
    urls = _urls_of(music, ("play_url", "play_url_lowbr", "play_addr", "url"))
    extra = music.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if isinstance(extra, dict):
        original = str(extra.get("original_song_url") or "").strip()
        if original.startswith(("http://", "https://")):
            urls.append(original)
        uri = str(extra.get("original_song_uri") or "").strip().lstrip("/")
        if uri and media_type(payload) == "gallery":
            for url in _urls_of(_video(payload), ("play_addr", "play_addr_h264",
                                                  "play_addr_265", "download_addr")):
                if urlparse(url).path.lstrip("/") == uri:
                    urls.append(url)
    return list(dict.fromkeys(urls))


def music_info(payload: Mapping[str, Any]) -> Tuple[List[str], str, str]:
    """``(play urls, title, author)`` of the soundtrack as the web player uses it."""
    music = next((payload.get(key) for key in ("music", "music_info", "music_detail")
                  if isinstance(payload.get(key), dict)), {})
    sources: List[str] = []
    for candidate in (music.get("play_url"), music.get("play_url_lowbr"),
                      music.get("play_addr"), music.get("url_list"), music.get("url"),
                      payload.get("music_url")):
        sources = [url for url in url_list(candidate) if url.startswith(("http", "data:"))]
        if sources:
            break
    name = str(music.get("music_name") or music.get("title") or music.get("name")
               or payload.get("music_name") or "").strip()
    owner = music.get("author") or music.get("owner") or music.get("artist")
    if isinstance(owner, dict):
        owner = owner.get("nickname") or owner.get("name") or owner.get("unique_id")
    return list(dict.fromkeys(sources)), name, str(owner or payload.get("music_author") or "")


def music_resource_key(payload: Mapping[str, Any]) -> str:
    music = payload.get("music") if isinstance(payload.get("music"), dict) else {}
    for key in ("id", "mid", "music_id", "uri"):
        value = str(music.get(key) or "").strip()
        if value:
            return value
    urls = music_urls(payload)
    return urls[0] if urls else ""


# ── storage ─────────────────────────────────────────────────────────

_TOP_KEYS = ("aweme_id", "desc", "create_time", "aweme_type", "media_type", "share_url",
             "is_top", "preview_title", "duration", "region", "caption")
_AUTHOR_KEYS = ("uid", "sec_uid", "short_id", "unique_id", "nickname", "signature")
_MUSIC_KEYS = ("id", "id_str", "mid", "title", "author", "album", "duration", "owner_nickname")
_STAT_KEYS = ("digg_count", "comment_count", "share_count", "collect_count", "play_count")


def slim(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The descriptive subset worth keeping in the database (no signed URLs).

    The complete payload is saved next to the media as ``*_data.json``.
    """
    if not isinstance(payload, Mapping):
        return {}
    result: Dict[str, Any] = {key: payload[key] for key in _TOP_KEYS if key in payload}
    if has_pin_state(payload):
        result["is_top"] = int(is_pinned(payload))
    owner = author(payload)
    if owner:
        result["author"] = {key: owner[key] for key in _AUTHOR_KEYS if key in owner}
        avatar = url_list(owner.get("avatar_thumb"))[:1]
        if avatar:
            result["author"]["avatar_thumb"] = {"url_list": avatar}
    music = payload.get("music")
    if isinstance(music, Mapping):
        result["music"] = {key: music[key] for key in _MUSIC_KEYS if key in music}
    stats = payload.get("statistics")
    if isinstance(stats, Mapping):
        result["statistics"] = {key: stats[key] for key in _STAT_KEYS if key in stats}
    video = _video(payload)
    slim_video = {key: video[key] for key in ("duration", "width", "height") if key in video}
    cover = url_list(video.get("cover"))[:2]
    if cover:
        slim_video["cover"] = {"url_list": cover}
    if slim_video:
        result["video"] = slim_video
    images = gallery_items(payload)
    if images:
        result["image_count"] = len(images)
    elif "image_count" in payload:
        result["image_count"] = payload["image_count"]
    tags = [str(tag.get("hashtag_name")) for tag in payload.get("text_extra") or ()
            if isinstance(tag, Mapping) and tag.get("hashtag_name")]
    if tags:
        result["hashtags"] = tags
    elif isinstance(payload.get("hashtags"), list):
        result["hashtags"] = list(payload["hashtags"])
    return result


def slim_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(slim(payload), ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "{}"


def decode(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value or "") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ── display ─────────────────────────────────────────────────────────

MEDIA_TYPE_LABELS = {"video": "视频", "gallery": "图文", "music": "音乐"}


def format_timestamp(value: Any) -> str:
    stamp = to_int(value)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp > 0 else "未知"


def format_duration(value: Any) -> str:
    ms = to_int(value)
    if ms <= 0:
        return "未知"
    seconds = max(1, round(ms / 1000))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def collection_name(payload: Mapping[str, Any]) -> str:
    info = payload.get("mix_info")
    name = ""
    if isinstance(info, dict):
        name = str(info.get("mix_name") or info.get("name") or "").strip()
    return name or str(payload.get("_collection_name") or "").strip()


def preview_row(payload: Mapping[str, Any], source_type: str, author_name: str = "") -> Dict:
    """One row of the link page's result table."""
    kind = "music" if str(payload.get("media_type") or "") == "music" else media_type(payload)
    label = MEDIA_TYPE_LABELS.get(kind, kind)
    owner = author(payload)
    return {
        "aweme_id": aweme_id(payload),
        "title": title(payload) or "无标题",
        "publish_time": format_timestamp(payload.get("create_time")),
        "duration": format_duration(_video(payload).get("duration")) if kind == "video" else "—",
        "media_type": label,
        "author_name": author_name or str(owner.get("nickname") or "unknown"),
        "author_sec_uid": str(owner.get("sec_uid") or ""),
        "size_text": "—",
        "source_type": source_type,
        "collection_name": collection_name(payload),
    }
