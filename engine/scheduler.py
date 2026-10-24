"""The one place that decides which Douyin API request goes next.

Every API request (list pages, work details, the logged-in page channel)
asks for one of a few *slots*.  Waiting requests are served by priority —
what the user is doing first, then creator checks, creator downloads,
creator full scans, subscriptions, subscription full scans and finally
background chores — and a request
that has waited long enough moves up, so a busy download queue cannot
starve the scans forever.  Media transfers from the CDN do not take a slot.

Background requests are also spaced out (``spacing``, a random gap between
two of them): the app runs for days, so a slow steady pace matters more
than speed.  User requests are never spaced.

The scheduler also owns the global brakes:

* while a user-started download runs, background work waits;
* a risk signal with a real cooldown pauses all background work until it
  ends, after which a single probe request goes first (comment endpoints
  excepted: the comment sync pauses itself);
* a login failure pauses background work until the cookies change.

The priority of the running code travels in a context variable, so tasks
declare it once (``set_priority``) and every request they make inherits it.
"""

from __future__ import annotations

import asyncio
import contextvars
import heapq
import itertools
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from common.log import get_logger

logger = get_logger("scheduler")

# Lower runs first.  Creator checks go before creator downloads: a check
# finds the new works the downloads should fetch.  Everything about the
# followed creators (checks, downloads, full scans) goes before the
# like/collect subscriptions, whose queues are far longer.
USER = 0
CREATOR_SCAN = 10
CREATOR_DOWNLOAD = 20
CREATOR_FULL_SCAN = 25
SUBSCRIPTION = 30
FULL_SCAN = 40
BACKGROUND = 50

LABELS = {USER: "手动操作", CREATOR_SCAN: "主播扫描", CREATOR_DOWNLOAD: "主播下载",
          CREATOR_FULL_SCAN: "主播全量扫描", SUBSCRIPTION: "订阅", FULL_SCAN: "全量扫描", BACKGROUND: "后台任务"}

SLOTS = 2
AGING_SECONDS = 2.0          # one priority step gained per this many seconds of waiting
RISK_PAUSE_MIN = 8.0         # shorter cooldowns are left to the per-path gate
WARMUP_SECONDS = 60.0        # subscriptions, full scans and chores wait this long after start
BACKGROUND_SPACING = (1.5, 3.0)  # the app's gap between two background requests, seconds
# Endpoints whose risk signals do not pause everything: the comment sync
# pauses itself (for hours), scans and downloads go on.
SELF_PAUSING_PATHS = ("/aweme/v1/web/comment/",)

_priority: contextvars.ContextVar[int] = contextvars.ContextVar("douyin_priority", default=USER)
_holding: contextvars.ContextVar[bool] = contextvars.ContextVar("douyin_slot", default=False)


def set_priority(priority: int) -> None:
    """Every request made by the current task from now on uses ``priority``."""
    _priority.set(int(priority))


def current_priority() -> int:
    return _priority.get()


