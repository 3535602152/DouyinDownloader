from __future__ import annotations

import asyncio

from engine import scheduler as sched
from engine.scheduler import Scheduler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def _hold(scheduler: Scheduler, priority: int, order: list, name: str,
                release: asyncio.Event) -> None:
    async with scheduler.slot(priority):
        order.append(name)
        await release.wait()


async def test_waiters_are_served_by_priority():
    scheduler = Scheduler()
    release = asyncio.Event()
    order: list = []
    blockers = [asyncio.ensure_future(_hold(scheduler, sched.CREATOR_DOWNLOAD, order, f"b{i}",
                                            release)) for i in range(sched.SLOTS)]
    await asyncio.sleep(0)
    waiting = [asyncio.ensure_future(_hold(scheduler, priority, order, name, release))
               for priority, name in ((sched.BACKGROUND, "chore"), (sched.FULL_SCAN, "full"),
                                      (sched.SUBSCRIPTION, "like"),
                                      (sched.CREATOR_FULL_SCAN, "creator-full"),
                                      (sched.CREATOR_SCAN, "check"))]
    await asyncio.sleep(0)
    assert order == ["b0", "b1"]
    release.set()
    await asyncio.gather(*blockers, *waiting)
    assert order[2:] == ["check", "creator-full", "like", "full", "chore"]


async def test_user_requests_never_wait_for_a_slot():
    scheduler = Scheduler()
    release = asyncio.Event()
    order: list = []
    blockers = [asyncio.ensure_future(_hold(scheduler, sched.BACKGROUND, order, f"b{i}", release))
                for i in range(sched.SLOTS)]
    await asyncio.sleep(0)
    async with scheduler.slot(sched.USER):
        order.append("user")
    assert order[-1] == "user"
    release.set()
    await asyncio.gather(*blockers)


async def test_waiting_raises_priority():
    clock = Clock()
    scheduler = Scheduler(clock=clock)
    # A full scan waiting a little over a minute goes before a fresh download.
    assert scheduler._rank(sched.FULL_SCAN, clock.now - 62, clock.now) \
        < scheduler._rank(sched.CREATOR_DOWNLOAD, clock.now, clock.now)


async def test_foreground_holds_background_requests():
    busy = {"value": True}
    scheduler = Scheduler(foreground_busy=lambda: busy["value"])
    scheduler.attach(asyncio.get_running_loop())
    order: list = []
    release = asyncio.Event()
    release.set()
    task = asyncio.ensure_future(_hold(scheduler, sched.CREATOR_DOWNLOAD, order, "bg", release))
    await asyncio.sleep(0.05)
    assert order == []
    busy["value"] = False
    scheduler.poke()
    await asyncio.wait_for(task, 1)
    assert order == ["bg"]


async def test_risk_pause_then_a_single_probe():
    clock = Clock()
    scheduler = Scheduler(clock=clock)
    scheduler.attach(asyncio.get_running_loop())
    scheduler.observe("risk", "/aweme/v1/web/aweme/detail/", 4.0)
    assert not scheduler.paused()            # short cooldowns stay per-path
    scheduler.observe("risk", "/aweme/v1/web/aweme/detail/", 60.0)
    assert scheduler.paused() and not scheduler.paused(sched.USER)
    assert scheduler.state()["risk"] == 60
    clock.now += 61
    assert not scheduler.paused()
    assert scheduler._limit() == 1           # the first request after a pause is a probe
    scheduler.observe("ok", "/aweme/v1/web/aweme/detail/", 0)
    assert scheduler._limit() == sched.SLOTS


async def test_login_failure_pauses_until_renewed():
    scheduler = Scheduler()
    scheduler.attach(asyncio.get_running_loop())
    scheduler.observe("login", "/aweme/v1/web/aweme/post/", 0)
    assert scheduler.login_paused and scheduler.paused()
    scheduler.resume_login()
    assert not scheduler.paused()


async def test_priority_follows_the_task():
    seen = []

    async def job(priority):
        sched.set_priority(priority)
        await asyncio.sleep(0)
        seen.append(sched.current_priority())

    await asyncio.gather(asyncio.ensure_future(job(sched.FULL_SCAN)),
                         asyncio.ensure_future(job(sched.CREATOR_DOWNLOAD)))
    assert sorted(seen) == [sched.CREATOR_DOWNLOAD, sched.FULL_SCAN]
    assert sched.current_priority() == sched.USER


async def test_background_requests_are_spaced_but_user_requests_are_not():
    clock = Clock()
    scheduler = Scheduler(clock=clock, spacing=(2.0, 2.0))
    async with scheduler.slot(sched.CREATOR_SCAN):
        pass
    second = asyncio.ensure_future(scheduler.slot(sched.CREATOR_SCAN).__aenter__())
    await asyncio.sleep(0)
    assert not second.done()                           # within the gap
    async with scheduler.slot(sched.USER):            # the user never waits
        pass
    clock.now += 2.0
    scheduler.poke()
    await asyncio.sleep(0)
    assert second.done()
    scheduler._release()


async def test_a_quiet_probe_ends_the_single_slot_mode():
    clock = Clock()
    scheduler = Scheduler(clock=clock)
    scheduler.attach(asyncio.get_running_loop())
    scheduler.observe("risk", "/aweme/v1/web/aweme/detail/", 30.0)
    assert scheduler.pause_path == "/aweme/v1/web/aweme/detail/"
    clock.now += 31
    # e.g. a browser page: no API "ok" arrives, but no new risk either.
    async with scheduler.slot(sched.CREATOR_DOWNLOAD):
        assert scheduler._limit() == 1
    assert scheduler._limit() == sched.SLOTS
