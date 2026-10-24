"""关注主播: monitored creators.

Creators are ``creator`` sources.  A background loop checks every enabled
creator about every 10 minutes (incremental) and walks its whole catalogue
on the configured interval (full scan); new works go to the shared ledger
and the download queue fetches them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import time
from typing import Any, Dict, List, Optional, Sequence

from douyin.client import use_account
from douyin.errors import LoginRequired
from douyin.pages import avatar_url, to_int, user_sec_uid
from engine import scheduler
from engine.runtime import STOPPING
from services.common import explain, pages, resolve_link
from services.context import Context
from services.feed import FOLLOWED_CHECK_EVERY, FollowFeed
from services.library import LibraryService
from services.scanning import (
    Outcome,
    Policy,
    Scanner,
    creator_policy,
    incremental_due,
    plan,
)
from storage import scans as scan_store
from storage import sources as source_store
from storage import works as work_store

TICK_SECONDS = 15
CREATOR_SCAN_TASKS = ("monitor-check", "monitor-feed", "monitor-full-scan")
# The app runs for days: in the stable scan strategy (the default) creators
# are checked one at a time with a pause in between, so the API sees a slow,
# steady trickle rather than bursts; the fast strategy skips these pauses.
CHECK_GAP = (12.0, 18.0)        # seconds between two creators of an automatic check
MANUAL_GAP = (4.0, 7.0)         # ... of a check the user started (the first one goes at once)
FULL_GAP = (5.0, 10.0)          # seconds between two creators of a full scan
LOCAL_STATS_INTERVAL = 10.0


def _creator_columns(figures: Dict[str, Any]) -> Dict[str, Any]:
    """The optional 关注主播 columns, named for the GUI."""
    return {
        "hidden_work_count": int(figures.get("hidden", 0)),
        "new_1d_count": int(figures.get("new_1d", 0)),
        "new_7d_count": int(figures.get("new_7d", 0)),
        "latest_publish_time": int(figures.get("latest_time", 0)),
        "video_count": int(figures.get("videos", 0)),
        "gallery_count": int(figures.get("galleries", 0)),
        "video_duration_ms": int(figures.get("duration_ms", 0)),
        "downloaded_work_count": int(figures.get("done", 0)),
        "comments_saved_count": int(figures.get("comments_saved", 0)),
        "top_digg_count": int(figures.get("top_digg", 0)),
        "top_digg_title": str(figures.get("top_digg_title", "")),
        "avg_digg_count": int(figures.get("avg_digg", 0)),
        "douyin_id": str(figures.get("douyin_id", "")),
    }


class MonitorService:
    def __init__(self, ctx: Context, library: LibraryService, account: Any = None,
                 comments: Any = None):
        self.ctx = ctx
        self.library = library
        self.comments = comments
        self.policy = creator_policy(ctx)
        # A followed creator's new works come through the feed; its own list
        # is only re-read as a safety net.
        self.followed_policy = dataclasses.replace(self.policy,
                                                   incremental_interval=FOLLOWED_CHECK_EVERY)
        self.scanner = Scanner(ctx, lambda source_id, ids: library.delete_source_files(
            [source_id], ids))
        self.feed = FollowFeed(ctx, account, Scanner._record_page) if account else None
        self._loop: Optional[asyncio.Task] = None
        self._login_pause_until = 0.0
        self._scan_notes: Dict[int, str] = {}
        self._full_source = 0       # the creator the running full scan is on
        self._local: Dict[str, Dict[str, Any]] = {}
        self._local_dirty = True
        self._local_task: Optional[asyncio.Task] = None
        self._following: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._following_request = ""
        self._following_key = "monitor-following-fetch"
        self._following_account = ""
        self._following_seq = 0

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._loop is None or self._loop.done():
            self._loop = asyncio.ensure_future(self._schedule())
            self.ctx.events.log("关注主播后台监控已启动（约每 30 分钟检查一次）。")

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()

    def changed(self) -> None:
        self._local_dirty = True
        self.ctx.events.emit_latest("monitor-refresh", 1.0, "monitor_refresh")

    async def _schedule(self) -> None:
        try:
            await self.ctx.db.write(scan_store.interrupt_stale, "应用已关闭，扫描中断")
        except Exception as exc:
            self.ctx.events.log(f"清理上次的扫描任务失败：{explain(exc)}")
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.events.log(f"监控调度异常：{explain(exc)}")
            await asyncio.sleep(TICK_SECONDS)

    def creator_scan_pending(self) -> bool:
        """A creator check, feed read or full scan is queued or running.

        Subscription scans give way to these: they wait to start and stop
        between pages while one is pending.
        """
        return self.ctx.tasks.pending(*CREATOR_SCAN_TASKS)

    def policy_for(self, source: Dict[str, Any]) -> Policy:
        """How often this creator's own list is checked."""
        if self.feed is not None and self.feed.covers(source):
            return self.followed_policy
        return self.policy

    def _submit_feed(self) -> None:
        async def run(_task) -> None:
            activity = self.ctx.activity
            activity.set("check", name="关注动态", failed=0, waiting_until=0)
            try:
                await self.feed.run()
            finally:
                activity.clear("check")
                self.changed()

        self.ctx.tasks.submit("monitor-feed", run, scope="monitor", lane="monitor-discover",
                              key="monitor-feed", kind="automatic", priority=15)

    async def _tick(self) -> None:
        if time.time() < self._login_pause_until or not self.ctx.settings.logged_in:
            return
        if self.ctx.scheduler.paused():
            return      # risk control / expired login: checks resume after the pause
        if self.feed is not None:
            if self.feed.due() and not self.ctx.tasks.get("monitor-feed"):
                self._submit_feed()
            if self.ctx.tasks.get("monitor-feed"):
                return  # plan the checks once the feed says which creators it covers
        now = int(time.time())
        full_now = self.ctx.scheduler.warm() and not self.ctx.tasks.running("monitor-heavy")
        due_full, due_check = [], []
        for source in await self.ctx.db.read(source_store.all_of, ["creator"]):
            if source["id"] == self._full_source:
                continue    # the running full scan is reading this creator already
            jitter = sum(ord(ch) for ch in str(source["sec_uid"])[-12:]) % 61
            policy = self.policy_for(source)
            mode = plan(source, policy, now, jitter)
            if mode == "full":
                due_full.append(source["id"])
                # A periodic full scan that cannot start yet must not delay
                # the new works an incremental check finds right away.
                if not full_now and source.get("last_full_at") \
                        and incremental_due(source, policy, now, jitter):
                    due_check.append(source["id"])
            elif mode == "incremental":
                due_check.append(source["id"])
        if due_full and full_now:
            await self._full_scan_ids(due_full, automatic=True)
        if due_check:
            self._submit_checks(due_check, automatic=True)

    # ── rows ────────────────────────────────────────────────────────

    async def rows(self) -> List[Dict[str, Any]]:
        sources = await self.ctx.db.read(source_store.all_of, ["creator"])
        counts = await self.ctx.db.read(source_store.download_counts, ["creator"])
        result = []
        for source in sources:
            numbers = counts.get(int(source["id"]), {})
            note = self._scan_notes.get(int(source["id"])) or source.get("last_note") or ""
            local = self._local.get(str(source["sec_uid"]))
            row = {
                "sec_uid": source["sec_uid"], "nickname": source["nickname"],
                "avatar_url": source["avatar_url"],
                "homepage_url": f"https://www.douyin.com/user/{source['sec_uid']}",
                "enabled": bool(source["enabled"]), "added_at": source["added_at"],
                "comments_enabled": bool(source.get("comments_enabled")),
                "last_check_at": source["last_check_at"] or None,
                "last_success_at": source["last_success_at"] or None,
                "last_check_note": note,
                "last_error": source["last_error"] if int(source.get("paused_until") or 0)
                > int(time.time()) or "异常" in note else "",
                "last_full_scan_at": source["last_full_at"],
                "public_aweme_count": source["cloud_count"],
                "profile_aweme_count": source["cloud_count"],
                "latest_aweme_id": source["latest_aweme_id"],
                "latest_aweme_title": source["latest_aweme_title"],
                "latest_aweme_time": source["latest_aweme_time"],
                "last_24h_downloaded": numbers.get("day", 0),
                "today_downloaded": numbers.get("day", 0),
                "invisible_count": numbers.get("hidden", 0),
                "permanent_failed_count": numbers.get("failed", 0),
                "retryable_failed_count": numbers.get("retry", 0),
                "pending_download_count": numbers.get("pending", 0),
                "downloading_count": numbers.get("active", 0),
                "last_work_error_message": numbers.get("last_error", ""),
            }
            if local is None:
                row["local_stats_pending"] = True
            else:
                row.update(local)
            result.append(row)
        self._refresh_local_stats()
        return result

    def _refresh_local_stats(self) -> None:
        if not self._local_dirty and self._local:
            return
        if self._local_task is not None and not self._local_task.done():
            return

        async def compute() -> None:
            started = time.monotonic()
            self._local_dirty = False
            entries = await self.ctx.db.read(source_store.local_file_stats, ["creator"])
            figures = await self.ctx.db.read(source_store.creator_stats, ["creator"])
            sources = {int(row["id"]): str(row["sec_uid"])
                       for row in await self.ctx.db.read(source_store.all_of, ["creator"])}

            def tally() -> Dict[str, Dict[str, int]]:
                stats: Dict[str, Dict[str, Any]] = {}
                for entry in entries:
                    sec_uid = sources.get(int(entry["source_id"]))
                    if sec_uid is None:
                        continue
                    try:
                        size = os.stat(entry["path"]).st_size
                    except OSError:
                        continue
                    bucket = stats.setdefault(sec_uid, {"bytes": 0, "comments": 0,
                                                        "works": set()})
                    bucket["bytes"] += size
                    if entry["kind"] == "comments":
                        bucket["comments"] += size
                    if entry["kind"] in ("media", "live") and size > 0:
                        bucket["works"].add(entry["aweme_id"])
                return {key: {"local_work_count": len(value["works"]),
                              "local_storage_bytes": value["bytes"],
                              "comments_bytes": value["comments"]}
                        for key, value in stats.items()}

            computed = await asyncio.to_thread(tally)
            self._local = {sec_uid: {**_creator_columns(figures.get(source_id, {})),
                                     **computed.get(sec_uid, {"local_work_count": 0,
                                                              "local_storage_bytes": 0,
                                                              "comments_bytes": 0})}
                           for source_id, sec_uid in sources.items()}
            self.ctx.events.emit("monitors", rows=await self.rows())
            await asyncio.sleep(max(0.0, LOCAL_STATS_INTERVAL - (time.monotonic() - started)))

        self._local_task = self.ctx.background(compute(), "统计本地文件")

    # ── adding ──────────────────────────────────────────────────────

    def add_urls(self, raw: str) -> bool:
        lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
        if not lines:
            return False

        async def run(_task) -> None:
            events = self.ctx.events
            added_ids, ok, failed = [], 0, 0
            for index, line in enumerate(lines):
                events.activity("monitor-add", f"添加主播 {index + 1}/{len(lines)}"
                                + (f" 失败{failed}" if failed else ""))
                try:
                    _url, link = await resolve_link(self.ctx.client, line)
                    if link is None or link.kind != "user" or not link.sec_uid:
                        raise ValueError("只支持抖音用户主页链接")
                    user = await self.ctx.client.user_profile(link.sec_uid)
                    if not user:
                        raise RuntimeError("获取主播信息失败，请检查 Cookie 或稍后重试")
                    sec_uid = user_sec_uid(user) or link.sec_uid
                    nickname = str(user.get("nickname") or "未知用户")
                    source_id, _created = await self.ctx.db.write(
                        source_store.add, "creator", sec_uid, nickname=nickname,
                        avatar_url=avatar_url(user), uid=str(user.get("uid") or ""),
                        cloud_count=to_int(user.get("aweme_count")))
                    added_ids.append(source_id)
                    ok += 1
                    events.emit("monitor_added", url=line, row={
                        "sec_uid": sec_uid, "nickname": nickname, "avatar_url": avatar_url(user),
                        "homepage_url": f"https://www.douyin.com/user/{sec_uid}",
                        "enabled": True, "last_check_note": "检查中"})
                    events.log(f"已添加关注主播：{nickname}（已自动启用监控）")
                except asyncio.CancelledError:
                    for rest in lines[index:]:
                        events.emit("monitor_add_failed", url=rest, text="任务已停止")
                    events.activity("monitor-add", "")
                    raise
                except Exception as exc:
                    failed += 1
                    events.emit("monitor_add_failed", url=line, text=explain(exc))
                    events.log(f"无法添加：{line}（{explain(exc)}）")
            self.changed()
            events.activity("monitor-add", f"添加完成 {ok}/{len(lines)}"
                            + (f" 失败{failed}" if failed else ""), running=False)
            if added_ids:
                await self._full_scan_ids(added_ids)

        self.ctx.tasks.submit("monitor-add", run, scope="monitor", lane="monitor-discover",
                              key="monitor-add:" + "\n".join(lines))
        return True

    async def set_enabled(self, sec_uids: Sequence[str], enabled: bool) -> bool:
        ids = list((await self.ctx.db.read(source_store.ids_for, "creator", sec_uids)).values())
        if not ids:
            return False
        await self.ctx.db.write(source_store.set_enabled, ids, enabled)
        self.ctx.events.log(f"已{'开启' if enabled else '暂停'} {len(ids)} 个主播的自动监控。")
        if enabled:
            self.ctx.queue.wake()
        self.changed()
        return True

    async def set_comments(self, sec_uids: Sequence[str], enabled: bool) -> bool:
        """Save (or stop saving) these creators' comments; turning it on starts at once."""
        ids = list((await self.ctx.db.read(source_store.ids_for, "creator", sec_uids)).values())
        if not ids:
            return False
        await self.ctx.db.write(source_store.set_comments, ids, enabled)
        if self.comments is not None:
            if enabled:
                self.comments.kick()
            else:
                self.comments.cancel_sources(ids)
        self.ctx.events.log(f"已{'开启' if enabled else '关闭'} {len(ids)} 个主播的评论保存"
                            + ("，后台会慢速读取已下载作品的评论。" if enabled else "。"))
        self.changed()
        return True

    # ── scanning ────────────────────────────────────────────────────

    async def _ids(self, sec_uids: Sequence[str]) -> List[int]:
        mapping = await self.ctx.db.read(source_store.ids_for, "creator", sec_uids)
        return [mapping[sec_uid] for sec_uid in dict.fromkeys(sec_uids) if sec_uid in mapping]

    async def check(self, sec_uids: Sequence[str]) -> bool:
        ids = await self._ids(sec_uids)
        if not ids:
            return False
        self._submit_checks(ids, automatic=False)
        return True

    def _submit_checks(self, ids: List[int], *, automatic: bool) -> None:
        async def run(_task) -> None:
            scheduler.set_priority(scheduler.CREATOR_SCAN if automatic else scheduler.USER)
            activity = self.ctx.activity
            failed = 0
            scanned = 0
            try:
                for source_id in ids:
                    source = await self.ctx.db.read(source_store.get, source_id)
                    if source is None or (automatic and (
                            not source["enabled"]
                            or int(source.get("paused_until") or 0) > time.time())):
                        continue
                    if scanned and not self.ctx.settings.scan_fast:
                        gap = random.uniform(*(CHECK_GAP if automatic else MANUAL_GAP))
                        activity.set("check", name="", waiting_until=time.time() + gap)
                        await asyncio.sleep(gap)
                    activity.set("check", name=str(source.get("nickname") or source["sec_uid"]),
                                 failed=failed, waiting_until=0)
                    outcome = await self._scan(source, "incremental")
                    failed += bool(outcome.error)
                    scanned += 1
            finally:
                activity.clear("check")

        key = "monitor-auto" if automatic else "monitor-check:" + ",".join(map(str, ids))
        self.ctx.tasks.submit("monitor-check", run, scope="monitor", lane="monitor-discover",
                              key=key, kind="automatic" if automatic else "user",
                              priority=20 if automatic else 10)

    async def full_scan(self, sec_uids: Sequence[str], validate_local: bool = False) -> bool:
        ids = await self._ids(sec_uids)
        if not ids:
            return False
        if validate_local:
            members = await self.ctx.db.read(source_store.members, ids)
            await self.library.validate(members)
        await self._full_scan_ids(ids)
        return True

    async def _full_scan_ids(self, ids: List[int], *, automatic: bool = False) -> None:
        batch, added = await self.ctx.db.write(scan_store.begin, ids)
        if not added:
            return
        self.ctx.events.log(f"已排队全量扫描：{len(added)} 个主播。")

        async def run(_task) -> None:
            scheduler.set_priority(scheduler.CREATOR_FULL_SCAN if automatic else scheduler.CREATOR_SCAN)
            activity = self.ctx.activity
            failed = 0
            try:
                for index, source_id in enumerate(added, start=1):
                    if index > 1 and not self.ctx.settings.scan_fast:
                        await asyncio.sleep(random.uniform(*FULL_GAP))
                    source = await self.ctx.db.read(source_store.get, source_id)
                    if source is None:
                        await self.ctx.db.write(scan_store.finish_source, batch, source_id,
                                                "主播已移除")
                        continue
                    await self.ctx.db.write(scan_store.start_source, batch, source_id)
                    self._full_source = source_id
                    activity.set("full", index=index, total=len(added), failed=failed,
                                 name=str(source.get("nickname") or source["sec_uid"]), seen=0)
                    outcome = await self._scan(source, "full", batch=batch, index=index,
                                               total=len(added))
                    failed += not outcome.complete
                    await self.ctx.db.write(scan_store.finish_source, batch, source_id,
                                            outcome.error if not outcome.complete else "")
            except asyncio.CancelledError:
                await self.ctx.db.write(scan_store.interrupt, batch, "扫描任务已停止")
                raise
            finally:
                self._full_source = 0
                activity.clear("full")
                self.ctx.events.emit("full_scan_done", sec_uid="")

        self.ctx.tasks.submit("monitor-full-scan", run, scope="monitor", lane="monitor-heavy",
                              key=f"monitor-full-scan:{batch}:{','.join(map(str, added))}",
                              kind="automatic" if automatic else "user")

    async def _scan(self, source: Dict[str, Any], mode: str, *, batch: int = 0,
                    index: int = 1, total: int = 1) -> Outcome:
        name = str(source.get("nickname") or source["sec_uid"])
        source_id = int(source["id"])
        full = mode == "full"
        events = self.ctx.events
        if full:
            reason = "首次全量扫描" if not source.get("last_full_at") else "全量扫描"
            events.log(f"{name}：{reason}，开始扫描全部作品。")
        self._scan_notes[source_id] = "扫描中：已扫描 0 个作品"
        self.changed()

        def progress(outcome: Outcome) -> None:
            self._scan_notes[source_id] = f"扫描中：已扫描 {outcome.seen} 个作品"
            if full:
                self.ctx.activity.set("full", seen=outcome.seen)
                if batch:
                    self.ctx.background(self.ctx.db.write(scan_store.progress, batch, source_id,
                                                          outcome.seen))
            self.changed()

        try:
            outcome = await self.scanner.scan(source, mode, self.policy, batch=batch,
                                              progress=progress,
                                              backlog=lambda: self.ctx.queue.backlog)
        finally:
            self._scan_notes.pop(source_id, None)
            self.changed()
        if outcome.error:
            if "登录" in outcome.error:
                self._login_pause_until = time.time() + 600
            events.log(f"{name} 检查失败：{outcome.error}")
        else:
            pending = await self.ctx.db.read(source_store.pending_count, source_id)
            events.log(f"{'全量扫描' if full else '扫描'}完成：{name} · 检查 {outcome.seen} 个 · "
                       f"新增 {len(outcome.queued)} 个 · 待下载 {pending} 个")
            if full and self.comments is not None and source.get("comments_enabled"):
                self.comments.kick()    # the scan refreshed the works' comment counts
        await self._maybe_rename_folder(source_id)
        return outcome

    async def _maybe_rename_folder(self, source_id: int) -> None:
        """Follow a nickname change once none of the creator's works is downloading."""
        source = await self.ctx.db.read(source_store.get, source_id)
        if source is None or source_id in self.ctx.queue.busy_sources():
            return
        layout = self.ctx.layout()
        if layout.author_style != "nickname":
            return
        from storage import folders

        root = layout.root / "关注主播"
        old_names = await self.ctx.db.read(lambda db: [str(row[0]) for row in db.execute(
            "SELECT DISTINCT author_name FROM works WHERE author_sec_uid = ? "
            "AND author_name <> ''", (source["sec_uid"],))])
        result = await asyncio.to_thread(
            folders.reconcile_folder, root, str(source["sec_uid"]), str(source["nickname"]),
            uid=str(source.get("uid") or ""), old_names=old_names)
        for old, new in result.moves:
            await self.ctx.db.write(work_store.rewrite_paths, str(old), str(new))
        if result.changed:
            moved = "、".join(filter(None, [result.renamed_from, *result.merged]))
            self.ctx.events.log(f"主播文件夹已整理：{moved} → {result.folder.name}")

    async def full_scan_progress(self) -> Optional[Dict[str, Any]]:
        return await self.ctx.db.read(scan_store.snapshot)

    # ── validation & deletion ───────────────────────────────────────

    def validate_local_files(self, sec_uids: Sequence[str]) -> bool:
        async def run(_task) -> None:
            events = self.ctx.events
            try:
                events.emit("local_validate_progress", scanned=0, total=0)
                ids = await self._ids(sec_uids) if sec_uids else None
                members = await self.ctx.db.read(source_store.members, ids) \
                    if ids is not None else None
                lost = await self.library.validate(
                    members, lambda done, total: events.emit_latest(
                        "validate", 0.2, "local_validate_progress", scanned=done, total=total))
                self.changed()
                events.emit("local_validate_done", reset_count=len(lost))
                if lost:
                    events.log(f"已将 {len(lost)} 个本地文件缺失的作品重新加入下载队列。")
            except asyncio.CancelledError:
                events.error("localValidate", "已停止后台任务")
                raise
            except Exception as exc:
                events.error("localValidate", explain(exc))

        self.ctx.tasks.submit("monitor-validate-local", run, scope="monitor",
                              lane="monitor-validate", key="monitor-validate-local")
        return True

    async def delete(self, sec_uids: Sequence[str], delete_local: bool) -> bool:
        ids = await self._ids(sec_uids)
        if not ids:
            return False
        await self.ctx.db.write(source_store.set_enabled, ids, False)

        async def run(_task) -> None:
            cleanup = {"works_deleted": 0, "files_deleted": 0, "files_failed": 0, "error": ""}
            if delete_local:
                try:
                    cleanup.update(await self.library.delete_source_files(ids))
                except Exception as exc:
                    cleanup.update(files_failed=1, error=explain(exc))
            await self.ctx.db.write(scan_store.forget_sources, ids)
            count = await self.ctx.db.write(source_store.delete, ids)
            await self.ctx.db.write(work_store.cancel_orphans)
            self.changed()
            self.ctx.events.emit("monitor_delete_done", creator_count=count,
                                 sec_uids=list(sec_uids), delete_local_works=delete_local,
                                 **cleanup)
            self.ctx.events.log(f"已移除 {count} 个关注主播。")

        self.ctx.tasks.submit("monitor-delete", run, scope="monitor", lane="control",
                              key="monitor-delete:" + ",".join(map(str, ids)), priority=0)
        self.changed()
        return True

    # ── importing the following list ────────────────────────────────

    def fetch_following(self, account: str = ""):
        """Read an account's following list (``""``: the main account's).

        Returns the request id the progress events carry (``False``: none).
        """
        account = str(account or "")
        if account == self.ctx.settings.main_account:
            account = ""
        if self.ctx.tasks.get(self._following_key) is not None:
            if account == self._following_account:
                return self._following_request or False
            self.cancel_following()
        self._following_seq += 1
        request_id = f"following-{self._following_seq}"
        self._following_request = request_id
        self._following = {}

        async def run(_task) -> None:
            events = self.ctx.events
            use_account(account)
            try:
                events.emit("following_progress", request_id=request_id, loaded=0, pages=0,
                            total_hint=0)
                me = await self.ctx.client.self_profile()
                owner = user_sec_uid(me)
                if not owner:
                    raise RuntimeError("未获取到当前登录账号资料")
                total_hint = to_int((me or {}).get("following_count"))
                existing = {row["sec_uid"] for row in
                            await self.ctx.db.read(source_store.all_of, ["creator"])}
                items: Dict[str, Dict[str, Any]] = {}
                count = 0
                async for page in pages(lambda cursor: self.ctx.client.following_page(
                        owner, cursor), what="关注列表"):
                    count += 1
                    for raw in page.dict_items:
                        user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
                        sec_uid = user_sec_uid(user)
                        if not sec_uid or sec_uid in items:
                            continue
                        items[sec_uid] = {
                            "sec_uid": sec_uid,
                            "nickname": str(user.get("nickname") or "").strip(),
                            "unique_id": str(user.get("unique_id") or user.get("short_id") or ""),
                            "avatar_url": avatar_url(user),
                            "signature": str(user.get("signature") or "").strip(),
                            "homepage_url": f"https://www.douyin.com/user/{sec_uid}",
                            "is_existing": sec_uid in existing, "can_import": sec_uid not in existing,
                        }
                    events.emit("following_progress", request_id=request_id, loaded=len(items),
                                pages=count, total_hint=total_hint)
                self._following[request_id] = items
                events.emit("following_ready", request_id=request_id, result={
                    "account": {"sec_uid": owner, "nickname": str((me or {}).get("nickname") or ""),
                                "avatar_url": avatar_url(me)},
                    "items": list(items.values()), "pages": count, "total_hint": total_hint})
            except asyncio.CancelledError:
                events.emit("following_cancelled", request_id=request_id)
                raise
            except LoginRequired:
                events.emit("following_error", request_id=request_id,
                            text="登录已失效，请重新登录抖音。")
            except Exception as exc:
                events.emit("following_error", request_id=request_id, text=explain(exc))
            finally:
                if self._following_request == request_id:
                    self._following_request = ""

        # Its own lane: a person is waiting on it, so it must not queue behind
        # a batch of creator checks (which can run for many minutes).
        self._following_key = f"monitor-following-fetch:{request_id}"
        self._following_account = account
        self.ctx.tasks.submit("monitor-following-fetch", run, scope="monitor",
                              lane="following", key=self._following_key, priority=5)
        return request_id

    def cancel_following(self) -> bool:
        task = self.ctx.tasks.get(self._following_key)
        request_id = self._following_request
        if task is None or not self.ctx.tasks.cancel(self._following_key):
            return False
        if task.state != STOPPING:
            # A queued task is dropped without running, so nothing else says so.
            self._following_request = ""
            self.ctx.events.emit("following_cancelled", request_id=request_id)
        return True

    def import_following(self, request_id: str, sec_uids: Sequence[str]) -> bool:
        snapshot = self._following.get(str(request_id)) or {}
        ids = [str(value) for value in dict.fromkeys(sec_uids) if str(value)]
        if not snapshot or not ids:
            return False

        async def run(_task) -> None:
            events = self.ctx.events
            try:
                added, skipped, invalid = [], 0, 0
                for sec_uid in ids:
                    row = snapshot.get(sec_uid)
                    if not row or not row.get("can_import"):
                        invalid += 1
                        continue
                    source_id, created = await self.ctx.db.write(
                        source_store.add, "creator", sec_uid, nickname=row["nickname"],
                        avatar_url=row["avatar_url"], enable=False)
                    if created:
                        added.append(source_id)
                    else:
                        skipped += 1
                self._following.pop(str(request_id), None)
                self.changed()
                if added:
                    events.log(f"已导入关注主播 {len(added)} 个，正在准备首次备份。")
                    await self._full_scan_ids(added)
                events.emit("following_imported", request_id=request_id, added=len(added),
                            skipped=skipped, invalid=invalid)
            except Exception as exc:
                events.emit("following_import_error", request_id=request_id, text=explain(exc))

        self.ctx.tasks.submit("monitor-following-import", run, scope="monitor",
                              lane="monitor-discover",
                              key=f"monitor-following-import:{request_id}")
        return True
