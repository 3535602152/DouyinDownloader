"""Work and creator views for the homepage/player UI.

Local views come from the database (work row + recorded files, already
classified by kind and gallery position); remote views from a listing or
detail payload.  Paths stay paths here — the desktop bridge turns them into
URLs the WebView can load.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from common.fs import nonempty_file
from douyin import aweme as aw
from douyin.pages import first_url, to_int, url_list
from storage.files import IMAGE_SUFFIXES, VIDEO_SUFFIXES, classify_file

LEDGER_TO_UI = {"done": "success", "retry": "retryable_failed", "failed": "permanent_failed",
                "pending": "pending", "active": "downloading"}
_GALLERY_FILE = re.compile(r"_(?:live_)?\d+$")


def _runtime_url(value: Any) -> str:
    urls = url_list(value)
    return urls[0] if urls else ""


def _is_image_payload(payload: Mapping[str, Any]) -> bool:
    return aw.media_type(payload) == "gallery" if payload else False


def work_kind(payload: Mapping[str, Any], media_type: str) -> str:
    share = payload.get("share_info") if isinstance(payload.get("share_info"), dict) else {}
    route = str(payload.get("share_url") or share.get("share_url") or "").lower()
    if "/article/" in route:
        return "article"
    return "gallery" if media_type == "image" else "video"


def _counts(payload: Mapping[str, Any]) -> Dict[str, int]:
    return {
        "like_count": aw.stat(payload, "digg_count", "like_count"),
        "comment_count": aw.stat(payload, "comment_count"),
        "collect_count": aw.stat(payload, "collect_count", "favorite_count"),
        "share_count": aw.stat(payload, "share_count"),
    }


def _gallery_from_folder(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add gallery files next to the recorded ones (older versions missed some)."""
    anchor = next((Path(entry["path"]) for entry in files
                   if entry["kind"] in ("media", "live", "json")), None)
    if anchor is None:
        return files
    stem = anchor.name[:-len("_data.json")] if anchor.name.endswith("_data.json") \
        else _GALLERY_FILE.sub("", anchor.stem)
    known = {entry["path"] for entry in files}
    try:
        entries = sorted(anchor.parent.iterdir())
    except OSError:
        return files
    extra = []
    for path in entries:
        if str(path) in known or not path.name.startswith(stem + "_"):
            continue
        kind, position = classify_file(path.name)
        if kind in ("media", "live") and _GALLERY_FILE.search(path.stem):
            extra.append({"path": str(path), "kind": kind, "position": position})
    return files + extra


