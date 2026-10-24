"""An account's own content: browsing its works/likes, and downloading by mode.

Browsing streams listing pages to the account homepage in API order and
marks which works are already saved locally.  Downloading walks the chosen
mode's listing and saves works one at a time through the shared engine; a
finite download is checkpointed so it can be resumed after a restart.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from douyin import aweme as aw
from douyin.client import use_account
from services import views
from services.common import explain, pages, resolve_profile
from services.context import Context
from services.foreground import DOWNLOADED, EXISTS, download_one
from storage import works as work_store
from storage.files import DownloadContext

MODES = ("post", "like", "mix", "music", "collect", "collectmix")
_generations = itertools.count(1)


def _day_range(start: str, end: str) -> Tuple[int, int]:
    low = int(datetime.strptime(start, "%Y-%m-%d").timestamp()) if start else 0
    high = int((datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp()) \
        if end else 0
    return low, high


class _Reporter:
    """Per-item and scan events of one account download."""

    def __init__(self, ctx: Context, owner: str, mode: str):
        self.ctx = ctx
        self.base = {"sec_uid": owner, "mode": mode, "generation": next(_generations)}
        self._revision = itertools.count(1)
        self._sequence = itertools.count(1)
        self.states: Dict[str, str] = {}
        self.scan = {"scan_state": "scanning", "scanned": 0, "accepted": 0, "pages": 0,
                     "total_hint": 0, "error": ""}

    def item(self, aweme_id: str, state: str, error: str = "", **extra: Any) -> None:
        self.states[aweme_id] = state
        self.ctx.events.emit("user_item_state", **self.base, revision=next(self._revision),
                             sequence=next(self._sequence), aweme_id=aweme_id, state=state,
                             error=error, **extra)

    def file(self, aweme_id: str, name: str, written: int, expected: Optional[int],
             rate: int) -> None:
        self.ctx.events.emit("user_file_progress", **self.base, revision=next(self._revision),
                             aweme_id=aweme_id, name=name, written=int(written),
                             expected=expected, rate=int(rate))

    def scanning(self, **values: Any) -> None:
        self.scan.update(values)
        self.ctx.events.emit("user_download_scan", **self.base, **self.scan,
                             revision=next(self._revision))

    def finish(self, *, cancelled: bool = False, error: str = "") -> None:
        for aweme_id, state in list(self.states.items()):
            if state in ("queued", "downloading"):
                self.item(aweme_id, "cancelled" if cancelled else "failed", error or "任务未完成")
        state = "cancelled" if cancelled else "failed" if error else "completed"
        if self.scan["scan_state"] == "scanning":
            self.scan["scan_state"] = state
        self.ctx.events.emit("user_download_scan", **self.base,
                             **{**self.scan, "error": error or self.scan["error"]},
                             revision=next(self._revision), download_state=state)


class UserContentService:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.request_id = ""
        self._client_order: Dict[str, int] = {}
        self._task_key = ""
        self.catalogue: Dict[str, str] = {}   # aweme_id -> author sec_uid

    # ── browsing ────────────────────────────────────────────────────

    def browse(self, account: Dict[str, Any], mode: str, request_id: str, client_id: str,
               order: int) -> bool:
        if not account.get("is_self") or not account.get("user_info"):
            raise ValueError("请先确认自己的账号；其他账号完整主页尚未开放")
        if mode not in ("post", "like"):
            raise ValueError("当前主页浏览仅支持作品和喜欢")
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("无效的主页请求标识")
        if client_id and order <= self._client_order.get(client_id, -1):
            return request_id == self.request_id
        if client_id:
            self._client_order[client_id] = order
        self.cancel_browse()
        self.request_id = request_id
        self.catalogue = {}
        owner = str(account["sec_uid"])
        base = {"request_id": request_id, "sec_uid": owner, "mode": mode}
        total_hint = int(account.get("favoriting_count" if mode == "like" else "aweme_count")
                         or 0)
        fetch = (lambda cursor: self.ctx.client.user_likes(owner, cursor, 20)) if mode == "like" \
            else (lambda cursor: self.ctx.client.user_posts(owner, cursor, 20))
        viewer = str(account.get("viewer") or "")

        async def run(_task) -> None:
            use_account(viewer)     # a library account browses with its own login
            events = self.ctx.events
            scanned, index, position = 0, 0, 0
            seen = set()
            try:
                async for page in pages(fetch, what="账号内容"):
                    if request_id != self.request_id:
                        raise asyncio.CancelledError()
                    works = []
                    for raw in page.items:
                        rank = position
                        position += 1
                        payload = aw.extract(raw) if isinstance(raw, dict) else None
                        aweme_id = aw.aweme_id(payload) if payload else ""
                        if not aweme_id or aweme_id in seen:
                            continue
                        seen.add(aweme_id)
                        author = aw.author(payload)
                        self.catalogue[aweme_id] = str(author.get("sec_uid") or "")
                        work = views.remote_work(payload, str(author.get("sec_uid") or ""))
                        work.update(source_index=rank, catalogue=True, download_status="unknown",
                                    author_profile=views.creator_payload({}, author))
                        works.append(work)
                    scanned += len(works)
                    events.emit("user_catalogue_page", **base, page_index=index,
                                next_cursor=page.cursor, has_more=page.has_more, works=works,
                                scanned=scanned, pages=index + 1, total_hint=total_hint)
                    index += 1
                    ids = [work["aweme_id"] for work in works]
                    done = await self.ctx.db.read(work_store.statuses, ids)
                    events.emit("user_catalogue_local_states", **base, states={
                        aweme_id: "downloaded" if done.get(aweme_id) == "done"
                        else "not_downloaded" for aweme_id in ids})
                events.emit("user_catalogue_state", **base, state="done", count=scanned)
            except asyncio.CancelledError:
                events.emit("user_catalogue_state", **base, state="cancelled")
                raise
            except Exception as exc:
                events.emit("user_catalogue_state", **base, state="failed", error=explain(exc))

        self._task_key = f"account-preview:{request_id}"
        self.ctx.tasks.submit("user-preview-stream", run, scope="user-preview",
                              lane="user-preview", key=self._task_key)
        return True

    def cancel_browse(self, request_id: str = "") -> bool:
        if request_id and request_id != self.request_id:
            return False
        return bool(self._task_key) and self.ctx.tasks.cancel(self._task_key)

    # ── downloading ─────────────────────────────────────────────────

    def download(self, account: Dict[str, Any], options: Dict[str, Any]) -> bool:
        mode = str(options.get("mode") or "post")
        if mode not in MODES:
            raise ValueError("不支持的下载模式")
        is_self = bool(account.get("is_self"))
        if mode in ("collect", "collectmix") and not is_self:
            raise ValueError("收藏和收藏的合集只能下载自己的账号")
        owner = str(account.get("sec_uid") or "")
        if not owner:
            raise ValueError("账号信息不完整，请重新确认账号")
        viewer = str(account.get("viewer") or "")
        limit = max(0, int(options.get("limit") or 0))
        increase = bool(options.get("increase")) and mode not in ("collect", "collectmix")
        start, end = str(options.get("start_time") or ""), str(options.get("end_time") or "")
        low, high = _day_range(start, end)
        settings = self.ctx.settings
        settings.update(mode=[mode], number={mode: limit}, increase={mode: increase})
        checkpoint = uuid.uuid4().hex
        settings.update_ui(pending_user_download={
            "id": checkpoint, "sec_uid": owner, "is_self": is_self, "mode": mode,
            "viewer": viewer,
            "limit": limit, "remaining_limit": limit, "increase": increase,
            "path": str(settings.download_root), "start_time": start, "end_time": end})
        context = DownloadContext("self" if is_self else "user",
                                  "我的内容" if is_self else str(account.get("nickname") or ""),
                                  owner, mode, source_uid=str(account.get("uid") or "") or None)
        reporter = _Reporter(self.ctx, owner, mode)
        total_hint = int(account.get("favoriting_count" if mode == "like" else "aweme_count")
                         or 0)

        async def run(_task) -> None:
            use_account(viewer)
            events = self.ctx.events
            events.emit("busy", scope="user", busy=True)
            totals = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
            error, cancelled = "", False
            try:
                with self.ctx.tasks.hold_foreground():
                    await self._walk(owner, mode, context, reporter, totals, limit=limit,
                                     increase=increase, window=(low, high),
                                     total_hint=total_hint, checkpoint=checkpoint)
            except asyncio.CancelledError:
                cancelled = True
                events.emit("user_download_cancelled", scope="user")
                raise
            except Exception as exc:
                error = explain(exc)
                events.log(f"用户作品下载失败：{error}")
                events.emit("error", scope="user", text=error)
            finally:
                reporter.finish(cancelled=cancelled, error=error)
                events.emit("busy", scope="user", busy=False)
            if not totals["failed"]:
                self._clear_checkpoint(checkpoint)
            events.emit("user_download_done", **totals)

        self.ctx.tasks.submit("user-download", run, scope="user", lane="user",
                              key=f"user-download:{owner}:{mode}")
        return True

    def resume_pending(self) -> bool:
        """Continue the download the previous session left unfinished.

        Runs at start-up without asking: the walk skips works already
        downloaded, so it simply carries on where it stopped.
        """
        pending = self.ctx.settings.ui().get("pending_user_download") or {}
        if not isinstance(pending, dict) or not pending.get("sec_uid")                 or pending.get("mode") not in MODES or not self.ctx.settings.logged_in:
            return False
        owner = str(pending["sec_uid"])
        is_self = bool(pending.get("is_self"))
        viewer = str(pending.get("viewer") or "")

        async def run(_task) -> None:
            use_account(viewer)
            try:
                profile = await resolve_profile(
                    self.ctx.client, None if is_self else f"https://www.douyin.com/user/{owner}")
            except Exception as exc:
                self.ctx.events.log(f"未能继续上次的用户作品下载：{explain(exc)}")
                return
            if profile.get("sec_uid") != owner or bool(profile.get("is_self")) != is_self:
                self.ctx.settings.update_ui(pending_user_download=None)
                return
            profile["viewer"] = viewer
            limit = int(pending.get("limit") or 0)
            label = {"like": "喜欢", "collect": "收藏", "post": "作品", "mix": "合集",
                     "music": "音乐", "collectmix": "收藏的合集"}[pending["mode"]]
            self.ctx.events.log(f"继续上次未完成的「{label}」下载：{profile.get('nickname')}")
            self.download(profile, {
                "mode": pending["mode"],
                "limit": int(pending.get("remaining_limit") or 0) if limit else 0,
                "increase": bool(pending.get("increase")),
                "start_time": pending.get("start_time") or "",
                "end_time": pending.get("end_time") or ""})

        self.ctx.tasks.submit("user-download-resume", run, scope="user", lane="user-resume",
                              key="user-download-resume")
        return True

    def _clear_checkpoint(self, checkpoint: str) -> None:
        pending = self.ctx.settings.ui().get("pending_user_download") or {}
        if isinstance(pending, dict) and pending.get("id") == checkpoint:
            self.ctx.settings.update_ui(pending_user_download=None)

    def _advance_checkpoint(self, checkpoint: str) -> None:
        pending = self.ctx.settings.ui().get("pending_user_download") or {}
        if not isinstance(pending, dict) or pending.get("id") != checkpoint:
            return
        remaining = int(pending.get("remaining_limit") or 0)
        if remaining > 0:
            pending["remaining_limit"] = remaining - 1
            self.ctx.settings.update_ui(
                pending_user_download=pending if pending["remaining_limit"] else None)

    async def _walk(self, owner, mode, context, reporter, totals, *, limit, increase, window,
                    total_hint, checkpoint) -> None:
        low, high = window
        accepted = scanned = page_count = 0
        async for batch in self._listing(owner, mode):
            page_count += 1
            scanned += len(batch)
            ids = [aw.aweme_id(payload) for payload, _extra in batch]
            done = await self.ctx.db.read(work_store.statuses, ids)
            todo = []
            for payload, extra in batch:
                aweme_id = aw.aweme_id(payload)
                stamp = aw.create_time(payload)
                if (low and stamp < low) or (high and stamp >= high):
                    continue
                if limit and accepted >= limit:
                    break
                if increase and done.get(aweme_id) == "done":
                    reporter.item(aweme_id, "downloaded", outcome="skipped")
                    totals["total"] += 1
                    totals["skipped"] += 1
                    continue
                accepted += 1
                todo.append((payload, extra))
                reporter.item(aweme_id, "queued", title=aw.title(payload),
                              creator=str(aw.author(payload).get("nickname") or ""))
            reporter.scanning(scanned=scanned, accepted=accepted, pages=page_count,
                              total_hint=total_hint)
            for payload, extra in todo:
                await self._save(payload, extra, context, reporter, totals, checkpoint,
                                 bool(limit))
            if increase and batch and all(done.get(aw.aweme_id(p)) == "done" for p, _ in batch):
                break  # a whole page already saved: the rest is older
            if limit and accepted >= limit:
                break
        reporter.scanning(scan_state="completed", scanned=scanned, accepted=accepted,
                          pages=page_count, total_hint=total_hint)

    async def _save(self, payload, extra, context, reporter, totals, checkpoint, finite) -> None:
        aweme_id = aw.aweme_id(payload)
        item_context = DownloadContext(**{**context.to_dict(), **extra}) if extra else context
        reporter.item(aweme_id, "downloading")
        totals["total"] += 1
        outcome, error = await download_one(
            self.ctx, aweme_id, payload, item_context,
            on_file=lambda name, written, expected, rate: reporter.file(
                aweme_id, name, written, expected, rate))
        if outcome == DOWNLOADED:
            totals["success"] += 1
            reporter.item(aweme_id, "downloaded")
        elif outcome == EXISTS:
            totals["skipped"] += 1
            reporter.item(aweme_id, "downloaded", outcome="skipped")
        else:
            totals["failed"] += 1
            reporter.item(aweme_id, "failed", error)
            self.ctx.events.log(f"作品 {aweme_id} 下载失败：{error}")
        if outcome != "failed" and finite:
            self._advance_checkpoint(checkpoint)

    async def _listing(self, owner: str, mode: str
                       ) -> AsyncIterator[List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        """Pages of ``(payload, extra context)`` for one download mode."""
        client = self.ctx.client

        def plain(fetch):
            async def walk():
                async for page in pages(fetch, what="账号内容"):
                    yield [(payload, {}) for payload in map(aw.extract, page.items) if payload]
            return walk()

        if mode == "post":
            async for batch in plain(lambda cursor: client.user_posts(owner, cursor, 20)):
                yield batch
        elif mode == "like":
            async for batch in plain(lambda cursor: client.user_likes(owner, cursor, 20)):
                yield batch
        elif mode == "collect":
            async for batch in plain(lambda cursor: client.self_collection(cursor, 20)):
                yield [(payload, {"collection_name": "默认收藏"}) for payload, _ in batch]
        elif mode in ("mix", "collectmix"):
            fetch = (lambda cursor: client.user_mixes(owner, cursor, 20)) if mode == "mix" \
                else (lambda cursor: client.self_collected_mixes(cursor, 12))
            async for page in pages(fetch, what="合集列表"):
                for entry in page.dict_items:
                    info = entry.get("mix_info") if isinstance(entry.get("mix_info"), dict) \
                        else entry
                    mix_id = str(info.get("mix_id") or info.get("id") or "")
                    if not mix_id:
                        continue
                    name = str(info.get("mix_name") or info.get("name") or mix_id)
                    extra = {"collection_id": mix_id, "collection_name": name}
                    async for mix_page in pages(lambda cursor, m=mix_id: client.mix_works(
                            m, cursor), what="合集作品"):
                        yield [(payload, extra) for payload in map(aw.extract, mix_page.items)
                               if payload]
        elif mode == "music":
            async for page in pages(lambda cursor: client.user_music(owner, cursor, 20),
                                    what="音乐列表"):
                for entry in page.dict_items:
                    music_id = str(entry.get("id_str") or entry.get("id")
                                   or entry.get("music_id") or "")
                    if music_id:
                        async for music_page in pages(lambda cursor, m=music_id: client.
                                                      music_works(m, cursor), what="音乐作品"):
                            yield [(payload, {}) for payload in map(aw.extract, music_page.items)
                                   if payload]

