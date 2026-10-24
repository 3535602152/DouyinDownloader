"""Creator homepages and the work player.

A homepage shows the local archive first; "refresh" merges the newest online
page on top.  Opening a work plays local files when complete, otherwise a
transient online preview (signed URLs are never stored).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.log import get_logger
from douyin import aweme as aw
from services import views
from services.common import explain
from services.context import Context
from storage import profiles as profile_store
from storage import sources as source_store
from storage import works as work_store
from storage.db import rows

PAGE_SIZE = 36

logger = get_logger(__name__)


def _image_key(url: str) -> str:
    """An image URL without its signature query, which changes per request."""
    return re.sub(r"[?#].*$", "", str(url or "").strip())


def _with_cached_images(payload: Dict[str, Any], cached: Optional[Dict[str, Any]]
                        ) -> Dict[str, Any]:
    """Point avatar / cover at the local copies when they show the same picture."""
    result = dict(payload)
    for kind in profile_store.IMAGE_KINDS:
        path = str((cached or {}).get(f"{kind}_file") or "")
        key = str((cached or {}).get(f"{kind}_key") or "")
        if path and key and key == _image_key(result.get(f"{kind}_url") or "")                 and Path(path).is_file():
            result[f"{kind}_path"] = path
    return result


def _local_page(db, sec_uid: str, offset: int, limit: int, hidden_only: bool = False
                ) -> Tuple[List[Dict], bool]:
    """A creator's known works: monitored memberships, else works they authored."""
    source = source_store.find(db, "creator", sec_uid)
    if source is not None:
        found, more = source_store.member_page(db, int(source["id"]), offset, limit, hidden_only)
    else:
        hidden = " AND w.visibility = 'hidden'" if hidden_only else ""
        found = rows(db, "SELECT w.*, d.status, d.error, d.error_code FROM works w "
                         "LEFT JOIN downloads d ON d.aweme_id = w.aweme_id "
                         f"WHERE w.author_sec_uid = ?{hidden} "
                         "ORDER BY w.is_pinned DESC, w.create_time DESC, w.aweme_id DESC "
                         "LIMIT ? OFFSET ?",
                     (sec_uid, limit + 1, offset))
        more = len(found) > limit
        found = found[:limit]
    files = work_store.files_of(db, [row["aweme_id"] for row in found])
    return [dict(row, files=files.get(row["aweme_id"], [])) for row in found], more


def _local_record(db, aweme_id: str) -> Optional[Dict[str, Any]]:
    return work_store.get_work(db, aweme_id)


