"""The background download queue shared by creator monitoring and subscriptions.

It reads due work straight from the ledger (``storage.works``), so every
scan anywhere only has to queue a work; the queue decides when:

1. works of monitored creators — round-robin across creators, newest first;
2. works of like/collect subscriptions — only when no creator work is due;
   once one is, a running subscription download is put back to make room;
3. missing covers/soundtracks of finished works — only when nothing else is.

``works`` (1–3, a setting) works download at a time.  A video larger than
``LARGE_VIDEO_BYTES`` moves to one of ``LARGE_SLOTS`` extra slots so it does
not hold the main lane for minutes.  While the user runs a download of
their own, the queue starts nothing new.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from common.fs import nonempty_file
from common.log import get_logger
from douyin import aweme as aw
from douyin.errors import LoginRequired
from engine import scheduler
from engine.downloader import (
    PERMANENT,
    SUCCESS,
    WorkDownloader,
    WorkRequest,
    WorkResult,
    retry_delay,
)
from engine.events import EventBus
from engine.fetcher import TransferProgress, current_progress
from engine.runtime import TaskManager
from storage import sources as source_store
from storage import works as work_store
from storage.db import Database
from storage.files import DownloadContext, Layout

logger = get_logger(__name__)

LARGE_VIDEO_BYTES = 200 * 1000 * 1000
LARGE_SLOTS = 2
IDLE_SECONDS = 2.0
LOGIN_PAUSE_SECONDS = 300
COUNTS_TTL = 5.0            # the queue sizes are re-counted at most this often
# A scan hands over each listed work's full payload (with signed media URLs).
# Within this age the download uses it directly instead of asking the detail
# API; the URLs stay valid for hours, and an expired one is refreshed through
# the browser like any other failed media download.
LISTING_TTL = 2 * 3600
LISTING_KEEP = 5000         # payloads remembered at most (oldest dropped first)
THROTTLE_PAUSE_MIN = 30     # seconds the queue waits once detail requests are throttled
PREEMPT_GAP = 30.0          # at most one subscription download is put back this often
SUBSCRIPTION_KINDS = ("like", "collect")


def source_context(source: Dict[str, Any]) -> DownloadContext:
    """Where a source's works are saved."""
    if source.get("kind") == "creator":
        return DownloadContext("following", str(source.get("nickname") or source["sec_uid"]),
                               str(source["sec_uid"]), "post",
                               source_uid=str(source.get("uid") or "") or None)
    return DownloadContext("subscription", str(source.get("nickname") or source["sec_uid"]),
                           str(source["sec_uid"]), str(source.get("kind") or "like"))


@dataclass
class _Active:
    aweme_id: str
    source_id: int
    title: str
    creator: str
    progress: TransferProgress = field(default_factory=TransferProgress)
    lane: str = "main"
    extras: bool = False
    request: Optional[WorkRequest] = None
    source: Optional[Dict[str, Any]] = None
    preempted: bool = False

    @property
    def creator_work(self) -> bool:
        """Shown in the 下载 tab: creator works and the extras chores."""
        return self.extras or str((self.source or {}).get("kind") or "") == "creator"