def local_work(row: Mapping[str, Any], files: List[Dict[str, Any]], *,
               include_media: bool = True) -> Dict[str, Any]:
    """The view of a work known to the database."""
    payload = aw.decode(row.get("metadata"))
    aweme_id = str(row.get("aweme_id") or "")
    media_type = "image" if (row.get("media_type") == "gallery" or _is_image_payload(payload)) \
        else "video"
    existing = [entry for entry in files if nonempty_file(entry["path"])]
    if media_type == "image" and include_media:
        existing = [entry for entry in _gallery_from_folder(existing)
                    if nonempty_file(entry["path"])]
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for entry in existing:
        by_kind.setdefault(entry["kind"], []).append(entry)
    cover = next((entry["path"] for entry in by_kind.get("cover", [])
                  if Path(entry["path"]).suffix.lower() in IMAGE_SUFFIXES), "")
    music = next((entry["path"] for entry in by_kind.get("music", [])), "")

    media_paths: List[str] = []
    live_paths: List[str] = []
    if media_type == "image":
        stills = {int(entry["position"] or 0): entry["path"] for entry in by_kind.get("media", [])
                  if Path(entry["path"]).suffix.lower() in IMAGE_SUFFIXES}
        clips = {int(entry["position"] or 0): entry["path"] for entry in by_kind.get("live", [])}
        for position in sorted(set(stills) | set(clips)):
            media_paths.append(stills.get(position) or clips[position])
            live_paths.append(clips.get(position, ""))
    else:
        media_paths = [entry["path"] for entry in by_kind.get("media", [])
                       if Path(entry["path"]).suffix.lower() in VIDEO_SUFFIXES][:1]
    if not cover:
        cover = next((path for path in media_paths
                      if Path(path).suffix.lower() in IMAGE_SUFFIXES), "")
    status = str(row.get("status") or "")
    expected = to_int(payload.get("image_count")) if media_type == "image" else 1
    if media_paths:
        partial = media_type == "image" and expected and len(media_paths) < expected
        media_state, local_status = ("partial" if partial else "present"), "present"
    elif status == "done":
        media_state = local_status = "missing"
    else:
        media_state, local_status = "not_downloaded", "not_downloaded"
    sources, music_title, music_author = aw.music_info(payload)
    hidden = row.get("visibility") == "hidden"
    view = {
        "aweme_id": aweme_id,
        "title": str(row.get("title") or aw.title(payload) or "无标题") or "无标题",
        "create_time": int(row.get("create_time") or aw.create_time(payload) or 0),
        "media_type": media_type,
        "work_kind": work_kind(payload, media_type),
        "media_paths": media_paths if include_media else [],
        "live_paths": live_paths if include_media else [],
        "media_state": media_state,
        "music_path": music if include_media else "",
        "music_sources": sources if include_media else [],
        "music_source": sources[0] if include_media and sources else "",
        "music_title": music_title,
        "music_author": music_author,
        "cover_path": cover or (media_paths[0] if not include_media and media_paths and
                                Path(media_paths[0]).suffix.lower() in IMAGE_SUFFIXES else ""),
        "is_pinned": bool(row.get("is_pinned", aw.is_pinned(payload))),
        "local_status": local_status,
        "download_status": LEDGER_TO_UI.get(status, ""),
        "error_code": str(row.get("error_code") or ""),
        "error_message": str(row.get("error") or row.get("visibility_note") or ""),
        "offline_label": "作品已隐藏" if hidden else "",
        "offline_reason": str(row.get("visibility_note") or "") if hidden else "",
        "author_sec_uid": str(row.get("author_sec_uid") or aw.author_sec_uid(payload)),
        **_counts(payload),
    }
    if not include_media and not view["cover_path"] and media_paths:
        view["first_media_path"] = media_paths[0]
    return view


def image_url(value: Any) -> str:
    """A gallery picture shown online: the same watermark-free choice as downloads.

    ``download_url_list`` carries the "抖音号" watermark (``tplv-dy-water``);
    ``url_list`` is the clean picture.
    """
    if isinstance(value, dict):
        urls = aw.ranked_image_urls(value)
        if urls:
            return urls[0]
    return _runtime_url(value)


def remote_media(payload: Mapping[str, Any]) -> Tuple[str, List[str], str, List[str]]:
    """``(media type, sources, cover, live clip per picture)`` of a fresh payload."""
    items = aw.gallery_items(payload)
    if items or aw.media_type(payload) == "gallery":
        sources, clips = [], []
        for item in items:
            source = image_url(item)
            if not source:
                continue
            sources.append(source)
            video = item.get("video") if isinstance(item, dict) and \
                isinstance(item.get("video"), dict) else {}
            clip = ""
            for key in ("play_addr_h264", "play_addr", "play_addr_265", "download_addr"):
                clip = next((url for url in url_list(video.get(key))
                             if url.startswith("http")), "")
                if clip:
                    break
            clips.append(clip)
        return "image", sources, sources[0] if sources else "", clips if any(clips) else []
    video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    sources: List[str] = []
    for key in ("play_addr_h264", "play_addr", "play_addr_265", "play_addr_256", "download_addr"):
        for url in url_list(video.get(key)):
            if url.startswith("http") and url not in sources:
                sources.append(url)
    cover = next((_runtime_url(video.get(key)) for key in
                  ("cover_original_scale", "cover", "origin_cover", "dynamic_cover",
                   "cover_medium")
                  if _runtime_url(video.get(key))), "")
    return "video", sources, cover, []


