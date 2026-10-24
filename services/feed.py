"""New works of followed creators from the logged-in account's 关注 feed.

Checking every monitored creator's own list every ten minutes costs one or
two requests per creator per round.  The account's follow feed lists the new
works of *everyone it follows*, newest first, so a single page usually
covers a whole round.  Creators the account follows are therefore checked
through the feed; their own list is only re-read every few hours as a
safety net.  Creators it does not follow keep the regular checks.

When the feed fails (or has not succeeded for a while) nothing is relaxed:
``covers`` returns False and every creator falls back to its own checks.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, List, Set

from common.log import get_logger
from douyin import aweme as aw
from douyin.errors import LoginRequired
from douyin.pages import user_sec_uid
from engine import scheduler
from services.common import explain, pages
from services.context import Context
from storage import sources as source_store

logger = get_logger(__name__)

FEED_EVERY = (16 * 60, 24 * 60)       # seconds between two feed reads
FEED_PAGES = 5                          # pages read at most per run
FIRST_PAGES = 2                         # ... when there is no previous run to meet
PAGE_GAP = (3.0, 6.0)
FOLLOWING_EVERY = 12 * 3600             # the account's following list is re-read this often
FOLLOWING_PAGES = 60
HEALTHY_FOR = 30 * 60                   # the feed counts as working this long after a success
FOLLOWED_CHECK_EVERY = 6 * 3600         # a followed creator's own list, as a safety net


class FollowFeed:
    def __init__(self, ctx: Context, account: Any, record_page: Any):
        self.ctx = ctx
        self.account = account
        self.record_page = record_page       # Scanner._record_page
        self.followed: Set[str] = set()
        self._followed_owner = ""
        self._followed_at = 0.0
        self._next_run = 0.0
        self._ok_at = 0.0
        self._newest = 0                     # create_time of the newest work already read

    # ── state ───────────────────────────────────────────────────────

    def due(self) -> bool:
        return time.monotonic() >= self._next_run

    def healthy(self) -> bool:
        return self._ok_at > 0 and time.monotonic() - self._ok_at < HEALTHY_FOR

    def covers(self, source: Dict[str, Any]) -> bool:
        """The feed reports this creator's new works (it is followed and the feed works)."""
        return self.healthy() and str(source.get("sec_uid") or "") in self.followed

    # ── one run ─────────────────────────────────────────────────────

    async def run(self) -> int:
        """Read the feed once; returns how many works were queued."""
        self._next_run = time.monotonic() + random.uniform(*FEED_EVERY)
        scheduler.set_priority(scheduler.CREATOR_SCAN)
        me = await self.account.current_sec_uid()
        if not me:
            return 0
        try:
            await self._refresh_following(me)
            queued = await self._read_feed()
        except asyncio.CancelledError:
            raise
        except LoginRequired:
            raise
        except Exception as exc:
            self._ok_at = 0.0
            self.ctx.events.log(f"关注动态读取失败，改为逐个检查主播：{explain(exc)}")
            return 0
        self._ok_at = time.monotonic()
        return queued

    async def _refresh_following(self, me: str) -> None:
        if me == self._followed_owner and time.monotonic() - self._followed_at < FOLLOWING_EVERY:
            return
        found: Set[str] = set()
        count = 0
        async for page in pages(lambda cursor: self.ctx.client.following_page(me, cursor),
                                what="关注列表", delay=lambda: random.uniform(*PAGE_GAP)):
            count += 1
            for raw in page.dict_items:
                user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
                sec_uid = user_sec_uid(user)
                if sec_uid:
                    found.add(sec_uid)
            if count >= FOLLOWING_PAGES:
                break
        self.followed, self._followed_owner = found, me
        self._followed_at = time.monotonic()

    async def _read_feed(self) -> int:
        creators = {str(row["sec_uid"]): row for row in
                    await self.ctx.db.read(source_store.all_of, ["creator"]) if row["enabled"]}
        limit = FEED_PAGES if self._newest else FIRST_PAGES
        newest = self._newest
        queued_total = 0
        count = 0
        async for page in pages(self.ctx.client.follow_feed, what="关注动态",
                                delay=lambda: random.uniform(*PAGE_GAP)):
            count += 1
            items = [item for item in (aw.extract(raw) for raw in page.dict_items)
                     if item and aw.aweme_id(item)]
            queued_total += await self._record(items, creators)
            times = [aw.create_time(item) for item in items if aw.create_time(item) > 0]
            if times:
                newest = max(newest, max(times))
            # Reached works an earlier run already read (the feed is newest first).
            if not items or (self._newest and times and min(times) <= self._newest) \
                    or count >= limit:
                break
        self._newest = newest
        if queued_total:
            self.ctx.events.log(f"关注动态：新增 {queued_total} 个待下载作品。")
        return queued_total

    async def _record(self, items: List[Dict[str, Any]],
                      creators: Dict[str, Dict[str, Any]]) -> int:
        by_owner: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            owner = aw.author_sec_uid(item)
            if owner in creators:
                by_owner.setdefault(owner, []).append(item)
        queued_total = 0
        now = int(time.time())
        for owner, works in by_owner.items():
            source = creators[owner]
            queued = await self.ctx.db.write(self.record_page, int(source["id"]), works, now,
                                             int(source.get("full_epoch") or 0), 0)
            if queued:
                wanted = set(queued)
                self.ctx.queue.hand_over([work for work in works
                                          if aw.aweme_id(work) in wanted])
                queued_total += len(queued)
        if queued_total:
            self.ctx.queue.wake()
        return queued_total