class CreatorPages:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def _local_row(self, sec_uid: str) -> Dict[str, Any]:
        return await self.ctx.db.read(source_store.find, "creator", sec_uid) or {"sec_uid": sec_uid}

    async def _cached(self, sec_uid: str) -> Optional[Dict[str, Any]]:
        """The last online profile (as a ``cached`` header), if one was seen."""
        cached = await self.ctx.db.read(profile_store.get, sec_uid)
        if not cached or not cached["payload"]:
            return None
        return _with_cached_images({**cached["payload"], "source": "cached"}, cached)

    async def _remember(self, sec_uid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Cache an online profile; new avatar / cover pictures download in the background."""
        await self.ctx.db.write(profile_store.save_payload, sec_uid,
                                {key: value for key, value in payload.items()
                                 if not key.endswith("_path")})
        cached = await self.ctx.db.read(profile_store.get, sec_uid)
        result = _with_cached_images(payload, cached)
        stale = [kind for kind in profile_store.IMAGE_KINDS
                 if result.get(f"{kind}_url") and not result.get(f"{kind}_path")]
        if stale:
            self.ctx.background(self._download_images(sec_uid, result, stale),
                                "creator profile images")
        return result

    async def _download_images(self, sec_uid: str, payload: Dict[str, Any],
                               kinds: List[str]) -> None:
        folder = self.ctx.cache_dir / "creators" / re.sub(r"[^\w.-]", "_", sec_uid)
        headers = self.ctx.client.media_headers()
        for kind in kinds:
            url = str(payload.get(f"{kind}_url") or "")
            try:
                saved = await self.ctx.downloader.fetcher.fetch(
                    [(url, headers)], folder / f"{kind}.jpeg", segmented=False,
                    image_suffix=True)
            except Exception as exc:  # the next visit retries; online URLs still show
                logger.info("creator %s %s not cached: %s", sec_uid, kind, exc)
                continue
            # A changed picture may arrive as another type: drop the old copy.
            for old in folder.glob(f"{kind}.*"):
                if old != Path(saved):
                    old.unlink(missing_ok=True)
            await self.ctx.db.write(profile_store.save_image, sec_uid, kind,
                                    _image_key(url), str(saved))

    async def profile(self, sec_uid: str) -> Dict[str, Any]:
        local = await self._local_row(sec_uid)
        try:
            user = await self.ctx.client.user_profile(sec_uid)
            if not user:
                raise RuntimeError("主播资料暂时无法读取")
            return {"creator": await self._remember(sec_uid, views.creator_payload(local, user))}
        except Exception as exc:
            cached = await self._cached(sec_uid)
            return {"creator": cached or views.creator_payload(local), "error": explain(exc)}

    async def home(self, sec_uid: str, refresh: bool, cursor: int, offset: int,
                   hidden_only: bool = False) -> Dict[str, Any]:
        # Hidden works are a local finding of full scans (the homepage API no
        # longer lists them), so that filter always pages the archive.
        refresh = refresh and not hidden_only
        local_row = await self._local_row(sec_uid)
        records, local_more = [], False
        if not (refresh and cursor > 0):
            records, local_more = await self.ctx.db.read(_local_page, sec_uid,
                                                         0 if refresh else offset, PAGE_SIZE,
                                                         hidden_only)
        local_works = [views.local_work(record, record["files"], include_media=False)
                       for record in records]
        cached = await self._cached(sec_uid)
        result = {"creator": cached or views.creator_payload(local_row), "works": local_works,
                  "has_more": local_more, "next_cursor": 0,
                  "next_offset": offset + len(local_works), "next_mode": "local",
                  "source": "local"}
        if not refresh:
            return result
        try:
            profile, profile_error = {}, ""
            if cursor == 0:
                try:
                    profile = await self.ctx.client.user_profile(sec_uid) or {}
                    if not profile:
                        profile_error = "主播资料暂时无法读取"
                except Exception as exc:
                    profile_error = explain(exc)
            page = (await self.ctx.client.user_posts(sec_uid, cursor, PAGE_SIZE)).ensure_valid(
                "主播作品接口")
            if page.has_more and page.cursor == cursor:
                raise RuntimeError("作品列表加载异常，请刷新后重试")
            if cursor > 0 and not page.has_more:
                records, local_more = await self.ctx.db.read(_local_page, sec_uid, offset,
                                                             PAGE_SIZE)
                local_works = [views.local_work(record, record["files"], include_media=False)
                               for record in records]
            by_id = {work["aweme_id"]: work for work in local_works}
            remote = []
            for item in page.dict_items:
                payload = aw.extract(item)
                if not payload or not aw.belongs_to(payload, sec_uid):
                    continue
                remote.append(views.merge_remote_local(
                    views.remote_work(payload, sec_uid), by_id.get(aw.aweme_id(payload)),
                    pin_known=aw.has_pin_state(payload)))
            await self.ctx.db.write(work_store.save_works,
                                    [aw.extract(item) for item in page.dict_items
                                     if aw.extract(item)])
            remote_ids = {work["aweme_id"] for work in remote}
            tail = [work for work in local_works if work["aweme_id"] not in remote_ids] \
                if cursor == 0 or not page.has_more else []
            creator = await self._remember(sec_uid, views.creator_payload(local_row, profile))                 if profile else (cached or views.creator_payload(local_row))
            result.update(
                creator=creator,
                profile_error=profile_error, works=remote + tail,
                has_more=page.has_more or local_more,
                next_cursor=page.cursor if page.has_more else 0,
                next_offset=offset + len(local_works),
                next_mode="remote" if page.has_more else "local",
                source="mixed" if local_works else "remote")
        except Exception as exc:
            result.update(error=explain(exc) or "主播资料刷新失败", has_more=True,
                          next_cursor=cursor, next_mode="remote")
        return result

    async def work_detail(self, sec_uid: str, aweme_id: str, force_remote: bool = False
                          ) -> Dict[str, Any]:
        record = await self.ctx.db.read(_local_record, aweme_id)
        local: Dict[str, Any] = {}
        if record is not None:
            owner = str(record.get("author_sec_uid") or "")
            if owner and sec_uid and owner != sec_uid:
                return {"error": "作品不属于当前主播", "media_sources": []}
            local = views.local_work(record, record.get("files") or [], include_media=True)
            local["remote_media"] = False
        needs_music = local.get("media_type") == "image" and not local.get("music_path")
        if local.get("media_paths") and local.get("media_state") == "present" \
                and not force_remote and not needs_music:
            return local

        def fallback() -> Dict[str, Any]:
            if not local:
                return {"error": "未获取到作品详情，请重试", "media_sources": []}
            view = dict(local)
            if needs_music:
                view["music_error"] = "音乐暂时无法读取，请重试"
            if view.get("media_state") == "partial":
                view["error"] = "本地图文仅下载了部分图片，远端详情暂时无法获取"
            return view

        try:
            payload, _reason, _kind = await self.ctx.downloader.fetch_detail(aweme_id)
        except Exception:
            return fallback()
        if payload is None or (aw.author_sec_uid(payload) and sec_uid
                               and aw.author_sec_uid(payload) != sec_uid):
            return fallback()
        work = views.remote_work(payload, sec_uid)
        media_type, sources, cover, clips = views.remote_media(payload)
        items = aw.gallery_items(payload) if media_type == "image" else []
        partial = media_type == "image" and (not items or len(sources) < len(items))
        work.update(media_type=media_type, media_sources=sources,
                    live_sources=clips if len(clips) == len(sources) else [],
                    cover_source=cover or work.get("cover_source") or "",
                    cover_media_type="image" if cover else "", remote_media=True)
        if local.get("media_state") == "present" and local.get("media_paths"):
            # Keep the verified local gallery; take the online soundtrack.
            work.update(media_paths=local["media_paths"], live_paths=local.get("live_paths", []),
                        media_sources=[], live_sources=[], media_state="present",
                        cover_path=local.get("cover_path", ""), remote_media=False,
                        music_remote=bool(work.get("music_sources")))
        elif partial:
            work.update(media_paths=local.get("media_paths", []), media_state="partial",
                        live_sources=[],
                        error="远端图文仅获取到部分图片，已保留可用图片，请重试")
        if not work.get("media_sources") and not work.get("media_paths") \
                and not work.get("error"):
            work["error"] = "作品暂未提供可播放地址，请重试"
        for key in ("title", "create_time", "like_count", "comment_count", "collect_count",
                    "share_count", "music_title", "music_author"):
            if not work.get(key) and local.get(key):
                work[key] = local[key]
        return work