class DownloadQueue:
    def __init__(self, db: Database, downloader: WorkDownloader, events: EventBus,
                 tasks: TaskManager, *, layout: Callable[[], Layout],
                 concurrency: Callable[[], int], homepage_screenshot: Callable[[], bool],
                 browser: Any = None, scheduler: Any = None):
        self.db = db
        self.scheduler = scheduler
        self.downloader = downloader
        self.events = events
        self.tasks = tasks
        self.layout = layout
        self.concurrency = concurrency
        self.homepage_screenshot = homepage_screenshot
        self.browser = browser
        self.listeners: List[Callable[[int, WorkResult], None]] = []
        self._active: Dict[str, _Active] = {}
        self._running: Dict[asyncio.Task, _Active] = {}
        self._wake = asyncio.Event()
        self._stopped = False
        self._paused_until = 0.0
        self._rotation: Dict[str, List[int]] = {"creator": [], "subscription": []}
        # This download round (since the queue was last empty): works started
        # and works finished (downloaded or given up); a retry leaves it.
        self._started = 0
        self._finished = 0
        self._counts: Dict[str, int] = {"ready": 0, "waiting": 0}
        self._listed: "OrderedDict[str, tuple]" = OrderedDict()   # aweme_id -> (at, payload)
        self._counted_at = -COUNTS_TTL
        self._homes_done: Set[str] = set()
        self._loop_task: Optional[asyncio.Task] = None
        self._ready = 0     # works due now and not started, for the round's total
        self._preempted_at = -PREEMPT_GAP

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._stopped = False
            self._loop_task = asyncio.ensure_future(self._serve())

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.wait(list(self._running), timeout=5)
        if self._loop_task is not None:
            self._loop_task.cancel()
        leftovers = list(self._active)
        if leftovers:
            await self.db.write(work_store.release, leftovers)

    def hand_over(self, payloads: List[Dict[str, Any]]) -> None:
        """Full listing payloads from a scan, for the downloads that follow."""
        now = time.monotonic()
        for payload in payloads:
            aweme_id = aw.aweme_id(payload)
            if aweme_id and aw.is_complete(payload):
                self._listed.pop(aweme_id, None)
                self._listed[aweme_id] = (now, payload)
        while len(self._listed) > LISTING_KEEP:
            self._listed.popitem(last=False)

    def _listing(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        entry = self._listed.pop(aweme_id, None)
        if entry is None or time.monotonic() - entry[0] > LISTING_TTL:
            return None
        return entry[1]

    def busy_sources(self) -> Set[int]:
        return {entry.source_id for entry in self._active.values()}

    # ── scheduling ──────────────────────────────────────────────────

    async def _serve(self) -> None:
        while not self._stopped:
            try:
                await self._fill()
            except Exception:
                logger.exception("Download queue step failed")
            await self._publish_progress()
            self._wake.clear()
            waiters = [asyncio.ensure_future(self._wake.wait()), *self._running]
            await asyncio.wait(waiters, timeout=IDLE_SECONDS if not self._running else 1.0,
                               return_when=asyncio.FIRST_COMPLETED)
            waiters[0].cancel()

    async def _fill(self) -> None:
        if time.monotonic() < self._paused_until or self.tasks.foreground_busy:
            return
        if self.scheduler is not None and self.scheduler.paused(scheduler.CREATOR_DOWNLOAD):
            return      # risk control or an expired login: nothing background may start
        await self._make_room()
        while sum(1 for entry in self._running.values() if entry.lane == "main") \
                < self.concurrency():
            picked = await self._claim()
            if picked is None:
                return
            self._running[asyncio.ensure_future(self._download(picked))] = picked

    async def _make_room(self) -> None:
        """Creator works outrank like/collect works: when every main slot runs
        a subscription download and a creator work is due, put one back."""
        main = [(task, entry) for task, entry in self._running.items() if entry.lane == "main"]
        if len(main) < self.concurrency() or any(entry.preempted for _task, entry in main):
            return
        victims = [(task, entry) for task, entry in main if not entry.creator_work]
        if not victims or time.monotonic() - self._preempted_at < PREEMPT_GAP:
            return
        if not await self.db.read(work_store.ready_sources, ("creator",), int(time.time())):
            return
        task, entry = victims[-1]
        entry.preempted = True
        self._preempted_at = time.monotonic()
        self.events.log(f"关注主播有作品要下载，先暂停「{entry.creator or entry.aweme_id}」"
                        f"这个订阅作品，稍后重新排队。")
        task.cancel()

    async def _claim(self) -> Optional[_Active]:
        now = int(time.time())
        busy = set(self._active)
        for group, kinds in (("creator", ("creator",)), ("subscription", ("like", "collect"))):
            ready = await self.db.read(work_store.ready_sources, kinds, now)
            rotation = [source for source in self._rotation[group] if source in ready]
            rotation += [source for source in ready if source not in rotation]
            for _ in range(len(rotation)):
                source_id = rotation.pop(0)
                rotation.append(source_id)
                row = await self.db.read(work_store.next_for_source, source_id, now, busy)
                if row is None:
                    continue
                self._rotation[group] = rotation
                return await self._begin(row, source_id)
            self._rotation[group] = rotation
        row = await self.db.read(work_store.next_extras, now, busy)
        if row is not None:
            return await self._begin_extras(row)
        return None

    async def _begin(self, row: Dict[str, Any], source_id: int) -> Optional[_Active]:
        source = await self.db.read(source_store.get, source_id)
        if source is None:
            return None
        context = source_context(source)
        if not await self.db.write(work_store.claim, row["aweme_id"], source_id,
                                   context.to_dict()):
            return None
        entry = _Active(str(row["aweme_id"]), source_id, str(row.get("title") or ""),
                        str(row.get("author_name") or source.get("nickname") or ""))
        entry.request = WorkRequest(entry.aweme_id, context,
                                    payload=aw.decode(row.get("metadata")),
                                    attempt=int(row.get("attempts") or 0) + 1)
        entry.source = source
        self._active[entry.aweme_id] = entry
        self._started += 1
        self._ready = max(0, self._ready - 1)     # moved from waiting to started
        return entry

    async def _begin_extras(self, row: Dict[str, Any]) -> _Active:
        try:
            context = DownloadContext.from_dict(json.loads(row.get("context") or "{}"))
        except ValueError:
            context = DownloadContext("single_aweme")
        kinds = tuple(kind for kind in str(row["extras_missing"]).split(",") if kind)
        entry = _Active(str(row["aweme_id"]), int(row.get("origin") or 0), "", "", extras=True)
        entry.request = WorkRequest(entry.aweme_id, context,
                                    payload=aw.decode(row.get("metadata")),
                                    attempt=int(row.get("extras_attempts") or 0) + 1,
                                    extras_only=kinds)
        entry.source = None
        self._active[entry.aweme_id] = entry
        return entry

    # ── one work ────────────────────────────────────────────────────

    async def _download(self, entry: _Active) -> None:
        request = entry.request
        current_progress.set(entry.progress)
        kind = str((entry.source or {}).get("kind") or "")
        scheduler.set_priority(scheduler.BACKGROUND if entry.extras
                               else scheduler.CREATOR_DOWNLOAD if kind == "creator"
                               else scheduler.SUBSCRIPTION)
        result: Optional[WorkResult] = None
        try:
            if not entry.extras:
                listed = self._listing(entry.aweme_id)
                if listed is not None:
                    payload, reason, kind = aw.merge_detail(listed, request.payload), "", ""
                else:
                    payload, reason, kind = await self.downloader.fetch_detail(
                        entry.aweme_id, request.payload, gentle=True)
                if payload is None:
                    result = self.downloader.detail_failure(request, reason, kind)
                else:
                    request.payload, request.fresh = payload, True
                    entry.title = aw.title(payload) or entry.title
                    size = aw.estimated_video_bytes(payload, self.downloader.quality())
                    if size > LARGE_VIDEO_BYTES and self._large_free():
                        entry.lane = "large"
                        self.events.log(f"大视频 {size / 1e6:.0f}MB 移到大文件通道："
                                        f"{entry.creator} {entry.aweme_id}")
                        self.wake()
            if result is None:
                result = await self.downloader.download(request)
        except asyncio.CancelledError:
            await self.db.write(work_store.release, [entry.aweme_id])
            if entry.preempted:
                self._started = max(0, self._started - 1)   # back in the queue
                self.wake()
                return
            raise
        except LoginRequired:
            self._paused_until = time.monotonic() + LOGIN_PAUSE_SECONDS
            self.events.log("抖音登录已失效，后台下载暂停 5 分钟；请在设置中重新扫码登录。")
            await self.db.write(work_store.release, [entry.aweme_id])
        finally:
            self._active.pop(entry.aweme_id, None)
            self._running.pop(asyncio.current_task(), None)
        if result is not None:
            if result.code == "THROTTLED":
                self._throttled()
            await self._settle(entry, result)
        self.wake()

    def _throttled(self) -> None:
        """Detail requests are rate-limited: stop claiming works until it clears."""
        browser = self.downloader.browser
        wait = max(THROTTLE_PAUSE_MIN, self.downloader.client.detail_cooldown(),
                   browser.cooldown_remaining() if browser is not None
                   and hasattr(browser, "cooldown_remaining") else 0.0)
        until = time.monotonic() + wait
        if until > self._paused_until + 1:
            self._paused_until = until
            self.events.log(f"作品详情接口被风控限流，后台下载暂停 {wait:.0f} 秒后再继续。")

    def _large_free(self) -> bool:
        return sum(1 for entry in self._running.values() if entry.lane == "large") < LARGE_SLOTS

    async def _settle(self, entry: _Active, result: WorkResult) -> None:
        request = entry.request
        if entry.extras:
            await self.db.write(work_store.record_extras, entry.aweme_id, files=result.files,
                                missing=result.extras_missing, attempts=request.attempt)
            return
        if result.outcome == SUCCESS:
            await self.db.write(work_store.record_success, entry.aweme_id, files=result.files,
                                extras_missing=result.extras_missing,
                                context=request.context.to_dict(), origin=entry.source_id,
                                payload=result.payload)
            self._finished += 1
            if entry.source and entry.source.get("kind") == "creator":
                asyncio.ensure_future(self._creator_home(entry, result))
        else:
            permanent = result.outcome == PERMANENT
            retry_at = 0 if permanent else int(time.time()) + retry_delay(max(1, result.attempt))

            def record(db):
                status = work_store.record_failure(
                    db, entry.aweme_id, error=result.error, permanent=permanent,
                    attempts=result.attempt, retry_at=retry_at, code=result.code)
                if result.code == "INACCESSIBLE" and permanent:
                    work_store.set_visibility(db, entry.aweme_id, "hidden", result.error)
                return status

            await self.db.write(record)
            if permanent:
                self._finished += 1
            else:
                self._started = max(0, self._started - 1)
            self.events.log(f"下载失败（{entry.creator or entry.aweme_id}）：{result.error}")
        for listener in list(self.listeners):
            try:
                listener(entry.source_id, result)
            except Exception:
                logger.exception("queue listener failed")

    async def _creator_home(self, entry: _Active, result: WorkResult) -> None:
        """Save the creator's avatar and homepage screenshot once per session."""
        source = entry.source
        key = str(source.get("sec_uid") or "")
        if not key or key in self._homes_done:
            return
        self._homes_done.add(key)
        try:
            home = self.layout().source_home(source_context(source))
            if home is None:
                return
            avatar = home / "头像.jpg"
            urls = aw.avatar_urls(aw.author(result.payload))
            if urls and not nonempty_file(avatar):
                headers = self.downloader.client.media_headers()
                await self.downloader.fetcher.fetch([(url, headers) for url in urls], avatar,
                                                    segmented=False)
            shot = home / "主页截图.png"
            if self.homepage_screenshot() and self.browser is not None \
                    and not nonempty_file(shot):
                scheduler.set_priority(scheduler.BACKGROUND)
                async with self.downloader.client.slot():
                    await self.browser.screenshot(key, shot)
        except Exception as exc:
            logger.info("Creator home extras failed for %s: %s", key, exc)

    # ── progress ────────────────────────────────────────────────────

    @property
    def backlog(self) -> int:
        """Works waiting to be downloaded (refreshed every queue step)."""
        return self._ready

    def round_progress(self) -> Dict[str, int]:
        """This download round: the works started since the queue was last
        empty, plus the works still waiting in the queue."""
        done = min(self._finished, self._started)
        if not self._active and not self._ready and self._started and done >= self._started:
            self._started = self._finished = 0
            return {"done": done, "total": done}
        return {"done": done, "total": self._started + self._ready}

    async def _queue_counts(self) -> Dict[str, int]:
        """``queue_counts`` scans the whole ledger: re-count every few seconds."""
        if time.monotonic() - self._counted_at >= COUNTS_TTL:
            self._counts = await self.db.read(work_store.queue_counts, int(time.time()))
            self._counted_at = time.monotonic()
            self._ready = self._counts["ready"]
        return self._counts

    async def _publish_progress(self) -> None:
        await self._queue_counts()
        progress = self.round_progress()
        if progress["total"]:
            self.events.emit_latest("download-lane", 1.0, "download_status",
                                    text=f"下载 {progress['done']}/{progress['total']}")

    @staticmethod
    def _active_row(entry: _Active) -> Dict[str, Any]:
        numbers = entry.progress.snapshot()
        return {"sec_uid": "", "aweme_id": entry.aweme_id, "creator": entry.creator,
                "title": entry.title,
                "state": "extras" if entry.extras else
                "large" if entry.lane == "large" else "downloading",
                "done": int(numbers["done"]), "total": int(numbers["total"]),
                "rate": float(numbers["rate"])}

    async def board(self, limit: int = 30) -> Dict[str, Any]:
        """The 下载 tab: running creator works (and extras), then the creator queue."""
        now = int(time.time())
        active = [self._active_row(entry) for entry in sorted(
            self._active.values(), key=lambda item: item.lane != "large") if entry.creator_work]
        queued = await self.db.read(work_store.peek_ready, now, max(0, limit - len(active)),
                                    set(self._active), ("creator",))
        counts = await self._queue_counts()
        ready = await self.db.read(work_store.ready_count, ("creator",), now)
        rows = active + [{"sec_uid": "", "aweme_id": row["aweme_id"],
                          "creator": row.get("author_name") or row.get("source_name") or "",
                          "title": row.get("title") or "", "state": "queued"}
                         for row in queued]
        rows = rows[:limit]
        shown = sum(1 for row in rows if row["state"] == "queued")
        return {"rows": rows, "remaining": max(0, ready - shown),
                "pending": len(active) + ready,
                "retry": counts["waiting"],
                "login_pause": max(0, int(self._paused_until - time.monotonic())),
                "progress": self.round_progress()}

    async def user_board(self, limit: int = 30, full: Set[int] = frozenset()) -> Dict[str, Any]:
        """The 用户 tab: one group per like/collect subscription with works to
        download.  A group lists its running works and its next queued work,
        or up to ``limit`` rows when it is in ``full``."""
        now = int(time.time())
        ready = await self.db.read(work_store.ready_by_source, SUBSCRIPTION_KINDS, now)
        running: Dict[int, List[Dict[str, Any]]] = {}
        for entry in self._active.values():
            if not entry.creator_work:
                running.setdefault(entry.source_id, []).append(self._active_row(entry))
        groups = []
        for source_id in sorted(set(ready) | set(running),
                                key=lambda sid: (sid not in running, sid)):
            source = await self.db.read(source_store.get, source_id)
            if source is None:
                continue
            active = running.get(source_id, [])[:limit]
            want = max(0, limit - len(active)) if source_id in full else 1
            queued = await self.db.read(work_store.peek_source, source_id, now, want,
                                        set(self._active))
            waiting = ready.get(source_id, 0)
            groups.append({
                "id": source_id, "kind": source.get("kind") or "like",
                "name": str(source.get("nickname") or source.get("sec_uid") or ""),
                "remaining": waiting + len(running.get(source_id, [])),
                "running": len(running.get(source_id, [])),
                "rows": active + [{"sec_uid": "", "aweme_id": row["aweme_id"],
                                   "creator": row.get("author_name") or "",
                                   "title": row.get("title") or "", "state": "queued"}
                                  for row in queued],
                "more": max(0, waiting - len(queued)),
            })
        return {"groups": groups, "pending": sum(group["remaining"] for group in groups)}

    def folder_busy(self, source_id: int) -> bool:
        return source_id in self.busy_sources()

    @staticmethod
    def files_exist(files: List[Dict[str, Any]]) -> bool:
        return all(nonempty_file(Path(entry["path"])) for entry in files
                   if entry.get("kind") in ("media", "live"))
