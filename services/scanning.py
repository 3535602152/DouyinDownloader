"""Scanning a source (a creator's posts, an account's likes or collection).

One scan loop serves every kind; a :class:`Policy` supplies what differs:

==============  =========================  ==================================
                creator (关注主播)          like / collect (订阅)
==============  =========================  ==================================
listing         user posts                 user likes / own collection
incremental     stop after 3 known works   stop after a page with a known work
full scan       every N hours (setting)    every 3 days, 20–30 s per page
risk control    pause 30 minutes           pause 12 hours
==============  =========================  ==================================

Every page is persisted immediately (works, membership, ledger), so new works
are downloadable while the scan continues, and a full scan saves its cursor
after each page: an interruption resumes where it stopped.  A complete full
scan also finds works that disappeared from the list.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from douyin import aweme as aw
from douyin.errors import LoginRequired, PaginationError, is_risk
from douyin.pages import CursorGuard, Page
from services.common import explain
from services.context import Context
from storage import scans as scan_store
from storage import sources as source_store
from storage import works as work_store

KIND_LABELS = {"creator": "作品", "like": "喜欢", "collect": "收藏"}
VIEWER_FLAGS = {"like": "user_digged", "collect": "collect_stat"}
LOGIN_PAUSE = 10 * 60


@dataclass
class Policy:
    kind: str
    incremental_interval: int
    full_interval: Callable[[], int]      # seconds; 0 disables periodic full scans
    risk_pause: int
    error_pause: int
    page_wait: Callable[[bool, int], float]   # (full?, download backlog) -> seconds
    known_run: int                        # consecutive known works that end a check
    max_incremental_pages: int = 50


# A full scan only has to stay ahead of the downloads: a page (18 works)
# every few seconds while the queue is short, slowly once enough is waiting.
FULL_BACKLOG_ENOUGH = 100


def _creator_page_wait(full: bool, backlog: int) -> float:
    if not full:
        return random.uniform(3.0, 6.0)
    if backlog >= FULL_BACKLOG_ENOUGH:
        return random.uniform(8.0, 12.0)
    return random.uniform(4.0, 7.0)


def creator_policy(ctx: Context) -> Policy:
    return Policy(
        kind="creator", incremental_interval=30 * 60,
        full_interval=lambda: ctx.settings.full_scan_interval * 3600,
        risk_pause=30 * 60, error_pause=0,
        page_wait=lambda full, backlog: 0.0 if ctx.settings.scan_fast
        else _creator_page_wait(full, backlog),
        known_run=3,
    )


def subscription_policy(kind: str) -> Policy:
    # Pages at the creators' pace; subscriptions stay out of their way by
    # yielding whenever a creator scan is waiting (see ``Scanner.scan``).
    return Policy(
        kind=kind, incremental_interval=6 * 3600, full_interval=lambda: 3 * 86400,
        risk_pause=12 * 3600, error_pause=30 * 60,
        page_wait=_creator_page_wait,
        known_run=1,
    )


def incremental_due(source: Dict[str, Any], policy: Policy, now: int, jitter: int = 0) -> bool:
    """Whether new works should be looked for.

    Measured from the last successful scan only: a failing full scan must not
    keep postponing the check.  A creator's failed check waits one interval
    before it is tried again (subscriptions have ``error_pause`` for that).
    """
    last_success = max(int(source.get("last_incremental_at") or 0),
                       int(source.get("last_full_at") or 0))
    if now - last_success < policy.incremental_interval + jitter:
        return False
    if policy.kind == "creator":
        return now - int(source.get("incremental_try_at") or 0) >= policy.incremental_interval
    return True


def plan(source: Dict[str, Any], policy: Policy, now: int, jitter: int = 0) -> Optional[str]:
    """``full`` / ``incremental`` / ``None`` for a source at ``now``."""
    if not int(source.get("enabled") or 0) or int(source.get("paused_until") or 0) > now:
        return None
    last_full = int(source.get("last_full_at") or 0)
    due = incremental_due(source, policy, now, jitter)
    if int(source.get("full_epoch") or 0) > 0:
        # A long, interrupted full scan must not hide new works for days.
        if int(source.get("full_seen") or 0) > 0 and due:
            return "incremental"
        return "full"
    if last_full <= 0:
        return "full"
    interval = policy.full_interval()
    if interval > 0 and now - last_full >= interval:
        return "full"
    return "incremental" if due else None


def next_scan_at(source: Dict[str, Any], policy: Policy) -> int:
    paused = int(source.get("paused_until") or 0)
    last_full = int(source.get("last_full_at") or 0)
    if int(source.get("full_epoch") or 0) > 0 or last_full <= 0:
        return paused
    last_any = max(last_full, int(source.get("last_incremental_at") or 0))
    candidates = [last_any + policy.incremental_interval]
    if policy.full_interval() > 0:
        candidates.append(last_full + policy.full_interval())
    return max(paused, min(candidates))


@dataclass
class Outcome:
    mode: str
    pages: int = 0
    seen: int = 0
    queued: List[str] = field(default_factory=list)
    complete: bool = False
    paused: bool = False
    error: str = ""
    removed: int = 0
    missing: int = 0
    yielded: bool = False           # stopped early for more urgent scans; resumes later
    profile: Dict[str, Any] = field(default_factory=dict)


class Scanner:
    """Runs one scan of one source against the shared stores."""

    def __init__(self, ctx: Context, delete_files: Callable[[int, List[str]], Awaitable[int]]):
        self.ctx = ctx
        self.delete_files = delete_files
        self.verify_wait: Callable[[], float] = lambda: random.uniform(3.0, 6.0)

    def _fetch(self, source: Dict[str, Any]) -> Callable[[int], Awaitable[Page]]:
        client, sec_uid = self.ctx.client, str(source["sec_uid"])
        if source["kind"] == "creator":
            return lambda cursor: client.user_posts(sec_uid, cursor, 18)
        if source["kind"] == "collect":
            return lambda cursor: client.self_collection(cursor, 20)
        return lambda cursor: client.user_likes(sec_uid, cursor, 20)

    async def scan(self, source: Dict[str, Any], mode: str, policy: Policy, *,
                   batch: int = 0, progress: Callable[[Outcome], None] = lambda _o: None,
                   backlog: Callable[[], int] = lambda: 0,
                   viewer_is_owner: Optional[Callable[[], Awaitable[bool]]] = None,
                   should_yield: Callable[[], bool] = lambda: False) -> Outcome:
        """Scan ``source``; ``should_yield`` is asked between pages.

        A scan that yields stops without error: a full scan resumes from its
        saved cursor next time, an incremental check is simply due again.
        """
        db, source_id = self.ctx.db, int(source["id"])
        outcome = Outcome(mode)
        started = int(time.time())
        full = mode == "full"
        resumed = full and int(source.get("full_epoch") or 0) > 0
        epoch = int(source["full_epoch"]) if resumed else (started if full else
                                                            int(source.get("full_epoch") or 0))
        cursor = int(source.get("full_cursor") or 0) if resumed else 0
        if resumed:
            outcome.pages = int(source.get("full_pages") or 0)
            outcome.seen = int(source.get("full_seen") or 0)
        updates: Dict[str, Any] = {"last_check_at": started}
        if not full:
            updates["incremental_try_at"] = started
        if full:
            updates.update(full_epoch=epoch, full_cursor=cursor, full_pages=outcome.pages,
                           full_seen=outcome.seen)
        await db.write(source_store.update, source_id, **updates)
        if source["kind"] == "creator":
            outcome.profile = await self._refresh_creator(source)
        fetch = self._fetch(source)
        guard = CursorGuard(cursor)
        known_run = 0
        latest: Dict[str, Any] = {}
        try:
            while True:
                page = (await fetch(cursor)).ensure_valid(f"{KIND_LABELS[source['kind']]}列表")
                items = [aw.extract(item) for item in page.items]
                items = [item for item in items if item and aw.aweme_id(item)]
                if source["kind"] == "creator":
                    items = [item for item in items if aw.belongs_to(item, source["sec_uid"])]
                ids = [aw.aweme_id(item) for item in items]
                known = await db.read(source_store.known, source_id, ids)
                queued = await db.write(self._record_page, source_id, items, started, epoch,
                                        batch)
                outcome.queued.extend(queued)
                outcome.pages += 1
                outcome.seen += len(items)
                for item in items:
                    if aw.create_time(item) > int(latest.get("create_time") or 0):
                        latest = item
                if queued:
                    wanted = set(queued)
                    self.ctx.queue.hand_over([item for item in items
                                              if aw.aweme_id(item) in wanted])
                    self.ctx.queue.wake()
                if full:
                    await db.write(source_store.update, source_id,
                                   full_cursor=page.cursor if page.has_more else 0,
                                   full_pages=outcome.pages, full_seen=outcome.seen)
                progress(outcome)
                if not page.has_more:
                    break
                if not full:
                    stop = False
                    for item in items:
                        if aw.is_pinned(item):
                            continue
                        known_run = known_run + 1 if aw.aweme_id(item) in known else 0
                        stop = stop or known_run >= policy.known_run
                    if stop or outcome.pages >= policy.max_incremental_pages:
                        break
                cursor = guard.advance(page.cursor, "列表")
                wait = policy.page_wait(full, backlog())
                if wait:
                    await asyncio.sleep(wait)
                if should_yield():
                    outcome.yielded = True
                    return outcome
        except asyncio.CancelledError:
            raise
        except LoginRequired as exc:
            return await self._stopped(source, outcome, started + LOGIN_PAUSE, explain(exc))
        except Exception as exc:
            if is_risk(exc):
                outcome.paused = True
                hours = policy.risk_pause // 3600
                wait_text = f"{hours} 小时" if hours else f"{policy.risk_pause // 60} 分钟"
                return await self._stopped(
                    source, outcome, started + policy.risk_pause,
                    f"触发风控，暂停 {wait_text}后从第 {outcome.pages + 1} 页继续：{explain(exc)}")
            text = explain(exc) if not isinstance(exc, PaginationError) else str(exc)
            return await self._stopped(source, outcome,
                                       started + policy.error_pause if policy.error_pause else 0,
                                       text)
        outcome.complete = True
        finished = int(time.time())
        if latest and source["kind"] == "creator":
            await db.write(source_store.update, source_id,
                           latest_aweme_id=aw.aweme_id(latest),
                           latest_aweme_title=aw.title(latest),
                           latest_aweme_time=aw.create_time(latest))
        if not full:
            note = (f"扫描完成 · 检查 {outcome.seen} 个 · 新增 {len(outcome.queued)}"
                    if source["kind"] == "creator"
                    else f"增量扫描 {outcome.pages} 页，新增 {len(outcome.queued)}")
            await db.write(source_store.update, source_id, last_incremental_at=finished,
                           last_success_at=finished, last_error="", failure_count=0,
                           last_note=note)
            return outcome
        try:
            await self._settle_unseen(source, epoch, outcome, viewer_is_owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome.error = f"核对已消失的作品中断：{explain(exc)}"
        note = f"全量扫描完成 · 共 {outcome.seen} · 新增 {len(outcome.queued)}"
        if outcome.removed:
            note += f" · 已删除取消{KIND_LABELS[source['kind']]} {outcome.removed}"
        if outcome.missing:
            note += f" · 已不在列表 {outcome.missing}"
        await db.write(source_store.update, source_id, last_full_at=finished,
                       last_incremental_at=finished, last_success_at=finished, full_epoch=0,
                       full_cursor=0, full_pages=0, full_seen=0,
                       cloud_count=outcome.seen if source["kind"] != "creator"
                       else int(outcome.profile.get("aweme_count") or outcome.seen),
                       last_error=outcome.error, failure_count=0, last_note=note)
        return outcome

    @staticmethod
    def _record_page(db, source_id: int, items: List[Dict[str, Any]], seen_at: int,
                     epoch: int, batch: int) -> List[str]:
        work_store.save_works(db, items)
        ids = [aw.aweme_id(item) for item in items]
        source_store.record_listing(db, source_id, ids, seen_at=seen_at, epoch=epoch)
        queued = work_store.queue(db, ids, source_id)
        # A listed work that failed earlier gets another chance with every scan.
        statuses = work_store.statuses(db, ids)
        again = [aweme_id for aweme_id in ids if statuses.get(aweme_id) == "failed"
                 and aweme_id not in queued]
        if again:
            db.execute(f"UPDATE downloads SET status = 'retry', next_retry_at = 0 "
                       f"WHERE error_code <> 'INACCESSIBLE' AND aweme_id IN "
                       f"({','.join('?' for _ in again)})", again)
        if batch:
            scan_store.add_works(db, batch, [aweme_id for aweme_id in ids
                                             if statuses.get(aweme_id) != "done"])
        return queued

    async def _stopped(self, source: Dict[str, Any], outcome: Outcome, paused_until: int,
                       message: str) -> Outcome:
        outcome.error = message
        await self.ctx.db.write(
            source_store.update, int(source["id"]), paused_until=int(paused_until),
            last_error=message, failure_count=int(source.get("failure_count") or 0) + 1,
            last_note=f"异常：{message}")
        return outcome

    async def _refresh_creator(self, source: Dict[str, Any]) -> Dict[str, Any]:
        """Profile counts and a renamed nickname, best-effort."""
        try:
            user = await self.ctx.client.user_profile(str(source["sec_uid"])) or {}
        except LoginRequired:
            raise
        except Exception:
            return {}
        from douyin.pages import avatar_url, to_int

        fields: Dict[str, Any] = {}
        nickname = str(user.get("nickname") or "").strip()
        if nickname and nickname != source.get("nickname"):
            fields["nickname"] = nickname
        avatar = avatar_url(user)
        if avatar:
            fields["avatar_url"] = avatar
        if to_int(user.get("aweme_count")):
            fields["cloud_count"] = to_int(user.get("aweme_count"))
        if user.get("uid") and not source.get("uid"):
            fields["uid"] = str(user.get("uid"))
        if fields:
            await self.ctx.db.write(source_store.update, int(source["id"]), **fields)
        return user

    async def _settle_unseen(self, source: Dict[str, Any], epoch: int, outcome: Outcome,
                             viewer_is_owner: Optional[Callable[[], Awaitable[bool]]] = None
                             ) -> None:
        """Decide what happened to works a complete full scan did not list.

        Likes/collects of the logged-in account can be verified: if the work
        is visible and the viewer flag is off, it was un-liked — our copy is
        deleted.  Anything that cannot be confirmed is only marked missing.
        The flags describe whoever is logged in, so nothing is decided
        unless that is the subscribed account.
        """
        db, source_id, kind = self.ctx.db, int(source["id"]), source["kind"]
        if kind != "creator" and (viewer_is_owner is None or not await viewer_is_owner()):
            return
        unseen = await db.read(source_store.unseen_since, source_id, epoch)
        label = KIND_LABELS[kind]
        for index, row in enumerate(unseen):
            aweme_id = str(row["aweme_id"])
            if kind == "creator":
                await db.write(self._mark_missing, source_id, aweme_id, "已不在主页列表")
                outcome.missing += 1
                continue
            if index:
                await asyncio.sleep(self.verify_wait())
            detail, reason, verdict = await self.ctx.client.aweme_detail(aweme_id)
            flag = detail.get(VIEWER_FLAGS[kind]) if isinstance(detail, dict) else None
            if isinstance(detail, dict) and source.get("is_self") and flag is not None:
                if int(flag or 0):
                    await db.write(source_store.mark_seen, source_id, aweme_id, epoch)
                    continue
                await db.write(source_store.mark_gone, source_id, aweme_id, "removed",
                               f"已取消{label}")
                await self.delete_files(source_id, [aweme_id])
                outcome.removed += 1
                continue
            if isinstance(detail, dict):
                note = f"已不在{label}列表"
            elif verdict == "inaccessible":
                note = f"作品已不可见（{reason or '作者隐藏或删除'}）"
            else:
                continue  # no verdict (network / risk): try again next full scan
            await db.write(source_store.mark_gone, source_id, aweme_id, "missing", note)
            outcome.missing += 1

    @staticmethod
    def _mark_missing(db, source_id: int, aweme_id: str, note: str) -> None:
        source_store.mark_gone(db, source_id, aweme_id, "missing", note)
        work_store.set_visibility(db, aweme_id, "hidden", note)
