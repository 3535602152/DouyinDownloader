"""Comments and comment images.

Reading a work's comments shows the saved copy first, then fetches every
comment and reply, saves them, and makes thumbnails for comment images.
A creator with comments switched on has the comments of its downloaded
works saved by a slow background worker (each ``*_comments.json`` beside the
media), in two passes.  The first keeps what a person sees when opening a
work: the first 50 comments in Douyin's own order and the first 3 replies of
each; every work gets this first, and a newly downloaded work goes ahead of
the second pass.  The second then reads every remaining comment and reply,
the works with the fewest comments first, a page at a time (it resumes where
it stopped and gives way as soon as a work needs its first pass).  The
pictures and stickers of the comments go to a ``*_comments`` folder beside
the file.  The comment endpoints are quick to answer with risk control, so
the worker paces itself (a page every 3-7 s, a break between works) and
on a risk signal stops reading comments for hours
(scans and downloads go on).  A work's first comments are read again when a
scan saw its comment count change, all of them (at most daily) when it grew
by 5 or more.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.fs import atomic_write_json, image_data_url, nonempty_file
from douyin import urls
from douyin.errors import DouyinError, EmptyResponse, LoginRequired, is_risk
from douyin.pages import CursorGuard, Page, first_url, to_int, url_list
from engine import live_photo, scheduler
from services.common import explain, resolve_link
from services.context import Context
from storage import comments as comment_store
from storage import works as work_store

# What the background sync keeps of a work.
COMMENT_LIMIT = 50              # the first comments, in Douyin's order
REPLY_LIMIT = 3                 # the first replies of each comment
# Background sync pacing (seconds): steady, the app runs for days.
PAGE_GAP = (3.0, 7.0)           # between two comment requests
WORK_GAP = (15.0, 30.0)         # between two works
PAUSE_BASE = 2 * 3600           # the first risk pause; doubles per repeat
PAUSE_MAX = 24 * 3600
REFRESH_AFTER = 24 * 3600       # a work whose comment count changed is re-read at most daily
RETRY_AFTER = 6 * 3600          # a failed work is tried again after this long
IDLE_SECONDS = 600              # nothing to read: look again after this long
# The second pass (every comment and reply).
FULL_REFRESH_AFTER = 86400      # a finished work is read in full again at most this often
FULL_GROW_MIN = 5               #... and only once its comment count grew by this many
FULL_WRITE_EVERY = 10           # rewrite *_comments.json after this many pages
# Saved order: a comment's place in Douyin's list times this, plus its reply's place.
POSITION_STEP = 100000


def checked(page: Page, what: str) -> Page:
    """``Page.ensure_valid``, except that a work without comments is an empty page.

    Douyin answers ``comments: null`` (and no further page) for a work
    nobody commented on; that is not a risk signal.
    """
    if page.raw and page.items_missing and not page.has_more and page.status_code == 0 \
            and not page.verify_page and not page.login_tip:
        return page
    return page.ensure_valid(what)


def comment_id(comment: Dict[str, Any], fallback: str = "") -> str:
    for key in ("cid", "comment_id", "id"):
        value = comment.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    identity = json.dumps({"text": comment.get("text") or "", "time": comment.get("create_time"),
                           "uid": user.get("uid"), "nick": user.get("nickname")},
                          ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    return f"anon_{digest}{'_' + fallback if fallback else ''}"


def comment_images(comment: Dict[str, Any], cid: str) -> List[Dict[str, Any]]:
    """Pictures of one comment: original-first mirrors, preview, live clip."""
    images = []
    for index, item in enumerate(comment.get("image_list") or []):
        if not isinstance(item, dict):
            continue
        mirrors: List[str] = []
        for key in ("origin_url", "download_url", "medium_url", "thumb_url",
                    "download_url_list", "url_list", "display_image", "owner_watermark_image"):
            for url in url_list(item.get(key)):
                if url not in mirrors:
                    mirrors.append(url)
        if not mirrors:
            continue
        clips: List[str] = []
        for entry in item.get("video_list") or []:
            if isinstance(entry, dict):
                for key in ("play_addr", "download_addr", "url_list"):
                    clips += url_list(entry.get(key))
        video = item.get("video") if isinstance(item.get("video"), dict) else {}
        for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
            clips += url_list(video.get(key))
        thumbs = url_list(item.get("thumb_url"))
        images.append({
            "comment_id": cid, "index": index, "urls": mirrors,
            "live_video_urls": list(dict.fromkeys(clips)),
            "preview_url": thumbs[0] if thumbs else mirrors[-1],
            "width": to_int(item.get("width")), "height": to_int(item.get("height")),
            "is_live": bool(clips),
        })
    return images


def comment_sticker(comment: Dict[str, Any]) -> List[str]:
    """Mirrors of a comment's sticker (表情包): the animated one first."""
    sticker = comment.get("sticker")
    if not isinstance(sticker, dict):
        return []
    found: List[str] = []
    for key in ("animate_url", "static_url", "url"):
        for url in url_list(sticker.get(key)):
            if url not in found:
                found.append(url)
    return found


# The player's comment panel reads pages live, the way the web player does.
LIVE_PAGE_SIZE = 10            # the web player reads 10 comments per page
LIVE_REPLY_FIRST = 3            # "展开 N 条回复" shows a few first, then pages of 10
LIVE_REPLY_PAGE = 10
EMOJI_CACHE_SECONDS = 7 * 86400
EMPTY_RETRY_WAIT = 1.2          # an empty comment page is asked once more after this


