"""喜欢 / 收藏 subscriptions: an account's like list or own collection.

Subscriptions are ``like`` / ``collect`` sources.  One source is scanned at a
time, with a pause between scans: incremental every 6 hours, a slow full
scan every 3 days, 12 hours of rest after risk control (resuming from the
saved cursor).  Their works share the download queue, which serves them
only while no monitored creator has work due.

Each subscription is scanned with its own account's login when that account
is in the account library: the collection endpoint answers for whoever is
logged in, and the liked/collected flags used to confirm an un-like
describe that account too.  Anyone else's public likes are read with the
main account; their un-likes cannot be confirmed, so nothing is deleted.
Accounts take turns, one scan at a time, and every automatic scan gives way
to the creator scans (it waits to start and stops between pages while one
is pending).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from douyin.client import use_account
from engine import scheduler
from services.account import AccountService
from services.context import Context
from services.library import LibraryService
from services.scanning import KIND_LABELS, Scanner, next_scan_at, plan, subscription_policy
from storage import sources as source_store
from storage import works as work_store

TICK_SECONDS = 30.0
GAP_SECONDS = 60.0


class SubscriptionService:
    def __init__(self, ctx: Context, library: LibraryService, account: AccountService):
        self.ctx = ctx
        self.library = library
        self.account = account
        self.blocked: Dict[int, str] = {}     # sources that cannot be scanned now -> why
        self.creator_busy: Callable[[], bool] = lambda: False
        self._turns: Dict[str, float] = {}    # account -> when it last started a scan
        self.scanner = Scanner(ctx, lambda source_id, ids: library.delete_source_files(
            [source_id], ids))
        self._requests: Dict[int, str] = {}
        self._scanning: Dict[str, Any] = {}
        self._wake = asyncio.Event()
        self._loop: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.ensure_future(self._schedule())

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()

    # ── schedule ────────────────────────────────────────────────────

    async def _schedule(self) -> None:
        scheduler.set_priority(scheduler.SUBSCRIPTION)
        while True:
            picked = await self._next()
            if picked is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue
            source, mode = picked
            task = self.ctx.tasks.submit(
                "subscription-scan", lambda _task, s=source, m=mode: self._scan(s, m),
                scope="subscription", lane="subscription", key=f"subscription-scan:{source['id']}",
                kind="automatic")
            await task.done_event.wait()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), GAP_SECONDS)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _blocked(source: Dict[str, Any], library: Dict[str, Dict[str, Any]]) -> str:
        """Why ``source`` cannot be scanned now (``""``: it can)."""
        entry = library.get(str(source["sec_uid"]))
        if entry is not None and entry["expired"]:
            return "该账号登录已失效，请在设置里重新登录"
        if entry is None and source["kind"] == "collect":
            return "收藏只能用这个账号自己的登录读取，请先把它加入账号库"
        return ""

    async def _next(self):
        """The next ``(source, mode)`` to scan, or ``None``.

        The source dict carries ``_viewer`` (the account to scan with, ``""``
        for the main one), ``_owned`` (the viewer is the subscribed account)
        and ``_manual`` (asked for by the user: it does not give way).
        """
        if not self.ctx.settings.logged_in or self.ctx.scheduler.paused():
            return None
        main = await self.account.current_sec_uid()
        library = {entry["sec_uid"]: entry for entry in self.ctx.settings.accounts()
                   if entry["sec_uid"]}
        sources = {int(row["id"]): row for row in
                   await self.ctx.db.read(source_store.all_of, ["like", "collect"])}
        blocked = {source_id: reason for source_id, row in sources.items()
                   if (reason := self._blocked(row, library))}
        if blocked != self.blocked:
            self.blocked = blocked
            self.publish()

        def prepared(row: Dict[str, Any], manual: bool) -> Dict[str, Any]:
            owned = str(row["sec_uid"]) in library
            return {**row, "is_self": owned, "_owned": owned, "_manual": manual,
                    "_viewer": str(row["sec_uid"]) if owned else main}

        while self._requests:
            source_id, mode = next(iter(self._requests.items()))
            self._requests.pop(source_id)
            source = sources.get(source_id)
            if source is None:
                continue
            if source_id in blocked:
                self.ctx.events.log(f"{self._label(source)}：{blocked[source_id]}，已跳过扫描。")
                self.publish()
                continue
            if mode == "incremental" and not source["last_full_at"] and not source["full_seen"]:
                mode = "full"
            await self.ctx.db.write(source_store.update, source_id, paused_until=0)
            source["paused_until"] = 0
            return prepared(source, True), mode
        if not self.ctx.scheduler.warm():
            return None     # creators are checked first after start-up
        if self.creator_busy():
            return None     # creator scans go first
        now = int(time.time())
        due = []
        for source_id, source in sources.items():
            if source_id in blocked:
                continue
            mode = plan(source, subscription_policy(source["kind"]), now)
            if mode:
                due.append((prepared(source, False), mode))
        if not due:
            return None
        # Accounts take turns: the one that scanned longest ago goes first.
        due.sort(key=lambda entry: (self._turns.get(entry[0]["_viewer"], 0.0),
                                    next_scan_at(entry[0], subscription_policy(entry[0]["kind"])),
                                    int(entry[0]["id"])))
        return due[0]

    async def _scan(self, source: Dict[str, Any], mode: str) -> None:
        scheduler.set_priority(scheduler.FULL_SCAN if mode == "full" else scheduler.SUBSCRIPTION)
        viewer = str(source.get("_viewer") or "")
        use_account(viewer)
        self._turns[viewer] = time.monotonic()
        owned = bool(source.get("_owned"))
        label = self._label(source)
        kind = "全量" if mode == "full" else "增量"
        self._scanning = {"source_id": int(source["id"]), "mode": mode, "pages": 0, "seen": 0,
                          "queued": 0}
        self.publish()
        activity = self.ctx.activity
        activity.set("subscription", kind=source["kind"], name=label, seen=0,
                     total=int(source.get("cloud_count") or 0))
        self.ctx.events.log(f"{label}：开始{kind}扫描。")

        def progress(outcome) -> None:
            self._scanning.update(pages=outcome.pages, seen=outcome.seen,
                                  queued=len(outcome.queued))
            self.publish()
            activity.set("subscription", seen=outcome.seen)

        async def viewer_is_owner() -> bool:
            return owned

        manual = bool(source.get("_manual"))
        try:
            outcome = await self.scanner.scan(
                source, mode, subscription_policy(source["kind"]), progress=progress,
                viewer_is_owner=viewer_is_owner,
                should_yield=lambda: not manual and self.creator_busy())
        finally:
            self._scanning = {}
            self.publish()
            activity.clear("subscription")
        if outcome.error:
            self.ctx.events.log(f"{label}：{outcome.error}")
        elif outcome.yielded:
            self.ctx.events.log(f"{label}：关注主播有扫描任务，{kind}扫描先暂停"
                                f"（已读 {outcome.pages} 页），稍后继续。")
        elif outcome.complete:
            self.ctx.events.log(f"{label}：{kind}扫描完成，读取 {outcome.seen} 个，新增待下载 "
                                f"{len(outcome.queued)} 个。")
        if outcome.removed:
            self.ctx.events.log(f"{label}：{outcome.removed} 个作品已取消"
                                f"{KIND_LABELS[source['kind']]}，本地文件已删除。")
        if outcome.complete and mode == "full":
            members = await self.ctx.db.read(source_store.members, [int(source["id"])])
            await self.library.validate(members)

    # ── user actions ────────────────────────────────────────────────

    async def add(self, profile: Dict[str, Any], kinds: Sequence[str]) -> List[int]:
        ids = []
        for kind in kinds:
            if kind == "collect" and not profile.get("is_self"):
                continue
            source_id, _created = await self.ctx.db.write(
                source_store.add, kind, profile["sec_uid"], nickname=profile.get("nickname", ""),
                avatar_url=profile.get("avatar_url", ""), is_self=bool(profile.get("is_self")),
                cloud_count=int(profile.get("favoriting_count") or 0) if kind == "like" else 0)
            ids.append(source_id)
        self.publish()
        self._wake.set()
        return ids

    def request_scan(self, ids: Sequence[int], full: bool) -> int:
        for source_id in ids:
            self._requests[int(source_id)] = "full" if full else "incremental"
        self._wake.set()
        self.publish()
        return len(ids)

    async def set_enabled(self, ids: Sequence[int], enabled: bool) -> int:
        changed = await self.ctx.db.write(source_store.set_enabled, ids, enabled)
        if enabled:
            self._wake.set()
            self.ctx.queue.wake()
        self.publish()
        return changed

    async def delete(self, ids: Sequence[int]) -> int:
        for source_id in ids:
            self._requests.pop(int(source_id), None)
        removed = await self.ctx.db.write(source_store.delete, ids)
        await self.ctx.db.write(work_store.cancel_orphans)
        self.publish()
        return removed

    async def retry_failed(self, ids: Sequence[int]) -> int:
        members = await self.ctx.db.read(source_store.members, ids)
        changed = await self.ctx.db.write(work_store.requeue, members)
        self.ctx.queue.wake()
        self.publish()
        return len(changed)

    # ── table ───────────────────────────────────────────────────────

    async def rows(self) -> List[Dict[str, Any]]:
        now = int(time.time())
        sources = await self.ctx.db.read(source_store.all_of, ["like", "collect"])
        counts = await self.ctx.db.read(source_store.download_counts, ["like", "collect"])
        library = {entry["sec_uid"] for entry in self.ctx.settings.accounts()}
        rows = []
        for source in sorted(sources, key=lambda row: row["id"]):
            numbers = counts.get(int(source["id"]), {})
            policy = subscription_policy(source["kind"])
            rows.append({
                "id": source["id"], "account_sec_uid": source["sec_uid"], "kind": source["kind"],
                "kind_label": KIND_LABELS.get(source["kind"], source["kind"]),
                "nickname": source["nickname"], "avatar_url": source["avatar_url"],
                "is_self": source["sec_uid"] in library, "enabled": source["enabled"],
                "added_at": source["added_at"], "cloud_count": source["cloud_count"],
                "last_incremental_at": source["last_incremental_at"],
                "last_full_at": source["last_full_at"], "full_epoch": source["full_epoch"],
                "full_cursor": source["full_cursor"], "full_pages": source["full_pages"],
                "full_seen": source["full_seen"], "paused_until": source["paused_until"],
                "last_error": source["last_error"], "last_note": source["last_note"],
                "counts": {
                    "total": numbers.get("total", 0), "downloaded": numbers.get("done", 0),
                    "pending": numbers.get("pending", 0) + numbers.get("retry", 0)
                    + numbers.get("active", 0),
                    "failed": numbers.get("failed", 0), "missing": numbers.get("missing", 0),
                    "removed": numbers.get("removed", 0)},
                "next_scan_at": next_scan_at(source, policy) if source["enabled"] else 0,
                "paused": int(source["paused_until"] or 0) > now,
                "scanning": dict(self._scanning)
                if self._scanning.get("source_id") == source["id"] else None,
                "requested": self._requests.get(int(source["id"]), ""),
                "blocked": self.blocked.get(int(source["id"]), ""),
            })
        return rows

    def publish(self) -> None:
        async def deliver() -> None:
            self.ctx.events.emit_latest("subscriptions", 1.0, "subscription_rows",
                                        rows=await self.rows())

        self.ctx.background(deliver(), "刷新订阅列表")

    def on_download(self, source_id: int) -> None:
        self.publish()

    @staticmethod
    def _label(source: Dict[str, Any]) -> str:
        name = str(source.get("nickname") or source.get("sec_uid") or "")
        return f"{name}的{KIND_LABELS.get(source.get('kind'), '')}"