class Scheduler:
    def __init__(self, *, foreground_busy: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic,
                 spacing: Tuple[float, float] = (0.0, 0.0)):
        self.foreground_busy = foreground_busy
        self.clock = clock
        self.spacing = spacing
        self._next_background = 0.0
        self.started = clock()
        self._active = 0
        self._probe = False               # after a pause: one request at a time until one succeeds
        self._probe_mark: Optional[int] = None
        self._risks = 0                   # risk signals seen so far
        self._paused_until = 0.0
        self.pause_path = ""              # the endpoint whose risk signals caused the pause
        self._login_paused = False
        self._waiters: List[Tuple[float, int, asyncio.Future]] = []
        self._meta: Dict[int, Tuple[int, float]] = {}
        self._order = itertools.count()
        self._pause_timer: Optional[asyncio.TimerHandle] = None
        self._tick_timer: Optional[asyncio.TimerHandle] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.listeners: List[Callable[[], None]] = []

    # ── state ───────────────────────────────────────────────────────

    def pause_remaining(self) -> float:
        return max(0.0, self._paused_until - self.clock())

    @property
    def login_paused(self) -> bool:
        return self._login_paused

    def paused(self, priority: int = BACKGROUND) -> bool:
        """Background work at ``priority`` may not start now."""
        if priority <= USER:
            return False
        return self._login_paused or self.pause_remaining() > 0

    def warm(self) -> bool:
        """Past the start-up window in which only urgent work runs."""
        return self.clock() - self.started >= WARMUP_SECONDS

    def state(self) -> Dict[str, Any]:
        return {"login": self._login_paused, "risk": round(self.pause_remaining()),
                "active": self._active, "waiting": len(self._waiters)}

    def _changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                logger.debug("scheduler listener failed", exc_info=True)

    # ── brakes ──────────────────────────────────────────────────────

    def observe(self, what: str, _path: str, value: float) -> None:
        """RiskGate observer; may be called from any thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._observe(what, value, _path)
        else:
            loop.call_soon_threadsafe(self._observe, what, value, _path)

    def _observe(self, what: str, value: float, path: str = "") -> None:
        if what == "risk":
            self._risks += 1
        if what == "risk" and value >= RISK_PAUSE_MIN                 and not str(path or "").startswith(SELF_PAUSING_PATHS):
            self.pause_for(value, path)
        elif what == "ok" and self._probe:
            self._probe = False
            self._dispatch()
        elif what == "login":
            self.pause_login()

    def pause_for(self, seconds: float, path: str = "") -> None:
        until = self.clock() + float(seconds)
        if until <= self._paused_until:
            return
        self._paused_until = until
        self.pause_path = path
        self._probe = True
        self._probe_mark = None
        logger.info("Background Douyin requests paused for %.0fs (risk control)", seconds)
        self._arm_timer(seconds)
        self._changed()

    def pause_login(self) -> None:
        if not self._login_paused:
            self._login_paused = True
            logger.info("Background Douyin requests paused until the login is renewed")
            self._changed()

    def resume_login(self) -> None:
        if self._login_paused:
            self._login_paused = False
            self._dispatch()
            self._changed()

    def _arm_timer(self, seconds: float) -> None:
        if self._loop is None:
            return
        if self._pause_timer is not None:
            self._pause_timer.cancel()

        def expired() -> None:
            self._pause_timer = None
            self._dispatch()
            self._changed()

        self._pause_timer = self._loop.call_later(max(0.0, seconds) + 0.05, expired)

    def poke(self) -> None:
        """Re-evaluate the waiters (e.g. the foreground finished)."""
        self._dispatch()

    # ── slots ───────────────────────────────────────────────────────

    def _limit(self) -> int:
        return 1 if self._probe else SLOTS

    def _rank(self, priority: int, since: float, now: float) -> float:
        return priority - (now - since) / AGING_SECONDS

    def _allowed(self, priority: int) -> bool:
        if priority <= USER:
            return True
        return not self.paused(priority) and not self.foreground_busy()

    def _dispatch(self) -> None:
        now = self.clock()
        # Re-rank with aging, then grant in order while slots are free.
        entries = []
        for _rank, seq, future in self._waiters:
            if future.done():
                continue
            priority, since = self._meta[seq]
            entries.append((self._rank(priority, since, now), seq, future))
        heapq.heapify(entries)
        self._waiters = entries
        kept = []
        while self._waiters and self._active < self._limit() and now >= self._next_background:
            rank, seq, future = heapq.heappop(self._waiters)
            priority, _since = self._meta[seq]
            if not self._allowed(priority):
                kept.append((rank, seq, future))
                continue
            self._meta.pop(seq, None)
            self._active += 1
            future.set_result(True)
            if self._probe:
                self._probe_mark = self._risks
            if priority > USER and self.spacing[1] > 0:
                self._next_background = now + random.uniform(*self.spacing)
        for entry in kept:
            heapq.heappush(self._waiters, entry)
        # User requests are never held back by the slot count.
        for rank, seq, future in list(self._waiters):
            priority, _since = self._meta.get(seq, (BACKGROUND, now))
            if priority <= USER and not future.done():
                self._waiters.remove((rank, seq, future))
                heapq.heapify(self._waiters)
                self._meta.pop(seq, None)
                self._active += 1
                future.set_result(True)
        if self._waiters and self._tick_timer is None and self._loop is not None:
            # Aging, the background spacing and a foreground that ends
            # silently are re-checked periodically.
            delay = 1.0
            if self._next_background > now:
                delay = min(delay, self._next_background - now + 0.01)
            self._tick_timer = self._loop.call_later(delay, self._tick)

    def _tick(self) -> None:
        self._tick_timer = None
        self._dispatch()

    @asynccontextmanager
    async def slot(self, priority: Optional[int] = None) -> AsyncIterator[None]:
        """Hold one API slot for the duration of one request (re-entrant)."""
        if _holding.get():
            yield
            return
        self._loop = self._loop or asyncio.get_running_loop()
        value = current_priority() if priority is None else int(priority)
        future: asyncio.Future = self._loop.create_future()
        seq = next(self._order)
        self._meta[seq] = (value, self.clock())
        heapq.heappush(self._waiters, (float(value), seq, future))
        self._dispatch()
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self._release()
            else:
                self._meta.pop(seq, None)
            raise
        token = _holding.set(True)
        try:
            yield
        finally:
            _holding.reset(token)
            self._release()

    def _release(self) -> None:
        self._active = max(0, self._active - 1)
        # The probe after a pause ended without a new risk signal: back to
        # normal even when it was not an API response (e.g. the browser).
        if self._probe and self._probe_mark == self._risks and self.pause_remaining() <= 0:
            self._probe, self._probe_mark = False, None
        self._dispatch()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
