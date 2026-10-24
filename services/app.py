"""The application: every component wired together, started and stopped."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import aiohttp

from common.log import get_logger, log_to_file
from douyin import aweme as aw
from douyin.browser import BrowserChannel
from douyin.client import DouyinClient
from engine.downloader import WorkDownloader
from engine.events import EventBus
from engine.fetcher import MediaFetcher
from engine.queue import DownloadQueue
from engine.runtime import Runtime, TaskManager
from engine.scheduler import BACKGROUND_SPACING, Scheduler
from services.account import AccountService
from services.activity import ScanActivity
from services.comments import CommentService
from services.context import Context
from services.creators import CreatorPages
from services.library import LibraryService
from services.links import LinkService
from services.monitor import MonitorService
from services.subscriptions import SubscriptionService
from services.user_content import UserContentService
from storage import schema
from storage import sources as source_store
from storage import works as work_store
from storage.db import Database
from storage.settings import Settings

logger = get_logger(__name__)


COVER_FIX_KEY = "cover_fix_v1"
COVER_FIX_DELAY = 10 * 60      # let start-up scans go first
COVER_FIX_SPACING = 30         # one cover repair due every this many seconds
WATERMARK_FIX_KEY = "watermark_fix_v1"
WATERMARK_FIX_SPACING = 60     # one gallery re-fetch due every this many seconds
WATERMARK_FIX_WINDOW = 3600    # pictures written this long before their JSON


def watermarked_galleries(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    """Galleries with a picture saved from its watermarked copy.

    The fetcher used to let yarl decode ``%2F`` in the query: a clean picture
    URL whose ``x-signature`` holds one answered 403, so the download fell
    back to the watermarked ``download_url_list`` copy.  The saved JSON holds
    the URLs of that attempt, but only for pictures fetched in that same
    attempt (a later run keeps existing pictures and rewrites the JSON), so a
    picture counts only when it was written shortly before the JSON.
    """
    from yarl import URL

    found: List[str] = []
    for entry in entries:
        try:
            payload = json.loads(Path(entry["json"]).read_text(encoding="utf-8"))
            saved_at = datetime.fromisoformat(
                str((payload.get("_backup") or {}).get("saved_at"))).timestamp()
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        stills = [urls for _index, urls, _clips in aw.gallery_assets(payload) if urls]
        for position, path in entry["pictures"].items():
            if not 1 <= position <= len(stills):
                continue
            urls = stills[position - 1]
            clean = [url for url in urls if not aw.is_watermarked(url)]
            if not clean or len(clean) == len(urls)                     or any(str(URL(url)) == url for url in clean):
                continue
            try:
                written = os.path.getmtime(path)
            except OSError:
                continue
            if saved_at - WATERMARK_FIX_WINDOW <= written <= saved_at + 60:
                found.append(str(entry["aweme_id"]))
                break
    return found


def landscape_covers(files: Sequence[Mapping[str, Any]]) -> List[str]:
    """Portrait videos whose saved cover is landscape (the detail API's 4:3 cut)."""
    from PIL import Image

    found: List[str] = []
    for entry in files:
        if entry["aweme_id"] in found or not entry["height"] > entry["width"] > 0:
            continue
        try:
            with Image.open(entry["path"]) as image:
                width, height = image.size
        except (OSError, ValueError):
            continue
        if width > height:
            found.append(entry["aweme_id"])
    return found


def background_spacing(settings: Settings):
    """The gap between two background requests: none in the fast scan strategy."""
    return (0.0, 0.0) if settings.scan_fast else BACKGROUND_SPACING


class App:
    """Build with :meth:`create`; all services live on ``runtime.loop``."""

    def __init__(self, ctx: Context, http: aiohttp.ClientSession):
        self.ctx = ctx
        self._http = http
        self.activity = ScanActivity(ctx)
        ctx.activity = self.activity
        self.account = AccountService(ctx)
        self.library = LibraryService(ctx)
        self.links = LinkService(ctx)
        self.user_content = UserContentService(ctx)
        self.comments = CommentService(ctx)
        self.monitor = MonitorService(ctx, self.library, self.account, self.comments)
        self.subscriptions = SubscriptionService(ctx, self.library, self.account)
        self.subscriptions.creator_busy = self.monitor.creator_scan_pending
        self.activity.skipped = lambda: set(self.subscriptions.blocked)
        self.activity.creator_policy = self.monitor.policy_for
        self.creators = CreatorPages(ctx)
        ctx.queue.listeners.append(self._download_finished)
        ctx.settings.subscribe(self._settings_changed)

    # ── construction ────────────────────────────────────────────────

    @classmethod
    def create(cls, data_dir: Path, *, start_background: bool = True) -> "App":
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        log_to_file(data_dir / "logs" / "backend.log")
        runtime = Runtime()
        return runtime.call(cls._build(data_dir, runtime, start_background), timeout=120)

    @classmethod
    async def _build(cls, data_dir: Path, runtime: Runtime, start_background: bool) -> "App":
        settings = Settings(data_dir)
        events = EventBus()
        db = Database(settings.database_path)
        tasks = TaskManager(runtime, events)
        scheduler = Scheduler(foreground_busy=lambda: tasks.foreground_busy,
                              spacing=background_spacing(settings))
        scheduler.attach(runtime.loop)
        holder: Dict[str, Any] = {}

        def risk(event: Dict[str, Any]) -> None:
            events.emit("risk_event", event=event)
            app = holder.get("app")
            if app is not None:
                runtime.loop.call_soon_threadsafe(
                    lambda: app.ctx.background(app.library.record_risk(event), "保存风控事件"))

        client = DouyinClient(settings.cookies, proxy=settings.proxy, rate=settings.rate_limit,
                              risk_listener=risk)
        browser = BrowserChannel(profile_dir=data_dir / "browser_profile",
                                 headless=bool((settings.get("browser_fallback") or {})
                                               .get("headless", True)),
                                 user_agent=client.user_agent,
                                 on_captcha=lambda: client.gate.note_risk("/browser/", "captcha"))
        browser.enabled = settings.browser_fallback
        browser.set_cookies(settings.cookies)
        client.page_channel = browser.request_api
        client.scheduler = scheduler
        client.gate.observers.append(scheduler.observe)
        http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            limit=settings.connections + 8, limit_per_host=0, ttl_dns_cache=300))
        fetcher = MediaFetcher(http, connections=settings.connections,
                               max_segments=settings.segments, proxy=settings.proxy or None)
        holder_ctx: Dict[str, Context] = {}

        def layout():
            return holder_ctx["ctx"].layout()

        downloader = WorkDownloader(
            client, fetcher, layout=layout, quality=lambda: settings.quality,
            merge_live_photos=lambda: bool(settings.get("live_photo_merge", True)),
            browser=browser)
        queue = DownloadQueue(db, downloader, events, tasks, layout=layout,
                              concurrency=lambda: settings.work_concurrency,
                              homepage_screenshot=lambda: bool(
                                  settings.get("homepage_screenshot", True)),
                              browser=browser, scheduler=scheduler)
        ctx = Context(data_dir=data_dir, settings=settings, events=events, runtime=runtime,
                      tasks=tasks, db=db, client=client, browser=browser,
                      downloader=downloader, queue=queue, scheduler=scheduler)
        holder_ctx["ctx"] = ctx
        app = cls(ctx, http)
        holder["app"] = app
        client.on_login_lost = app.account.login_lost
        client.on_cookies_renewed = app.save_renewed_cookies
        await app.account.start()
        if start_background:
            app.start()
        return app

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        self.ctx.queue.start()
        self.monitor.start()
        self.subscriptions.start()
        self.comments.start()
        if self.account.needs_refresh():
            self.account.refresh_profile()
        self.user_content.resume_pending()
        self.ctx.runtime.submit(self.repair_covers())
        self.ctx.runtime.submit(self.repair_watermarks())

    def save_renewed_cookies(self, account: str, cookies: Dict[str, str]) -> None:
        """Keep a renewed device id (``ttwid``) with that account's login."""
        settings = self.ctx.settings
        settings.save_account(account or settings.main_account, cookies)
        self.ctx.events.log("已为账号更换设备标识，图文将按原尺寸无水印下载。")

    async def repair_covers(self) -> int:
        """Once: queue a slow re-fetch of covers saved as a landscape cut."""
        db = self.ctx.db
        if await db.read(schema.meta, COVER_FIX_KEY):
            return 0
        try:
            files = await db.read(work_store.cover_files)
            found = await asyncio.to_thread(landscape_covers, files)

            def queue(connection) -> int:
                queued = work_store.queue_cover_fix(connection, found,
                                                    int(time.time()) + COVER_FIX_DELAY,
                                                    COVER_FIX_SPACING)
                schema.set_meta(connection, COVER_FIX_KEY, queued)
                return queued

            queued = await db.write(queue)
        except Exception:
            logger.exception("Cover repair scan failed")
            return 0
        if queued:
            self.ctx.events.log(f"发现 {queued} 个作品的封面是横版裁切图，"
                                f"将在后台每 {COVER_FIX_SPACING} 秒重新获取一个。")
        return queued

    async def repair_watermarks(self) -> int:
        """Once: queue a slow re-fetch of galleries saved with watermarked pictures."""
        db = self.ctx.db
        if await db.read(schema.meta, WATERMARK_FIX_KEY):
            return 0
        try:
            entries = await db.read(work_store.gallery_json_files)
            found = await asyncio.to_thread(watermarked_galleries, entries)

            def queue(connection) -> int:
                queued = work_store.queue_cover_fix(connection, found,
                                                    int(time.time()) + COVER_FIX_DELAY,
                                                    WATERMARK_FIX_SPACING, "watermark_fix")
                schema.set_meta(connection, WATERMARK_FIX_KEY, queued)
                return queued

            queued = await db.write(queue)
        except Exception:
            logger.exception("Watermark repair scan failed")
            return 0
        if queued:
            self.ctx.events.log(f"发现 {queued} 个图文作品下载成了带水印的图片，"
                                f"将在后台每 {WATERMARK_FIX_SPACING} 秒重新下载一个。")
        return queued

    async def rebuild_backlog(self) -> int:
        """Drop the waiting works and full-scan every enabled source again.

        The scans queue the works anew and hand their listing payloads to the
        downloads, so no detail request is needed (useful after the backlog
        grew from scans that could not hand anything over).
        """
        ctx = self.ctx
        dropped = await ctx.db.write(work_store.drop_backlog)
        ctx.queue.wake()
        creators = [row["sec_uid"] for row in
                    await ctx.db.read(source_store.all_of, ["creator"]) if row["enabled"]]
        if creators:
            await self.monitor.full_scan(creators)
        subscriptions = [int(row["id"]) for row in
                         await ctx.db.read(source_store.all_of, ["like", "collect"])
                         if row["enabled"]]
        if subscriptions:
            self.subscriptions.request_scan(subscriptions, True)
        ctx.events.log(f"已清空待下载 {dropped} 个作品，正在重新全量扫描 {len(creators)} 个主播"
                       f"{f'和 {len(subscriptions)} 个订阅' if subscriptions else ''}；"
                       f"扫描到的作品会直接交给下载。")
        self.monitor.changed()
        return dropped

    def close(self) -> None:
        async def shutdown() -> None:
            await self.monitor.stop()
            await self.subscriptions.stop()
            self.comments.stop()
            await self.ctx.tasks.shutdown()
            await self.ctx.queue.stop()
            await self.ctx.browser.close()
            await self.ctx.client.close()
            self.ctx.downloader.fetcher.close()
            await self._http.close()

        try:
            self.ctx.runtime.call(shutdown(), timeout=20)
        except Exception as exc:
            logger.warning("Shutdown incomplete: %s", exc)
        self.ctx.runtime.stop()
        self.ctx.db.close()

    # ── cross-feature reactions ─────────────────────────────────────

    def _download_finished(self, source_id: int, _result) -> None:
        self.monitor.changed()
        self.subscriptions.on_download(source_id)

    def _settings_changed(self) -> None:
        settings = self.ctx.settings

        def apply() -> None:
            self.ctx.client.set_rate(settings.rate_limit)
            self.ctx.scheduler.spacing = background_spacing(settings)
            self.ctx.browser.enabled = settings.browser_fallback
            self.ctx.queue.wake()

        if self.ctx.runtime.on_loop:
            apply()
        else:
            self.ctx.runtime.call_soon(apply)