def live_view(comment: Dict[str, Any], author_sec_uid: str = "") -> Dict[str, Any]:
    """One comment as the player's panel shows it (nothing is saved)."""
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    cid = comment_id(comment)
    images = [{"preview": image["preview_url"], "full": image["urls"][0],
               "mirrors": list(dict.fromkeys([image["preview_url"], *image["urls"]])),
               "width": image["width"], "height": image["height"]}
              for image in comment_images(comment, cid)]
    sticker = comment_sticker(comment)
    labels = [str(item.get("label_text") or "") for item in comment.get("label_list") or []
              if isinstance(item, dict)]
    return {
        "cid": cid,
        "nickname": str(user.get("nickname") or "匿名"),
        "avatar": first_url(user.get("avatar_thumb") or user.get("avatar_medium")),
        "text": str(comment.get("text") or ""),
        "create_time": to_int(comment.get("create_time")),
        "ip_label": str(comment.get("ip_label") or ""),
        "digg_count": to_int(comment.get("digg_count")),
        "reply_total": to_int(comment.get("reply_comment_total")),
        "reply_to": str(comment.get("reply_to_username") or ""),
        "is_author": bool(author_sec_uid) and str(user.get("sec_uid") or "") == author_sec_uid,
        "author_liked": bool(to_int(comment.get("is_author_digged"))) or "作者赞过" in labels,
        "images": images,
        "sticker": sticker[0] if sticker else "",
        # Signed links expire: the panel tries every mirror before hiding it.
        "sticker_mirrors": sticker,
    }


def saved_view(row: Dict[str, Any], images: List[Dict[str, Any]],
               author_sec_uid: str = "", files: Optional[Dict[str, str]] = None
               ) -> Dict[str, Any]:
    """A saved comment as the player's panel shows it, with its downloaded files.

    The saved links are signed and expire; ``files`` (name → path) are the
    pictures and sticker the comment sync downloaded (``<cid>_<n>``, ``<cid>_sticker``).
    """
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    view = live_view(raw if isinstance(raw, dict) else {}, author_sec_uid)
    files = files or {}

    def downloaded(stem: str) -> str:
        return next((path for name, path in files.items() if Path(name).stem == stem), "")

    view["sticker_path"] = downloaded(f"{row['cid']}_sticker") if view["sticker"] else ""
    view.update({
        "cid": row["cid"], "nickname": row.get("nickname") or view["nickname"],
        "text": row.get("text") or "", "create_time": to_int(row.get("create_time")),
        "ip_label": row.get("ip_label") or "", "digg_count": to_int(row.get("digg_count")),
        # Only the replies saved here can be shown.
        "reply_total": to_int(row.get("saved_replies")), "is_reply": bool(row.get("is_reply")),
        "images": [{"preview": image["preview_url"],
                    "full": (image["urls"] or [image["preview_url"]])[0],
                    "mirrors": list(dict.fromkeys(filter(None, [image["preview_url"],
                                                                *image["urls"]]))),
                    "width": image["width"], "height": image["height"],
                    "local_paths": [image["thumb_path"], image["local_path"]
                                    or downloaded(f"{row['cid']}_{image['index'] + 1}")]}
                   for image in images],
    })
    return view


def display(comment: Dict[str, Any], *, is_reply: bool, parent: str) -> Dict[str, Any]:
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    stamp = to_int(comment.get("create_time"))
    images = comment_images(comment, comment["cid"])
    return {
        "cid": comment["cid"], "nickname": str(user.get("nickname") or "匿名"),
        "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "",
        "ip_label": str(comment.get("ip_label") or ""),
        "digg_count": to_int(comment.get("digg_count")),
        "text": str(comment.get("text") or ""),
        "image_count": len(images), "live_image_count": sum(1 for i in images if i["is_live"]),
        "is_reply": is_reply, "parent_cid": parent,
    }


def _stem_files(folder: Path, stem: str) -> List[Path]:
    """The saved files of one comment picture: ``stem.ext`` and its ``stem_live*``."""
    if not folder.exists():
        return []
    return [path for path in folder.iterdir()
            if (path.stem == stem or path.name.startswith(f"{stem}_live"))
            and nonempty_file(path)]


def _image_ext(url: str) -> str:
    from douyin.aweme import image_ext

    return image_ext(url)