def remote_work(payload: Mapping[str, Any], sec_uid: str = "") -> Dict[str, Any]:
    """A card for a work seen online (listing), without local files."""
    media_type = "image" if aw.gallery_items(payload) or aw.media_type(payload) == "gallery" \
        else "video"
    _kind, _sources, cover, _clips = remote_media(payload)
    sources, music_title, music_author = aw.music_info(payload)
    return {
        "aweme_id": aw.aweme_id(payload),
        "title": aw.title(payload) or "无标题",
        "create_time": aw.create_time(payload),
        "media_type": media_type,
        "work_kind": work_kind(payload, media_type),
        "media_paths": [],
        "media_state": "remote",
        "cover_path": "",
        "cover_source": cover,
        "cover_media_type": "image" if cover else "",
        "music_sources": sources,
        "music_source": sources[0] if sources else "",
        "music_title": music_title,
        "music_author": music_author,
        "remote_media": True,
        "is_pinned": aw.is_pinned(payload),
        "local_status": "remote",
        "author_sec_uid": aw.author_sec_uid(payload) or sec_uid,
        **_counts(payload),
    }


def merge_remote_local(remote: Dict[str, Any], local: Optional[Dict[str, Any]], *,
                       pin_known: bool = True) -> Dict:
    if not local:
        return remote
    merged = dict(remote)
    for key in ("media_paths", "media_state", "cover_path", "media_type", "work_kind",
                "local_status", "author_sec_uid", "download_status", "error_message",
                "error_code", "offline_label", "offline_reason", "first_media_path"):
        if local.get(key):
            merged[key] = local[key]
    for key in ("title", "create_time"):
        if not merged.get(key) and local.get(key):
            merged[key] = local[key]
    if not pin_known:
        merged["is_pinned"] = bool(local.get("is_pinned"))
    if local.get("local_status") == "present":
        merged["remote_media"] = False
    return merged


def _avatar_url(user: Mapping[str, Any]) -> str:
    """The largest avatar a user object carries, loaded straight from the CDN.

    Listing authors (likes, collections) only carry ``avatar_thumb``.  A
    signed URL keeps its query, which the CDN needs to serve it.
    """
    url = first_url(user.get("avatar_larger") or user.get("avatar_medium")
                    or user.get("avatar_thumb"))
    return url if "x-signature" in url or "x-expires" in url else re.sub(r"[?#].*$", "", url)


def creator_payload(local: Optional[Mapping[str, Any]],
                    remote: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The creator header of a homepage (remote profile over the stored row)."""
    local = local or {}
    online = remote or {}
    raw_gender = online.get("gender", local.get("gender"))
    gender = to_int(raw_gender)
    if gender == 0 and str(raw_gender or "").strip() in ("男", "male", "m"):
        gender = 1
    elif gender == 0 and str(raw_gender or "").strip() in ("女", "female", "f"):
        gender = 2
    aweme_count = online.get("aweme_count")
    if aweme_count is None:
        aweme_count = online.get("aweme_num", local.get("cloud_count"))
    sec_uid = str(online.get("sec_uid") or local.get("sec_uid") or "")
    return {
        "sec_uid": sec_uid,
        "nickname": str(online.get("nickname") or local.get("nickname") or "未知主播"),
        "signature": str(online.get("signature") or ""),
        "unique_id": str(online.get("unique_id") or online.get("short_id") or ""),
        "ip_location": str(online.get("ip_location") or online.get("location") or ""),
        "avatar_url": _avatar_url(online) or str(local.get("avatar_url") or ""),
        "cover_url": _runtime_url(online.get("cover_url") or online.get("profile_cover")
                                  or online.get("homepage_background") or online.get("cover")),
        "gender": gender if gender in (1, 2) else 0,
        # Shown as small tags next to the IP location, like the web homepage.
        "age": max(0, to_int(online.get("user_age"))),
        "province": str(online.get("province") or ""),
        "city": str(online.get("city") or ""),
        "country": str(online.get("country") or ""),
        "homepage_url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else "",
        "source": "remote" if online else "local",
        "following_count": to_int(online.get("following_count") or online.get("following_num")),
        "follower_count": to_int(online.get("follower_count") or online.get("follower_num")),
        "total_favorited": to_int(online.get("total_favorited") or online.get("favorited_count")),
        "aweme_count": to_int(aweme_count),
    }