class CommentService:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.aweme_id = ""
        self.images: Dict[str, Dict[str, Any]] = {}
        self._generation = 0
        self._worker: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._current: Optional[Dict[str, Any]] = None
        self._sent: collections.deque = collections.deque()    # request times, last hour
        self._next_request = 0.0
        self._pause_until = 0
        self._pause_level = 0
        # For the download panel's 评论 tab: what the worker is doing now.
        self._phase, self._phase_until = "idle", 0.0
        self._read = 0          # comments read (saved, in the second pass) of the current work
        self._stage = "first"   # the current work's pass: "first" or "full"

    # ── fetching ────────────────────────────────────────────────────

    async def collect(self, aweme_id: str, progress=None, *, paced: bool = False,
                      limit: int = 0, reply_limit: int = 0) -> List[Dict[str, Any]]:
        """The comments of one work with their replies (raises if incomplete).

        ``limit`` keeps only the first comments and ``reply_limit`` only the
        first replies of each (one request); 0 reads everything.  ``paced``
        (the background sync) spaces the requests out and waits out risk
        signals instead of failing.
        """
        client = self.ctx.client
        page_size = self.ctx.settings.comments_page_size
        fetch = self._paced_page if paced else self._page
        result: List[Dict[str, Any]] = []
        seen = set()
        cursor, guard = 0, CursorGuard()
        while True:
            count = min(page_size, limit - len(result)) if limit else page_size
            page = await fetch(lambda cursor=cursor, count=count: client.comments(
                aweme_id, cursor, count, gentle=paced), "评论接口")
            for position, raw in enumerate(page.dict_items):
                if limit and len(result) >= limit:
                    break
                comment = dict(raw)
                comment["cid"] = comment_id(raw, f"{cursor}_{position}")
                if comment["cid"] in seen:
                    continue
                seen.add(comment["cid"])
                comment["_order"] = len(result)
                if to_int(raw.get("reply_comment_total")) > 0:
                    comment["_replies"] = await self._replies(
                        aweme_id, comment["cid"], reply_limit or page_size, seen, fetch, paced,
                        once=bool(reply_limit))
                result.append(comment)
            if progress is not None:
                progress(len(result))
            if not page.has_more or (limit and len(result) >= limit):
                return result
            cursor = guard.advance(page.cursor, "评论")

    async def _replies(self, aweme_id: str, cid: str, page_size: int, seen: set, fetch,
                       paced: bool, *, once: bool = False) -> List[Dict]:
        replies: List[Dict[str, Any]] = []
        cursor, guard = 0, CursorGuard()
        while True:
            # Reply pages often come back empty; like the player's panel, an
            # empty answer gets one try through the logged-in page.
            try:
                page = await fetch(lambda cursor=cursor: self.ctx.client.comment_replies(
                    aweme_id, cid, cursor, page_size, gentle=paced, page_fallback=True),
                    "回复接口")
            except EmptyResponse:
                if not paced:
                    raise
                # The background sync keeps the comment without (more) replies:
                # an empty reply page is not a reason to stop reading comments.
                return replies
            for position, raw in enumerate(page.dict_items):
                reply = dict(raw)
                reply["cid"] = comment_id(raw, f"{cid}_{cursor}_{position}")
                if reply["cid"] not in seen:
                    seen.add(reply["cid"])
                    replies.append(reply)
            if once:
                return replies[:page_size]
            if not page.has_more:
                return replies
            cursor = guard.advance(page.cursor, "回复")

    @staticmethod
    async def _page(call, what: str) -> Page:
        return checked(await call(), what)

    # ── live browsing (the player's comment panel) ─────────────────

    async def browse(self, aweme_id: str, cursor: int = 0, author_sec_uid: str = "",
                     comment_id_: str = "", count: int = 0) -> Dict[str, Any]:
        """One page of a work's comments (or of one comment's replies), live.

        One gentle request per page; a risk answer pauses the comment
        features like the background sync does (scans and downloads go on).
        """
        client = self.ctx.client
        what = "回复接口" if comment_id_ else "评论接口"
        size = int(count or (LIVE_REPLY_PAGE if comment_id_ else LIVE_PAGE_SIZE))

        async def read() -> Page:
            if comment_id_:
                # One direct try, then the logged-in page if that comes back empty.
                return checked(await client.comment_replies(aweme_id, comment_id_, int(cursor),
                                                            size, gentle=True,
                                                            page_fallback=True), what)
            return checked(await client.comments(aweme_id, int(cursor), size, gentle=True),
                           what)

        try:
            try:
                page = await read()
            except EmptyResponse:
                if comment_id_:
                    raise       # replies already went through the logged-in page
                # One empty answer is common; ask once more before giving up.
                await asyncio.sleep(EMPTY_RETRY_WAIT)
                page = await read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A person clicking: only an explicit risk answer pauses the
            # comment features, not an empty page.
            if is_risk(exc) and not isinstance(exc, (LoginRequired, EmptyResponse)):
                await self._risk_pause(exc)
            text = "回复暂时读不到" if comment_id_ else "评论暂时读不到"
            return {"comments": [], "cursor": int(cursor), "has_more": False,
                    "error": text if isinstance(exc, EmptyResponse) else (explain(exc) or text)}
        comments = [live_view(raw, author_sec_uid) for raw in page.dict_items]
        return {"comments": comments, "cursor": page.cursor,
                "has_more": page.has_more and page.cursor != int(cursor),
                "total": to_int(page.raw.get("total")) if isinstance(page.raw, dict) else 0}

    async def saved(self, aweme_id: str, cursor: int = 0, author_sec_uid: str = "",
                    comment_id_: str = "", count: int = 0, *,
                    author_only: bool = False) -> Dict[str, Any]:
        """One page of the saved comments, shaped like :meth:`browse` (no request).

        For hidden works; ``author_only`` pages the comments the work's author
        wrote or replied under, each with the author's replies (``author_replies``).
        """
        if author_only and not author_sec_uid:
            return {"comments": [], "cursor": int(cursor), "has_more": False, "total": 0,
                    "source": "local"}
        size = int(count or (LIVE_REPLY_PAGE if comment_id_ else LIVE_PAGE_SIZE))
        page = await self.ctx.db.read(
            comment_store.saved_page, aweme_id, parent_cid=comment_id_,
            author=author_sec_uid if author_only else "", offset=int(cursor), limit=size)
        def view(row: Dict[str, Any]) -> Dict[str, Any]:
            return saved_view(row, page["images"].get(row["cid"], []), author_sec_uid,
                              page["files"])

        comments = [view(row) for row in page["comments"]]
        if author_only:
            for comment in comments:
                comment["author_replies"] = [view(row) for row in
                                             page["author_replies"].get(comment["cid"], [])]
        return {"comments": comments, "cursor": int(cursor) + len(comments),
                "has_more": page["has_more"], "total": page["total"], "source": "local"}

    async def emojis(self) -> Dict[str, str]:
        """The comment emoji table, cached on disk for a week."""
        path = self.ctx.cache_dir / "comment_emojis.json"
        cached: Dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached = data.get("emojis") or {}
            if time.time() - float(data.get("saved_at") or 0) < EMOJI_CACHE_SECONDS and cached:
                return cached
        except (OSError, ValueError, AttributeError):
            pass
        try:
            fresh = await self.ctx.client.emoji_list()
        except asyncio.CancelledError:
            raise
        except Exception:
            return cached
        if fresh:
            atomic_write_json(path, {"saved_at": int(time.time()), "emojis": fresh})
        return fresh or cached

    @staticmethod
    def flatten(comments: List[Dict[str, Any]]) -> List[Tuple[Dict, bool, str]]:
        records = []
        for comment in comments:
            records.append((comment, False, ""))
            for reply in comment.get("_replies") or []:
                records.append((reply, True, comment["cid"]))
        return records

    async def _save(self, aweme_id: str, comments: List[Dict[str, Any]], *,
                    replace: bool = True) -> List[Dict]:
        """Save these comments and replies, in Douyin's order (``_order``)."""
        rows, images = [], []
        for index, comment in enumerate(comments):
            base = to_int(comment.get("_order"), index) * POSITION_STEP
            entries = [(comment, False, "")] + [(reply, True, comment["cid"])
                                                for reply in comment.get("_replies") or []]
            for offset, (item, is_reply, parent) in enumerate(entries):
                view = display(item, is_reply=is_reply, parent=parent)
                rows.append({**view, "create_time": to_int(item.get("create_time")),
                             "position": base + min(offset, POSITION_STEP - 1),
                             "raw": {key: value for key, value in item.items()
                                     if key not in ("_replies", "_order")}})
                images += comment_images(item, item["cid"])
        await self.ctx.db.write(comment_store.save, aweme_id, rows, images, replace=replace)
        return images

    # ── reading one work (the comments page) ───────────────────────

    def load(self, source: str) -> bool:
        text = str(source or "").strip()
        if not text:
            raise ValueError("请输入作品链接、分享文案或作品 ID")
        self.ctx.tasks.cancel_where(lambda task: task.name == "comments-load")
        self._generation += 1
        generation = self._generation
        events = self.ctx.events

        def current() -> bool:
            return generation == self._generation

        async def run(_task) -> None:
            events.emit("comments_reset")
            events.emit("comment_busy", busy=True)
            try:
                aweme_id = await self._resolve(text)
                self.aweme_id, self.images = aweme_id, {}
                cached = await self._show_cached(aweme_id, current)
                claimed = await self.ctx.db.write(comment_store.claim_sync, aweme_id, force=True)
                if not claimed:
                    # Another task is syncing this work; the cached copy is shown.
                    events.activity("comments", f"评论 {cached[0]} 图片 {cached[1]}",
                                    running=False)
                    return
                events.activity("comments", f"加载评论 {cached[0]}" if cached[0] else "加载评论")

                def loaded(count: int) -> None:
                    if current():
                        events.activity("comments", f"加载评论 {count}")

                comments, error = None, None
                for attempt in range(3):
                    try:
                        comments = await self.collect(aweme_id, loaded)
                        break
                    except LoginRequired as exc:
                        error = exc
                        break
                    except DouyinError as exc:
                        error = exc
                        if attempt < 2 and current():
                            events.activity("comments", "加载评论",
                                            detail=f"重试 {attempt + 2}/3")
                            await asyncio.sleep(2 + attempt * 3)
                if comments is None:
                    await self.ctx.db.write(comment_store.finish_sync, aweme_id, success=False,
                                            error=explain(error or RuntimeError("评论加载失败")))
                    events.activity("comments", "")
                    events.error("comments", f"评论加载失败：{explain(error or RuntimeError())}")
                    return
                images = await self._save(aweme_id, comments)
                await self.ctx.db.write(comment_store.finish_sync, aweme_id, success=True,
                                        visibility="visible")
                if not current():
                    return
                records = self.flatten(comments)
                nicknames = {}
                for comment, is_reply, parent in records:
                    view = display(comment, is_reply=is_reply, parent=parent)
                    nicknames[view["cid"]] = view["nickname"]
                    events.emit("comment", comment=view)
                await self._thumbnails(aweme_id, images, nicknames, current)
                events.activity("comments", f"评论 {len(records)} 图片 {len(images)}",
                                running=False)
                events.emit("comments_done", count=len(records), images=len(images))
            except asyncio.CancelledError:
                events.activity("comments", "")
                raise
            except Exception as exc:
                events.activity("comments", "")
                events.error("comments", f"评论加载失败：{explain(exc)}")
            finally:
                if current():
                    events.emit("comment_busy", busy=False)

        self.ctx.tasks.submit("comments-load", run, scope="comments", lane="comments",
                              key=f"comments-load:{generation}")
        return True

    async def _resolve(self, text: str) -> str:
        direct = urls.aweme_id_in(text)
        if direct:
            return direct
        url, link = await resolve_link(self.ctx.client, text)
        if link is not None and link.aweme_id:
            return link.aweme_id
        found = urls.aweme_id_in(url)
        if found:
            return found
        raise ValueError("无法从链接中找到作品 ID，请粘贴视频 / 图文链接、分享文案或数字作品 ID。")

    async def _show_cached(self, aweme_id: str, current) -> Tuple[int, int]:
        # Shows the saved copy first; returns the (comments, images) shown.
        saved = await self.ctx.db.read(comment_store.load, aweme_id)
        counts = (len(saved["comments"]), len(saved["images"]))
        if not saved["comments"] and not saved["images"]:
            return counts
        events = self.ctx.events
        nicknames = {}
        for row in saved["comments"]:
            if not current():
                return counts
            stamp = int(row.get("create_time") or 0)
            nicknames[row["cid"]] = row["nickname"] or "匿名"
            events.emit("comment", comment={
                "cid": row["cid"], "nickname": row["nickname"] or "匿名",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "",
                "ip_label": row["ip_label"], "digg_count": int(row["digg_count"] or 0),
                "text": row["text"], "image_count": int(row["image_count"] or 0),
                "live_image_count": int(row["live_image_count"] or 0),
                "is_reply": bool(row["is_reply"]), "parent_cid": row["parent_cid"]})
        for image in saved["images"]:
            key = f"{image['comment_id']}_{image['index']}"
            self.images[key] = image
            self._emit_image(key, image, nicknames.get(image["comment_id"], "匿名"))
        return counts

    def _emit_image(self, key: str, image: Dict[str, Any], nickname: str) -> None:
        src = image_data_url(image.get("thumb_path")) if image.get("thumb_path") else ""
        self.ctx.events.emit("comment_image", image={
            "key": key, "comment_id": str(image["comment_id"]), "index": int(image["index"]),
            "is_live": bool(image.get("is_live")), "nickname": nickname, "src": src,
            "placeholder": not src})

    async def _thumbnails(self, aweme_id: str, images: List[Dict[str, Any]],
                          nicknames: Dict[str, str], current) -> None:
        folder = self.ctx.cache_dir / "comments" / aweme_id / "thumbs"
        folder.mkdir(parents=True, exist_ok=True)
        for order, image in enumerate(images, start=1):
            if not current():
                return
            key = f"{image['comment_id']}_{image['index']}"
            cached = self.images.get(key) or {}
            image = {**image, "local_path": cached.get("local_path", "")}
            thumb = await self._thumbnail(image, folder, key)
            if thumb:
                image["thumb_path"] = str(thumb)
                await self.ctx.db.write(comment_store.set_image_path, aweme_id,
                                        image["comment_id"], image["index"],
                                        thumb_path=str(thumb))
            self.images[key] = image
            self._emit_image(key, image, nicknames.get(image["comment_id"], "匿名"))
            if order % 5 == 0 or order == len(images):
                self.ctx.events.activity("comments", f"加载图片 {order}/{len(images)}")

    async def _thumbnail(self, image: Dict[str, Any], folder: Path, key: str) -> Optional[Path]:
        target = folder / f"{key}.png"
        if nonempty_file(target):
            return target
        headers = self.ctx.client.media_headers()
        for url in dict.fromkeys([image.get("preview_url") or "", *image.get("urls", [])]):
            if not url:
                continue
            source = folder / f"{key}.source{_image_ext(url)}"
            try:
                saved = await self.ctx.downloader.fetcher.fetch([(url, headers)], source,
                                                                segmented=False,
                                                                image_suffix=True)
                await asyncio.to_thread(self._to_png, saved, target)
                return target
            except Exception:
                continue
            finally:
                for leftover in folder.glob(f"{key}.source*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        return None

    @staticmethod
    def _to_png(source: Path, target: Path) -> None:
        from PIL import Image

        with Image.open(source) as picture:
            picture.save(target, format="PNG")

    # ── saving and zooming images ───────────────────────────────────

    def download_images(self, keys: Sequence[str], save_dir: str) -> bool:
        chosen = [self.images[key] for key in keys if key in self.images]
        root = Path(str(save_dir or "").strip() or self.ctx.settings.download_root)
        aweme_id = self.aweme_id or "unknown"
        target = root if root.name == aweme_id and root.parent.name == "评论图片" \
            else root / aweme_id if root.name == "评论图片" else root / "评论图片" / aweme_id

        async def run(_task) -> None:
            events = self.ctx.events
            events.emit("comment_busy", busy=True)
            success = failed = 0
            try:
                target.mkdir(parents=True, exist_ok=True)
                for index, image in enumerate(chosen, start=1):
                    stem = f"{image['comment_id']}_{int(image['index']) + 1}"
                    if await self._save_original(image, target, stem):
                        success += 1
                    else:
                        failed += 1
                    events.emit("comment_download_progress", current=index, total=len(chosen))
            finally:
                events.emit("comment_download_done", success=success, failed=failed,
                            path=str(target))
                events.emit("comment_busy", busy=False)

        self.ctx.tasks.submit("comment-images", run, scope="comments", lane="comments",
                              key="comment-images:" + ",".join(sorted(keys)))
        return True

    async def _save_original(self, image: Dict[str, Any], folder: Path, stem: str
                             ) -> Optional[Path]:
        headers = self.ctx.client.media_headers()
        urls_ = image.get("urls") or [image.get("preview_url")]
        try:
            still = await self.ctx.downloader.fetcher.fetch(
                [(url, headers) for url in urls_ if url], folder / f"{stem}{_image_ext(urls_[0])}",
                segmented=False, image_suffix=True)
        except Exception:
            return None
        clips = image.get("live_video_urls") or []
        if clips:
            try:
                video = await self.ctx.downloader.fetcher.fetch(
                    [(url, headers) for url in clips], folder / f"{stem}_live.mp4")
                await asyncio.to_thread(live_photo.merge_live_photo_files, still, video,
                                        folder / f"{stem}_live.jpg")
            except Exception:
                pass
        return still

    def zoom(self, key: str) -> bool:
        image = self.images.get(key)
        if image is None:
            return False
        folder = self.ctx.cache_dir / "comments" / (self.aweme_id or "unknown") / "zoom"

        async def run(_task) -> None:
            existing = next((path for path in folder.glob(f"{key}.*") if nonempty_file(path)),
                            None) if folder.exists() else None
            saved = existing or await self._save_original(image, folder, key)
            if saved:
                self.ctx.events.emit("comment_zoom", ok=True, src=image_data_url(saved, 20 << 20),
                                     text="")
            else:
                self.ctx.events.emit("comment_zoom", ok=False, src="", text="原图下载失败")

        folder.mkdir(parents=True, exist_ok=True)
        self.ctx.tasks.submit("comment-zoom", run, scope="comments", lane="comment-zoom",
                              key=f"comment-zoom:{key}")
        return True

    # ── background sync of creators with comments on ──────────────

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._work_loop())

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.ctx.tasks.cancel_where(lambda task: task.name == "comments-sync")

    def kick(self) -> None:
        """Look for works to read now (a switch was turned on, a scan finished)."""
        self._wake.set()

    def cancel_sources(self, source_ids: Sequence[int]) -> None:
        """Stop reading a work of these creators (their switch was turned off)."""
        current = self._current
        if current is not None and int(current["source_id"]) in {int(v) for v in source_ids}:
            self.ctx.tasks.cancel(f"comments-sync:{current['aweme_id']}")

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _set_phase(self, phase: str, until: float = 0.0) -> None:
        self._phase, self._phase_until = phase, float(until)

    async def _work_loop(self) -> None:
        state = await self.ctx.db.read(comment_store.pause_state)
        self._pause_until, self._pause_level = state["until"], state["level"]
        due = {"refresh_after": REFRESH_AFTER, "retry_after": RETRY_AFTER}
        full_due = {"refresh_after": FULL_REFRESH_AFTER, "retry_after": RETRY_AFTER,
                    "grow_min": FULL_GROW_MIN}
        done: set = set()       # works handled since the queue was last empty
        while True:
            try:
                paused = self.ctx.scheduler
                if not self.ctx.settings.logged_in or paused.login_paused:
                    self._set_phase("login")
                    await self._idle(60)
                    continue
                if not paused.warm():
                    self._set_phase("starting")
                    await self._idle(60)
                    continue
                # The first pass always goes first; the second fills the time left.
                work = await self.ctx.db.read(comment_store.next_due, **due)
                run = self._sync_work
                if work is None:
                    work = await self.ctx.db.read(comment_store.next_full, **full_due)
                    run = self._full_slice
                if work is None:
                    if done:
                        self.ctx.events.log(f"评论同步：本轮 {len(done)} 个作品已处理完，等待评论数变化。")
                        done = set()
                    self._set_phase("idle")
                    await self._idle(IDLE_SECONDS)
                    continue
                self._set_phase("reading")
                task = self.ctx.tasks.submit(
                    "comments-sync", lambda _task, work=work, run=run: run(work),
                    scope="comments-sync", lane="comments-sync",
                    key=f"comments-sync:{work['aweme_id']}", kind="automatic", priority=30)
                await task.done_event.wait()
                done.add(str(work["aweme_id"]))
                gap = random.uniform(*WORK_GAP)
                self._set_phase("gap", time.time() + gap)
                await asyncio.sleep(gap)
            except asyncio.CancelledError:
                self._set_phase("idle")
                raise
            except Exception as exc:
                self.ctx.events.log(f"评论同步异常：{explain(exc)}")
                await asyncio.sleep(60)

    async def board(self, limit: int = 30) -> Dict[str, Any]:
        """The download panel's 评论 tab: the work being read, then the queue.

        The first pass's works come first; ``stage`` tells them ("first")
        from the works waiting for all their comments ("full").
        """
        db = self.ctx.db
        due = {"refresh_after": REFRESH_AFTER, "retry_after": RETRY_AFTER}
        full_due = {"refresh_after": FULL_REFRESH_AFTER, "retry_after": RETRY_AFTER,
                    "grow_min": FULL_GROW_MIN}
        queued = [(row, "first") for row in
                  await db.read(comment_store.due_list, **due, limit=limit)]
        if len(queued) < limit:
            queued += [(row, "full") for row in
                       await db.read(comment_store.full_list, **full_due, limit=limit)]
        first_total = await db.read(comment_store.due_count, **due)
        full_total = await db.read(comment_store.full_count, **full_due)
        total = first_total + full_total
        current = self._current
        rows = []
        if current is not None:
            rows.append({"aweme_id": str(current["aweme_id"]),
                         "title": str(current.get("title") or ""),
                         "creator": str(current.get("nickname") or ""),
                         "comment_count": to_int(current.get("comment_count"), -1),
                         "read": self._read, "state": "running", "stage": self._stage})
        rows += [{"aweme_id": str(row["aweme_id"]), "title": str(row.get("title") or ""),
                  "creator": str(row.get("nickname") or ""),
                  "comment_count": to_int(row.get("comment_count"), -1),
                  "read": to_int(row.get("saved")), "state": "queued", "stage": stage}
                 for row, stage in queued if current is None
                 or str(row["aweme_id"]) != str(current["aweme_id"])]
        rows = rows[:limit]
        now = time.time()
        while self._sent and self._sent[0] < now - 3600:
            self._sent.popleft()
        phase, until = self._phase, self._phase_until
        if self._pause_until > now:
            phase, until = "paused", self._pause_until
        return {"rows": rows, "pending": total + (current is not None),
                "remaining": max(0, total + (current is not None) - len(rows)),
                "phase": phase, "countdown": max(0, int(until - now + 0.999)) if until else 0,
                "hour_used": len(self._sent),
                "limit": COMMENT_LIMIT, "reply_limit": REPLY_LIMIT,
                "first_pending": first_total, "full_pending": full_total}

    async def _sync_work(self, work: Dict[str, Any]) -> None:
        scheduler.set_priority(scheduler.BACKGROUND)
        aweme_id = str(work["aweme_id"])
        name = str(work.get("nickname") or work["sec_uid"])
        events = self.ctx.events
        if not await self.ctx.db.write(comment_store.claim_sync, aweme_id, force=True):
            return
        self._current, self._read, self._stage = work, 0, "first"
        started = time.monotonic()

        def read(count: int) -> None:
            self._read = count

        try:
            # A scan saw no comments: nothing to read.
            comments = [] if to_int(work.get("comment_count"), -1) == 0 \
                else await self.collect(aweme_id, read, paced=True, limit=COMMENT_LIMIT,
                                        reply_limit=REPLY_LIMIT)
            folder = self._media_folder(work.get("path"))
            if folder is not None:
                await self._save_media(folder, comments)
            # Merged: what the second pass read of this work stays.
            await self._save(aweme_id, comments, replace=False)
            size = await self._write_file(aweme_id, work.get("path"))
            await self.ctx.db.write(comment_store.finish_sync, aweme_id, success=True,
                                    visibility="visible")
            if comments:
                count = sum(1 + len(item.get("_replies") or []) for item in comments)
                minutes = (time.monotonic() - started) / 60
                events.log(f"评论已保存：{name} · 作品 {aweme_id} · {count} 条 · "
                           f"{size / 1024:.0f} KB · 用时 {minutes:.1f} 分钟")
            if self._pause_level > 0:     # a whole work without risk: back off less next time
                self._pause_level -= 1
                await self.ctx.db.write(comment_store.set_pause, self._pause_until,
                                        self._pause_level)
        except asyncio.CancelledError:
            await self.ctx.db.write(comment_store.release_sync, aweme_id)
            raise
        except LoginRequired:
            await self.ctx.db.write(comment_store.finish_sync, aweme_id, success=False,
                                    error="登录已失效")
            events.log("评论同步暂停：登录已失效，重新登录后继续。")
        except Exception as exc:
            await self.ctx.db.write(comment_store.finish_sync, aweme_id, success=False,
                                    error=explain(exc))
            events.log(f"{name}：作品 {aweme_id} 评论同步失败（6 小时后重试）：{explain(exc)}")
        finally:
            self._current = None

    async def _full_slice(self, work: Dict[str, Any]) -> None:
        """Read the rest of one work's comments, every reply included.

        Resumes at the saved cursor, saves each page as it comes, and gives
        way (to be resumed later) as soon as a work needs its first pass.
        """
        scheduler.set_priority(scheduler.BACKGROUND)
        db, events = self.ctx.db, self.ctx.events
        aweme_id = str(work["aweme_id"])
        name = str(work.get("nickname") or work["sec_uid"])
        cursor = await db.write(comment_store.claim_full, aweme_id)
        if cursor is None:
            return
        due = {"refresh_after": REFRESH_AFTER, "retry_after": RETRY_AFTER}
        self._current, self._stage = work, "full"
        self._read = to_int(work.get("saved"))
        client, page_size = self.ctx.client, self.ctx.settings.comments_page_size
        folder = self._media_folder(work.get("path"))
        guard, pages = CursorGuard(cursor), 0
        try:
            while True:
                page = await self._paced_page(lambda cursor=cursor: client.comments(
                    aweme_id, cursor, page_size, gentle=True), "评论接口")
                batch = []
                for position, raw in enumerate(page.dict_items):
                    comment = dict(raw)
                    comment["cid"] = comment_id(raw, f"{cursor}_{position}")
                    comment["_order"] = cursor + position
                    if to_int(raw.get("reply_comment_total")) > 0:
                        comment["_replies"] = await self._replies(
                            aweme_id, comment["cid"], page_size, set(), self._paced_page, True)
                    batch.append(comment)
                if folder is not None:
                    await self._save_media(folder, batch)
                await self._save(aweme_id, batch, replace=False)
                self._read = await db.read(comment_store.saved_count, aweme_id)
                pages += 1
                if not page.has_more:
                    size = await self._write_file(aweme_id, work.get("path"))
                    await db.write(comment_store.finish_full, aweme_id, done=True)
                    events.log(f"评论已补全：{name} · 作品 {aweme_id} · 共 {self._read} 条 "
                               f"（抖音显示 {to_int(work.get('comment_count'), -1)}）· "
                               f"{size / 1024:.0f} KB")
                    return
                cursor = guard.advance(page.cursor, "评论")
                await db.write(comment_store.full_progress, aweme_id, cursor)
                if pages % FULL_WRITE_EVERY == 0:
                    await self._write_file(aweme_id, work.get("path"))
                if await db.read(comment_store.next_due, **due) is not None:
                    break       # a work waits for its first pass: it goes first
            await self._write_file(aweme_id, work.get("path"))
            await db.write(comment_store.finish_full, aweme_id, done=False)
        except asyncio.CancelledError:
            await db.write(comment_store.finish_full, aweme_id, done=False)
            raise
        except LoginRequired:
            await db.write(comment_store.finish_full, aweme_id, done=False)
            events.log("评论同步暂停：登录已失效，重新登录后继续。")
        except Exception as exc:
            if pages:
                await self._write_file(aweme_id, work.get("path"))
            await db.write(comment_store.finish_full, aweme_id, done=False, error=explain(exc))
            events.log(f"{name}：作品 {aweme_id} 评论补全中断（已存 {self._read} 条，"
                       f"6 小时后从断点继续）：{explain(exc)}")
        finally:
            self._current = None

    async def _paced_page(self, call, what: str) -> Page:
        """One comment request at a person's pace; a risk signal pauses for hours."""
        while True:
            await self._pace()
            try:
                return checked(await call(), what)
            except LoginRequired:
                raise
            except DouyinError as exc:
                if not is_risk(exc):
                    raise
                if isinstance(exc, EmptyResponse) and what == "回复接口":
                    raise       # the caller skips these replies; no pause
                await self._risk_pause(exc)

    async def _pace(self) -> None:
        """Wait for the risk pause and the gap after the last request."""
        while True:
            now = time.time()
            while self._sent and self._sent[0] < now - 3600:
                self._sent.popleft()
            until = max(self._pause_until, self._next_request)
            if until <= now:
                break
            if self._pause_until <= now:
                self._set_phase("reading", until)
            await asyncio.sleep(min(until - now, 60.0))
        self._set_phase("reading")
        self._sent.append(time.time())
        self._next_request = time.time() + random.uniform(*PAGE_GAP)

    async def _risk_pause(self, exc: BaseException) -> None:
        self._pause_level += 1
        seconds = min(PAUSE_MAX, PAUSE_BASE * 2 ** (self._pause_level - 1))
        self._pause_until = int(time.time() + seconds)
        await self.ctx.db.write(comment_store.set_pause, self._pause_until, self._pause_level)
        resume = time.strftime("%m-%d %H:%M", time.localtime(self._pause_until))
        self.ctx.events.log(f"评论接口触发风控（{explain(exc)}），评论同步暂停 {seconds / 3600:.0f} "
                            f"小时，{resume} 从当前位置继续；扫描和下载不受影响。")

    @staticmethod
    def _stem(anchor_path: Optional[str]) -> Optional[Path]:
        """``folder/name`` of a work's files (its ``_data.json`` or media)."""
        if not anchor_path:
            return None
        anchor = Path(anchor_path)
        name = anchor.name[:-len("_data.json")] if anchor.name.endswith("_data.json") \
            else anchor.stem
        return anchor.with_name(name)

    def _media_folder(self, anchor_path: Optional[str]) -> Optional[Path]:
        stem = self._stem(anchor_path)
        return stem.with_name(f"{stem.name}_comments") if stem is not None else None

    async def _write_file(self, aweme_id: str, anchor_path: Optional[str]) -> int:
        """Write every saved comment to ``*_comments.json`` beside the work.

        The file is rebuilt from the database (both passes merged, in
        Douyin's order, replies under their comment) and recorded with the
        comment pictures; returns their size.
        """
        stem = self._stem(anchor_path)
        if stem is None:
            return 0
        target = stem.with_name(f"{stem.name}_comments.json")
        saved = await self.ctx.db.read(comment_store.load, aweme_id)
        if not saved["comments"] and not target.exists():
            return 0
        comments: List[Dict[str, Any]] = []
        replies: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        for row in saved["comments"]:
            try:
                raw = json.loads(row["raw"] or "{}")
            except ValueError:
                raw = {}
            raw["cid"] = row["cid"]
            if row["is_reply"]:
                replies[row["parent_cid"]].append(raw)
            else:
                comments.append(raw)
        for comment in comments:
            if replies.get(comment["cid"]):
                comment["_replies"] = replies[comment["cid"]]
        await asyncio.to_thread(atomic_write_json, target,
                                {"aweme_id": aweme_id, "count": len(comments),
                                 "total": len(saved["comments"]),
                                 "include_replies": True, "comments": comments})
        folder = self._media_folder(anchor_path)
        media = [path for path in folder.iterdir() if nonempty_file(path)] \
            if folder is not None and folder.exists() else []
        files = [{"path": str(path), "kind": "comments", "size": path.stat().st_size}
                 for path in [target, *media] if path.exists()]
        await self.ctx.db.write(work_store.save_files, aweme_id, files)
        return sum(entry["size"] for entry in files)

    async def _save_media(self, folder: Path, comments: List[Dict[str, Any]]) -> List[Path]:
        """Download the pictures and stickers of these comments and replies.

        Each comment gets ``_files``: the names of its files in ``folder``.
        Files already there are kept (a refresh downloads only new ones).
        """
        saved: List[Path] = []
        headers = self.ctx.client.media_headers()
        for comment, _is_reply, _parent in self.flatten(comments):
            cid = comment["cid"]
            names: List[str] = []
            for image in comment_images(comment, cid):
                stem = f"{cid}_{int(image['index']) + 1}"
                files = _stem_files(folder, stem) or await self._original_files(image, folder,
                                                                                 stem)
                saved += files
                names += [path.name for path in files]
            sticker = comment_sticker(comment)
            if sticker:
                stem = f"{cid}_sticker"
                existing = next((path for path in folder.glob(f"{stem}.*")
                                 if nonempty_file(path)), None) if folder.exists() else None
                if existing is None:
                    try:
                        existing = await self.ctx.downloader.fetcher.fetch(
                            [(url, headers) for url in sticker],
                            folder / f"{stem}{_image_ext(sticker[0])}",
                            segmented=False, image_suffix=True)
                    except Exception:
                        existing = None
                if existing is not None:
                    saved.append(existing)
                    names.append(existing.name)
            if names:
                comment["_files"] = names
        return saved

    async def _original_files(self, image: Dict[str, Any], folder: Path, stem: str
                              ) -> List[Path]:
        still = await self._save_original(image, folder, stem)
        return _stem_files(folder, stem) if still is not None else []
