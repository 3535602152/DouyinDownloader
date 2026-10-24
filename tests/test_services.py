from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from douyin.errors import DouyinError, EmptyResponse
from douyin.pages import Page
from engine.scheduler import Scheduler
from services import comments as comment_service
from services.app import App
from storage import comments as comment_store
from storage import sources, works
from tests.conftest import FakeClient, FakeFetcher, page_of, video_payload


class ServiceClient(FakeClient):
    def set_rate(self, rate):
        pass

    async def close(self):
        pass

    async def set_cookies(self, cookies):
        pass

    async def resolve_short_url(self, url, timeout=10.0):
        return url

    async def comments(self, aweme_id, cursor=0, count=20, *, gentle=False):
        if cursor == 0:
            return Page.parse({"status_code": 0, "has_more": 1, "cursor": 20, "comments": [
                {"cid": "a", "text": "一", "reply_comment_total": 1,
                 "user": {"nickname": "甲"},
                 "sticker": {"animate_url": {"url_list": ["https://p.example/s.gif"]}}}]},
                ("comments",))
        return Page.parse({"status_code": 0, "has_more": 0, "comments": [
            {"cid": "b", "text": "二", "user": {"nickname": "乙"},
             "image_list": [{"origin_url": {"url_list": ["https://p.example/i.jpeg"]}}]}]},
            ("comments",))

    async def comment_replies(self, aweme_id, comment_id, cursor=0, count=20, *, gentle=False,
                              page_fallback=None):
        self.reply_fallbacks = [*getattr(self, 'reply_fallbacks', []), page_fallback]
        self.reply_counts = [*getattr(self, "reply_counts", []), count]
        return Page.parse({"status_code": 0, "has_more": 0, "comments": [
            {"cid": "a1", "text": "回复", "user": {"nickname": "丙"}}]}, ("comments",))


@pytest.fixture
def app(tmp_path):
    instance = App.create(tmp_path / "data", start_background=False)
    client, fetcher = ServiceClient(), FakeFetcher()
    instance.ctx.client = client
    instance.ctx.downloader.client = client
    instance.ctx.downloader.fetcher = fetcher
    instance.ctx.downloader.browser = None
    instance.ctx.settings.update(path=str(tmp_path / "downloads"))
    yield instance
    instance.close()


def run(app, coro):
    return app.ctx.call(coro, timeout=30)


async def _finish(app, name):
    for _ in range(200):
        if not any(state["name"] == name for state in app.ctx.tasks.states()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {name} did not finish")


def test_link_preview_and_download(app):
    app.ctx.client.details["1"] = video_payload("1")
    app.ctx.call(_submit(lambda: app.links.preview(["https://www.douyin.com/video/1"])))
    run(app, _finish(app, "link-preview"))
    events = app.ctx.events.drain()
    assert any(event["type"] == "preview_item" and event["item"]["aweme_id"] == "1"
               for event in events)
    assert any(event["type"] == "preview_done" and event["count"] == 1 for event in events)
    app.ctx.call(_submit(lambda: app.links.download(["1"])))
    run(app, _finish(app, "link-download"))
    events = app.ctx.events.drain()
    statuses = [event["status"] for event in events if event["type"] == "row_status"]
    assert statuses == ["下载中", "已下载"]
    progress = [event for event in events if event["type"] == "progress"][-1]
    assert (progress["success"], progress["total"]) == (1, 1)
    assert run(app, app.ctx.db.read(works.statuses, ["1"])) == {"1": "done"}
    # A second download of the same work is "已存在", in any feature.
    app.ctx.call(_submit(lambda: app.links.download(["1"])))
    run(app, _finish(app, "link-download"))
    statuses = [event["status"] for event in app.ctx.events.drain()
                if event["type"] == "row_status"]
    assert statuses[-1] == "已存在"


async def _submit(fn):
    return fn()


def test_deleting_a_creator_keeps_works_other_sources_own(app, tmp_path):
    db = app.ctx.db
    now = int(time.time())
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    liked, _ = db.write_sync(sources.add, "like", "ME")
    owned, shared = tmp_path / "owned.mp4", tmp_path / "shared.mp4"
    for path in (owned, shared):
        path.write_bytes(b"v")
    db.write_sync(sources.record_listing, creator, ["own", "both"], seen_at=now)
    db.write_sync(sources.record_listing, liked, ["both"], seen_at=now)
    db.write_sync(works.record_success, "own", origin=creator,
                  files=[{"path": str(owned), "kind": "media"}])
    db.write_sync(works.record_success, "both", origin=creator,
                  files=[{"path": str(shared), "kind": "media"}])
    assert run(app, app.monitor.delete(["C"], True))
    run(app, _finish(app, "monitor-delete"))
    done = [event for event in app.ctx.events.drain() if event["type"] == "monitor_delete_done"]
    assert done[0]["files_deleted"] == 1 and done[0]["creator_count"] == 1
    assert not owned.exists() and shared.exists()
    assert run(app, db.read(works.statuses, ["own", "both"])) == {"both": "done"}


def test_subscription_rows(app):
    ids = run(app, app.subscriptions.add({"sec_uid": "ME", "nickname": "我", "is_self": True,
                                          "favoriting_count": 12}, ["like", "collect"]))
    assert len(ids) == 2
    rows = run(app, app.subscriptions.rows())
    assert [row["kind_label"] for row in rows] == ["喜欢", "收藏"]
    assert rows[0]["cloud_count"] == 12 and rows[0]["counts"]["downloaded"] == 0
    assert rows[0]["next_scan_at"] == 0          # never scanned: due now
    assert app.ctx.call(_submit(lambda: app.subscriptions.request_scan([ids[0]], True))) == 1
    assert run(app, app.subscriptions.rows())[0]["requested"] == "full"


def test_comments_collect_every_reply(app):
    comments = run(app, app.comments.collect("9"))
    records = app.comments.flatten(comments)
    assert [(comment["cid"], reply) for comment, reply, _parent in records] == [
        ("a", False), ("a1", True), ("b", False)]
    images = run(app, app.comments._save("9", comments))
    assert images[0]["comment_id"] == "b" and images[0]["urls"]


def _fast_comments(monkeypatch):
    monkeypatch.setattr(comment_service, "PAGE_GAP", (0.0, 0.0))


def test_creator_comment_switch_saves_comment_files(app, tmp_path, monkeypatch):
    _fast_comments(monkeypatch)
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    db.write_sync(sources.record_listing, creator, ["7", "8"], seen_at=int(time.time()))
    anchor = tmp_path / "w_data.json"
    anchor.write_text("{}", encoding="utf-8")
    db.write_sync(works.record_success, "7", origin=creator,
                  files=[{"path": str(anchor), "kind": "json"}])
    assert run(app, app.monitor.rows())[0]["comments_enabled"] is False
    assert run(app, app.monitor.set_comments(["C"], True))
    assert run(app, app.monitor.rows())[0]["comments_enabled"] is True
    work = run(app, db.read(comment_store.next_due, refresh_after=0, retry_after=0))
    assert work["aweme_id"] == "7"          # "8" is not downloaded
    run(app, app.comments._sync_work(work))
    saved = tmp_path / "w_comments.json"
    kept = json.loads(saved.read_text(encoding="utf-8"))["comments"]
    assert [item["cid"] for item in kept] == ["a", "b"]
    assert kept[0]["_files"] == ["a_sticker.gif"] and kept[1]["_files"] == ["b_1.jpeg"]
    assert app.ctx.client.reply_counts == [3]       # the first 3 replies, one request
    folder = tmp_path / "w_comments"
    assert sorted(path.name for path in folder.iterdir()) == ["a_sticker.gif", "b_1.jpeg"]
    files = run(app, db.read(works.files_of, ["7"]))["7"]
    assert sorted(f["path"] for f in files if f["kind"] == "comments") == sorted(
        str(path) for path in (saved, folder / "a_sticker.gif", folder / "b_1.jpeg"))
    # A refresh keeps the pictures it already has.
    fetched = len(app.ctx.downloader.fetcher.fetched)
    run(app, app.comments._save_media(folder, kept))
    assert len(app.ctx.downloader.fetcher.fetched) == fetched
    assert run(app, db.read(comment_store.next_due, refresh_after=0, retry_after=0)) is None
    assert any("评论已保存" in event.get("text", "") for event in app.ctx.events.drain()
               if event["type"] == "log")


def test_second_pass_reads_every_comment_and_gives_way_to_new_works(app, tmp_path,
                                                                     monkeypatch):
    _fast_comments(monkeypatch)
    db = app.ctx.db
    full_due = {"refresh_after": 0, "retry_after": 0, "grow_min": 20}
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    db.write_sync(sources.record_listing, creator, ["7", "9"], seen_at=int(time.time()))
    anchor = tmp_path / "w_data.json"
    db.write_sync(works.record_success, "7", origin=creator,
                  files=[{"path": str(anchor), "kind": "json"}])
    db.write_sync(sources.set_comments, [creator], True)
    assert run(app, db.read(comment_store.next_full, **full_due)) is None   # first pass first
    run(app, app.comments._sync_work(
        run(app, db.read(comment_store.next_due, refresh_after=0, retry_after=0))))
    work = run(app, db.read(comment_store.next_full, **full_due))
    assert work["aweme_id"] == "7" and work["full_cursor"] == 0

    # A new work is downloaded: the second pass stops after its page.
    db.write_sync(works.record_success, "9", origin=creator,
                  files=[{"path": str(tmp_path / "n_data.json"), "kind": "json"}])
    app.ctx.client.reply_counts = []
    run(app, app.comments._full_slice(work))
    state = db.read_sync(lambda c: dict(c.execute(
        "SELECT * FROM comment_sync WHERE aweme_id = '7'").fetchone()))
    assert (state["full_cursor"], state["full_done"], state["status"]) == (20, 0, "done")
    assert app.ctx.client.reply_counts == [20]      # every reply, a full page at a time
    assert run(app, db.read(comment_store.next_due, refresh_after=0, retry_after=0)
               )["aweme_id"] == "9"

    # Its first pass done, the second resumes where it stopped.
    run(app, app.comments._sync_work(
        run(app, db.read(comment_store.next_due, refresh_after=0, retry_after=0))))
    work = next(row for row in run(app, db.read(comment_store.full_list, **full_due, limit=5))
                if row["aweme_id"] == "7")
    assert work["full_cursor"] == 20
    run(app, app.comments._full_slice(work))
    state = db.read_sync(lambda c: dict(c.execute(
        "SELECT * FROM comment_sync WHERE aweme_id = '7'").fetchone()))
    assert state["full_done"] == 1
    saved = json.loads((tmp_path / "w_comments.json").read_text(encoding="utf-8"))
    assert [item["cid"] for item in saved["comments"]] == ["a", "b"]
    assert [reply["cid"] for reply in saved["comments"][0]["_replies"]] == ["a1"]
    assert saved["total"] == 3
    # A first-pass refresh merges: the comments of the second pass stay.
    run(app, app.comments._save("7", [{"cid": "a", "text": "一", "_order": 0}], replace=False))
    assert run(app, db.read(comment_store.saved_count, "7")) == 3
    assert any("评论已补全" in event.get("text", "") for event in app.ctx.events.drain()
               if event["type"] == "log")


def test_comment_board_lists_the_queue(app, tmp_path):
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    db.write_sync(sources.set_comments, [creator], True)
    db.write_sync(sources.record_listing, creator, ["7"], seen_at=int(time.time()))
    db.write_sync(works.save_works, [{**video_payload("7", desc="作品七"),
                                      "statistics": {"comment_count": 12}}])
    db.write_sync(works.record_success, "7", files=[{"path": str(tmp_path / "w.mp4")}])
    board = run(app, app.comments.board(10))
    assert board["pending"] == 1 and board["phase"] == "idle"
    assert board["rows"] == [{"aweme_id": "7", "title": "作品七", "creator": "主播",
                              "comment_count": 12, "read": 0, "state": "queued",
                              "stage": "first"}]
    assert (board["limit"], board["reply_limit"]) == (50, 3)
    assert "pending" in run(app, app.ctx.queue.board(10))
    assert app.ctx.activity.line()["text"] == ""      # not on the scan lane


def test_collect_keeps_only_the_first_comments(app):
    comments = run(app, app.comments.collect("9", limit=1, reply_limit=3))
    assert [item["cid"] for item in comments] == ["a"]
    assert [reply["cid"] for reply in comments[0]["_replies"]] == ["a1"]


def test_comment_worker_reads_due_works_then_idles(app, tmp_path, monkeypatch):
    _fast_comments(monkeypatch)
    monkeypatch.setattr(comment_service, "WORK_GAP", (0.0, 0.0))
    monkeypatch.setattr(type(app.ctx.settings), "logged_in", property(lambda _self: True))
    monkeypatch.setattr(app.ctx.scheduler, "warm", lambda: True)
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    db.write_sync(sources.record_listing, creator, ["7"], seen_at=int(time.time()))
    db.write_sync(works.record_success, "7", origin=creator,
                  files=[{"path": str(tmp_path / "w_data.json"), "kind": "json"}])
    db.write_sync(sources.set_comments, [creator], True)

    async def drive():
        app.comments.start()
        for _ in range(200):
            if (tmp_path / "w_comments.json").exists() and app.comments._phase == "idle":
                break
            await asyncio.sleep(0.02)
        app.comments.stop()

    run(app, drive())
    assert (tmp_path / "w_comments.json").exists()
    assert any("本轮 1 个作品" in event.get("text", "") for event in app.ctx.events.drain()
               if event["type"] == "log")


def test_comment_sync_waits_out_risk_and_resumes(app, monkeypatch):
    _fast_comments(monkeypatch)
    client = app.ctx.client
    original, calls = client.comments, []

    async def flaky(aweme_id, cursor=0, count=20, *, gentle=False):
        calls.append((cursor, gentle))
        if len(calls) == 2:
            raise EmptyResponse("empty 200", path="/aweme/v1/web/comment/list/")
        return await original(aweme_id, cursor, count)

    client.comments = flaky
    pauses = []

    async def pause(exc):
        pauses.append(str(exc))
        app.comments._pause_until = 0     # the test does not wait hours

    app.comments._risk_pause = pause
    comments = run(app, app.comments.collect("9", paced=True))
    assert [item["cid"] for item in comments] == ["a", "b"]
    assert calls == [(0, True), (20, True), (20, True)]   # the failed page is asked again
    assert pauses == ["empty 200"]


def test_empty_reply_page_does_not_pause_comment_sync(app, monkeypatch):
    _fast_comments(monkeypatch)
    client = app.ctx.client
    fallbacks = []

    async def empty(aweme_id, comment_id, cursor=0, count=20, *, gentle=False,
                    page_fallback=None):
        fallbacks.append(page_fallback)
        raise EmptyResponse("empty 200", path="/aweme/v1/web/comment/list/reply/")

    client.comment_replies = empty
    pauses = []

    async def pause(exc):
        pauses.append(str(exc))

    app.comments._risk_pause = pause
    comments = run(app, app.comments.collect("9", paced=True, limit=50, reply_limit=3))
    assert [item["cid"] for item in comments] == ["a", "b"]
    assert comments[0]["_replies"] == []      # kept without its replies
    assert fallbacks == [True]                # tried through the logged-in page
    assert pauses == []


def test_a_work_without_comments_is_not_a_risk_signal():
    page = Page.parse({"status_code": 0, "has_more": 0, "comments": None}, ("comments",))
    assert comment_service.checked(page, "评论接口").items == []
    with pytest.raises(DouyinError):
        comment_service.checked(Page.parse({}, ("comments",)), "评论接口")


def test_comment_risk_does_not_pause_scans():
    scheduler = Scheduler()
    scheduler._observe("risk", 30.0, "/aweme/v1/web/comment/list/")
    assert scheduler.pause_remaining() == 0
    scheduler._observe("risk", 30.0, "/aweme/v1/web/aweme/post/")
    assert scheduler.pause_remaining() > 0


def test_validate_requeues_missing_media(app, tmp_path):
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C")
    db.write_sync(sources.record_listing, creator, ["gone"], seen_at=int(time.time()))
    db.write_sync(works.record_success, "gone", origin=creator,
                  files=[{"path": str(tmp_path / "missing.mp4"), "kind": "media"}])
    lost = run(app, app.library.validate())
    assert lost == ["gone"]
    assert run(app, db.read(works.statuses, ["gone"])) == {"gone": "pending"}
    assert not Path(tmp_path / "missing.mp4").exists()


def test_user_download_report_finishes_with_an_error(app):
    from services.user_content import _Reporter

    reporter = _Reporter(app.ctx, "SEC", "like")
    reporter.scanning(error="翻页失败")
    reporter.finish(error="任务出错")
    last = [event for event in app.ctx.events.drain()
            if event["type"] == "user_download_scan"][-1]
    assert last["download_state"] == "failed" and last["error"] == "任务出错"


def test_unfinished_user_download_resumes_without_asking(app):
    app.ctx.settings.set_cookies({"sessionid": "s", "sessionid_ss": "s", "uid_tt": "u",
                                  "passport_csrf_token": "p", "ttwid": "t"})
    app.ctx.client.profiles["self"] = {"sec_uid": "ME", "nickname": "我", "uid": "1"}
    app.ctx.settings.update_ui(pending_user_download={
        "id": "x", "sec_uid": "ME", "is_self": True, "mode": "like", "limit": 0,
        "remaining_limit": 0, "increase": False, "start_time": "", "end_time": ""})
    from gui.bridge import WebBridge  # noqa: F401  (the page is never told about it)

    assert app.ctx.settings.logged_in
    assert app.ctx.call(_submit(app.user_content.resume_pending))
    run(app, _finish(app, "user-download-resume"))
    run(app, _finish(app, "user-download"))
    logs = [event["text"] for event in app.ctx.events.drain() if event["type"] == "log"]
    assert any("继续上次未完成的「喜欢」下载" in text for text in logs)
    assert ("likes:ME", 0) in app.ctx.client.calls
    assert not app.ctx.settings.ui().get("pending_user_download")


def test_scan_line_and_board(app):
    from services.activity import readable_reason

    db = app.ctx.db
    activity = app.activity
    assert activity.line()["text"] == ""
    activity.set("full", index=2, total=31, failed=1, name="许二木", seen=267)
    activity.set("check", name="仓裴", failed=0)
    line = activity.line()
    assert (line["text"], line["detail"], line["running"]) == ("全量扫描 2/31 失败1", "许二木 267", True)
    activity.clear("full")
    assert activity.line()["text"] == "正在扫描"
    app.ctx.scheduler.pause_for(45)
    paused = activity.line()
    assert paused["text"] == "风控" and 44 <= paused["countdown"] <= 45
    app.ctx.scheduler._paused_until = 0

    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="诗柝")
    db.write_sync(sources.update, creator, last_error="触发风控，暂停 30 分钟后从第 3 页继续：x",
                  failure_count=2, last_check_at=int(time.time()))
    board = run(app, activity.board())
    assert [row["kind"] for row in board["rows"]][:1] == ["增量扫描"]
    assert board["failure_count"] == 1
    failure = board["failures"][0]
    assert failure["reason"] == "触发风控，已暂停" and failure["count"] == 2
    assert readable_reason("TypeError: boom") == ""


def test_creators_are_checked_before_a_delayed_full_scan(app, monkeypatch):
    app.ctx.settings.set_cookies({"sessionid": "s", "sessionid_ss": "s", "uid_tt": "u",
                                  "passport_csrf_token": "p", "ttwid": "t"})
    day = 86400
    now = int(time.time())
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C")
    # Closed for a day: the periodic full scan is due, but not before warm-up.
    db.write_sync(sources.update, creator, last_full_at=now - 2 * day, last_check_at=now - day)
    checked, full = [], []
    monkeypatch.setattr(app.monitor, "_submit_checks",
                        lambda ids, automatic: checked.extend(ids))

    async def fake_full(ids, automatic=False):
        full.extend(ids)

    monkeypatch.setattr(app.monitor, "_full_scan_ids", fake_full)
    app.monitor.feed = None             # the creator's own list, not the follow feed
    assert not app.ctx.scheduler.warm()
    run(app, app.monitor._tick())
    assert checked == [creator] and full == []
    app.ctx.scheduler.started -= 3600
    checked.clear()
    run(app, app.monitor._tick())
    assert checked == [] and full == [creator]


def test_followed_creators_are_left_to_the_follow_feed(app, monkeypatch):
    app.ctx.settings.set_cookies({"sessionid": "s", "sessionid_ss": "s", "uid_tt": "u",
                                  "passport_csrf_token": "p", "ttwid": "t"})
    now = int(time.time())
    db = app.ctx.db
    followed, _ = db.write_sync(sources.add, "creator", "FOLLOWED")
    other, _ = db.write_sync(sources.add, "creator", "OTHER")
    for source_id in (followed, other):
        db.write_sync(sources.update, source_id, last_full_at=now - 3600,
                      last_check_at=now - 3600)
    feed = app.monitor.feed
    feed.followed = {"FOLLOWED"}
    feed._ok_at = time.monotonic()                  # the feed works
    feed._next_run = time.monotonic() + 600          # and is not due
    checked = []
    monkeypatch.setattr(app.monitor, "_submit_checks",
                        lambda ids, automatic: checked.extend(ids))
    run(app, app.monitor._tick())
    assert checked == [other]


def test_the_scan_strategy_sets_the_request_spacing(app):
    from engine.scheduler import BACKGROUND_SPACING

    assert app.ctx.scheduler.spacing == BACKGROUND_SPACING       # stable by default
    app.ctx.settings.update(scan_strategy="fast")
    run(app, asyncio.sleep(0.05))                              # applied on the loop
    assert app.ctx.scheduler.spacing == (0.0, 0.0)


def test_each_subscription_is_scanned_with_its_own_account(app):
    settings = app.ctx.settings
    settings.save_account("ME", {"sessionid": "me"}, nickname="我")
    settings.save_account("ZYJ", {"sessionid": "zyj"}, nickname="ZYJ")
    db = app.ctx.db
    mine, _ = db.write_sync(sources.add, "like", "ME", nickname="我", is_self=True)
    second, _ = db.write_sync(sources.add, "collect", "ZYJ", nickname="ZYJ", is_self=True)
    public, _ = db.write_sync(sources.add, "like", "PUBLIC", nickname="路人")
    public_collect, _ = db.write_sync(sources.add, "collect", "PUBLIC", nickname="路人")
    subscriptions = app.subscriptions
    app.ctx.scheduler.started -= 3600

    # Asked for by hand: scanned right away, with the second account's login.
    app.ctx.call(_submit(lambda: subscriptions.request_scan([second], True)))
    source, mode = run(app, subscriptions._next())
    assert (source["id"], mode, source["_viewer"], source["_owned"], source["_manual"]) == \
        (second, "full", "ZYJ", True, True)
    # Someone else's likes are read with the main account and never deleted from.
    subscriptions._turns = {"ME": 2.0, "ZYJ": 1.0}
    picked = run(app, subscriptions._next())[0]
    assert picked["id"] == second                       # ZYJ's turn: it scanned longest ago
    subscriptions._turns = {"ZYJ": 2.0}
    picked = run(app, subscriptions._next())[0]
    assert picked["_viewer"] == "ME"
    assert {picked["id"]} <= {mine, public}
    if picked["id"] == public:
        assert not picked["_owned"] and not picked["is_self"]
    # Another account's collection cannot be read; an expired login is skipped.
    settings.save_account("ZYJ", expired=True)
    run(app, subscriptions._next())
    assert set(subscriptions.blocked) == {second, public_collect}
    rows = {row["id"]: row for row in run(app, subscriptions.rows())}
    assert "失效" in rows[second]["blocked"] and not rows[mine]["blocked"]
    assert rows[second]["is_self"] and not rows[public]["is_self"]
    assert public_collect in app.activity.skipped()
    # Creator scans go first.
    subscriptions.creator_busy = lambda: True
    assert run(app, subscriptions._next()) is None


def test_a_library_account_browses_its_likes_with_its_own_login(app):
    from douyin.client import current_account

    settings = app.ctx.settings
    settings.save_account("ME", {"sessionid": "me"}, nickname="我")
    settings.save_account("ZYJ", {"sessionid": "zyj"}, nickname="ZYJ")
    client = app.ctx.client
    seen = []

    async def self_profile():
        seen.append(("profile", current_account()))
        return {"sec_uid": current_account() or "ME", "nickname": "ZYJ", "uid": "2"}

    async def user_likes(sec_uid, cursor=0, count=20):
        seen.append(("likes", current_account()))
        return page_of([video_payload("1")])

    client.self_profile, client.user_likes = self_profile, user_likes
    profile = run(app, app.account.lock(None, "ZYJ"))
    assert (profile["sec_uid"], profile["is_self"], profile["viewer"]) == ("ZYJ", True, "ZYJ")
    assert settings.ui()["last_user_account"]["viewer"] == "ZYJ"
    assert app.ctx.call(_submit(lambda: app.user_content.browse(profile, "like", "r1", "", 0)))
    run(app, _finish(app, "user-preview-stream"))
    assert seen == [("profile", "ZYJ"), ("likes", "ZYJ")]
    with pytest.raises(RuntimeError, match="账号库里没有"):
        run(app, app.account.lock(None, "NOBODY"))


def test_a_subscription_scan_gives_way_to_creator_scans(app):
    app.ctx.settings.save_account("ME", {"sessionid": "me"})
    db = app.ctx.db
    liked, _ = db.write_sync(sources.add, "like", "ME", is_self=True)
    client = app.ctx.client
    client.pages["likes:ME"] = [
        page_of([video_payload("1")], has_more=True, cursor=10),
        page_of([video_payload("2")], has_more=False)]
    from services.scanning import subscription_policy

    policy = subscription_policy("like")
    policy.page_wait = lambda full, backlog: 0.0
    source = db.read_sync(sources.get, liked)
    outcome = run(app, app.subscriptions.scanner.scan(source, "full", policy,
                                                      should_yield=lambda: True))
    assert outcome.yielded and not outcome.complete and outcome.pages == 1
    saved = db.read_sync(sources.get, liked)
    assert saved["full_cursor"] == 10 and saved["full_epoch"] > 0
    # Next time the full scan resumes from the saved page.
    outcome = run(app, app.subscriptions.scanner.scan(saved, "full", policy))
    assert outcome.complete and ("likes:ME", 10) in client.calls


def test_rebuilding_the_backlog_rescans_every_source(app, monkeypatch):
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C")
    db.write_sync(sources.record_listing, creator, ["old"], seen_at=int(time.time()))
    db.write_sync(works.queue, ["old"], creator)
    scanned = []

    async def full_scan(sec_uids, validate_local=False):
        scanned.extend(sec_uids)
        return True

    monkeypatch.setattr(app.monitor, "full_scan", full_scan)
    assert run(app, app.rebuild_backlog()) == 1
    assert run(app, db.read(works.statuses, ["old"])) == {}
    assert scanned == ["C"]


def test_qr_login_shows_the_code_in_the_app(app, monkeypatch):
    import services.account as account_module

    seen = {}
    jar = {"sessionid": "s", "sessionid_ss": "s", "uid_tt": "u",
           "passport_csrf_token": "p", "ttwid": "t"}

    async def fake_qr_login(*, headless, user_agent, on_qr, on_status, **_):
        seen["headless"] = headless
        on_qr("data:image/png;base64,QR")
        on_status("scanned")
        return jar

    monkeypatch.setattr(account_module, "qr_login", fake_qr_login)
    assert app.ctx.call(_submit(app.account.start_qr_login))
    run(app, _finish(app, "qr-login"))
    events = app.ctx.events.drain()
    assert seen["headless"] is True
    shown = [event for event in events if event["type"] == "login_qr"]
    assert shown == [{"type": "login_qr", "image": "data:image/png;base64,QR"},
                     {"type": "login_qr", "status": "scanned"}]
    assert any(event["type"] == "login" and event["ok"] is True for event in events)
    assert app.ctx.settings.logged_in


def test_failed_qr_confirmation_does_not_replace_existing_login(app, monkeypatch):
    import services.account as account_module

    app.ctx.settings.set_cookies({"sessionid": "previous-account"})

    async def unconfirmed(**kwargs):
        return {"passport_csrf_token": "guest-only"}

    monkeypatch.setattr(account_module, "qr_login", unconfirmed)
    assert app.ctx.call(_submit(app.account.start_qr_login))
    run(app, _finish(app, "qr-login"))
    assert app.ctx.settings.cookies["sessionid"] == "previous-account"
    assert any(event["type"] == "login" and event["ok"] is False
               for event in app.ctx.events.drain())


def test_qr_login_can_be_cancelled(app, monkeypatch):
    import services.account as account_module

    async def never_scanned(**_):
        await asyncio.sleep(60)

    monkeypatch.setattr(account_module, "qr_login", never_scanned)
    assert app.ctx.call(_submit(app.account.start_qr_login))
    assert app.ctx.call(_submit(app.account.cancel_qr_login))
    run(app, _finish(app, "qr-login"))
    assert not any(event["type"] == "login" and event["ok"] is False
                   for event in app.ctx.events.drain())


def test_creator_payload_carries_age_and_region_tags():
    from services.views import creator_payload

    creator = creator_payload({"sec_uid": "S"}, {
        "sec_uid": "S", "nickname": "n", "gender": 1, "user_age": 29,
        "province": "浙江", "city": "杭州", "country": "中国", "ip_location": "IP属地：广西",
    })
    assert (creator["age"], creator["province"], creator["city"]) == (29, "浙江", "杭州")
    local_only = creator_payload({"sec_uid": "S"})
    assert (local_only["age"], local_only["province"], local_only["city"]) == (0, "", "")
    assert creator_payload({}, {"user_age": -1})["age"] == 0


def test_listing_authors_get_their_thumbnail_avatar():
    from services.views import creator_payload

    thumb = {"avatar_thumb": {"url_list": ["https://p3.example/a.jpeg?from=1"]}}
    assert creator_payload({}, thumb)["avatar_url"] == "https://p3.example/a.jpeg"
    signed = "https://p3.example/a.jpeg?x-expires=1&x-signature=s"
    assert creator_payload({}, {"avatar_thumb": {"url_list": [signed]}})["avatar_url"] == signed


def _profile(avatar, cover, followers):
    return {"sec_uid": "S", "nickname": "主播", "unique_id": "dy1", "follower_count": followers,
            "avatar_larger": {"url_list": [avatar]}, "cover_url": [{"url_list": [cover]}],
            "province": "浙江", "city": "杭州", "user_age": 29}


def _cached_files(app):
    from storage import profiles

    for _ in range(200):
        row = app.ctx.db.read_sync(profiles.get, "S")
        if row and row["avatar_file"] and row["cover_file"]:
            return row
        time.sleep(0.02)
    raise AssertionError("profile images were not cached")


def test_creator_profile_is_cached_with_its_images_for_offline_homepages(app):
    app.ctx.client.profiles["S"] = _profile("https://p.example/a.jpeg?sig=1",
                                            "https://p.example/c.jpeg?sig=1", 10)
    online = run(app, app.creators.profile("S"))["creator"]
    assert online["source"] == "remote" and online["follower_count"] == 10
    row = _cached_files(app)
    assert Path(row["cover_file"]).is_file() and row["cover_key"] == "https://p.example/c.jpeg"

    # Offline: the last online header, with the local pictures.
    app.ctx.client.profiles.pop("S")
    offline = run(app, app.creators.profile("S"))
    assert offline["error"] and offline["creator"]["source"] == "cached"
    assert offline["creator"]["follower_count"] == 10 and offline["creator"]["city"] == "杭州"
    assert offline["creator"]["cover_path"] == row["cover_file"]
    home = run(app, app.creators.home("S", False, 0, 0))
    assert home["creator"]["source"] == "cached" and home["creator"]["unique_id"] == "dy1"

    # A re-signed URL of the same picture reuses the copy; new numbers replace the old.
    fetched = len(app.ctx.downloader.fetcher.fetched)
    app.ctx.client.profiles["S"] = _profile("https://p.example/a.jpeg?sig=2",
                                            "https://p.example/c.jpeg?sig=2", 11)
    again = run(app, app.creators.profile("S"))["creator"]
    assert again["cover_path"] == row["cover_file"] and again["follower_count"] == 11
    assert len(app.ctx.downloader.fetcher.fetched) == fetched
    assert run(app, app.creators.profile("S"))["creator"]["follower_count"] == 11

    # A new cover is downloaded again.
    app.ctx.client.profiles["S"] = _profile("https://p.example/a.jpeg?sig=3",
                                            "https://p.example/c2.jpeg?sig=3", 11)
    run(app, app.creators.profile("S"))
    for _ in range(200):
        if _cached_files(app)["cover_key"] == "https://p.example/c2.jpeg":
            break
        time.sleep(0.02)
    assert _cached_files(app)["cover_key"] == "https://p.example/c2.jpeg"


def test_a_full_scan_marks_works_gone_from_the_homepage_as_hidden(app):
    app.ctx.settings.update(scan_strategy="fast")
    db = app.ctx.db
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    items = [video_payload(aweme_id, sec_uid="C") for aweme_id in ("A", "B", "D")]
    app.ctx.client.pages["posts:C"] = [page_of(items)]
    assert run(app, app.monitor.full_scan(["C"]))
    run(app, _finish(app, "monitor-full-scan"))
    # The next full scan starts later than the works were first seen.
    db.write_sync(lambda c: c.execute("UPDATE source_works SET first_seen_at = first_seen_at - 60, "
                                      "seen_epoch = seen_epoch - 60"))
    app.ctx.client.pages["posts:C"] = [page_of([items[0], items[2]])]
    assert run(app, app.monitor.full_scan(["C"]))
    run(app, _finish(app, "monitor-full-scan"))

    assert "已不在列表 1" in db.read_sync(sources.get, creator)["last_note"]
    home = run(app, app.creators.home("C", False, 0, 0))
    labels = {work["aweme_id"]: work["offline_label"] for work in home["works"]}
    assert labels == {"A": "", "B": "作品已隐藏", "D": ""}
    hidden = run(app, app.creators.home("C", False, 0, 0, True))
    assert [work["aweme_id"] for work in hidden["works"]] == ["B"]

    # Listed again later: no longer hidden.
    db.write_sync(lambda c: c.execute("UPDATE source_works SET first_seen_at = first_seen_at - 60, "
                                      "seen_epoch = seen_epoch - 60"))
    app.ctx.client.pages["posts:C"] = [page_of(items)]
    assert run(app, app.monitor.full_scan(["C"]))
    run(app, _finish(app, "monitor-full-scan"))
    home = run(app, app.creators.home("C", False, 0, 0))
    assert all(not work["offline_label"] for work in home["works"])


def test_player_comment_panel_reads_pages_live(app):
    first = run(app, app.comments.browse("9"))
    assert first["has_more"] and first["cursor"] == 20 and not first.get("error")
    comment = first["comments"][0]
    assert (comment["cid"], comment["nickname"], comment["reply_total"]) == ("a", "甲", 1)
    assert comment["sticker"] == "https://p.example/s.gif"
    second = run(app, app.comments.browse("9", 20))
    assert not second["has_more"] and second["comments"][0]["images"][0]["full"] == \
        "https://p.example/i.jpeg"
    replies = run(app, app.comments.browse("9", 0, "", "a", 3))
    assert [reply["cid"] for reply in replies["comments"]] == ["a1"]
    assert app.ctx.client.reply_counts[-1] == 3
    # Nothing is saved by browsing.
    assert not any(app.ctx.data_dir.rglob("*_comments.json"))


def test_player_comment_panel_reads_saved_comments_and_the_authors(app):
    rows = [
        {"cid": "a", "nickname": "甲", "text": "一", "position": 0,
         "raw": {"user": {"nickname": "甲", "sec_uid": "U1"}}},
        {"cid": "a1", "is_reply": True, "parent_cid": "a", "nickname": "作者", "text": "回",
         "position": 1, "raw": {"user": {"sec_uid": "AUTH"}, "reply_to_username": "甲"}},
        {"cid": "b", "nickname": "作者", "text": "二", "position": 2,
         "raw": {"user": {"sec_uid": "AUTH"}}},
    ]
    images = [{"comment_id": "b", "index": 0, "urls": ["https://p.example/b.jpeg"],
               "preview_url": "https://p.example/b.webp", "local_path": "D:/x/b.jpeg"}]
    run(app, app.ctx.db.write(comment_store.save, "9", rows, images))

    page = run(app, app.comments.saved("9", 0, "AUTH", "", 1))
    assert (page["source"], page["total"], page["has_more"]) == ("local", 2, True)
    assert (page["comments"][0]["cid"], page["comments"][0]["reply_total"]) == ("a", 1)
    rest = run(app, app.comments.saved("9", page["cursor"], "AUTH"))
    saved = rest["comments"][0]
    assert saved["is_author"] and not rest["has_more"]
    assert saved["images"][0]["local_paths"] == ["", "D:/x/b.jpeg"]
    replies = run(app, app.comments.saved("9", 0, "AUTH", "a"))
    assert [(r["cid"], r["reply_to"], r["is_reply"]) for r in replies["comments"]] == \
        [("a1", "甲", True)]
    # The 作者 tab: the comments the author wrote or replied under, with the
    # author's replies only.
    authored = run(app, app.comments.saved("9", 0, "AUTH", author_only=True))
    assert [(c["cid"], [r["cid"] for r in c["author_replies"]])
            for c in authored["comments"]] == [("a", ["a1"]), ("b", [])]
    assert authored["total"] == 2
    assert run(app, app.comments.saved("9", 0, "", author_only=True))["comments"] == []
    assert not getattr(app.ctx.client, "reply_counts", [])      # nothing was asked online


def test_empty_comment_pages_never_pause_the_comment_features(app, monkeypatch):
    from douyin.errors import EmptyResponse
    from services import comments as module

    monkeypatch.setattr(module, "EMPTY_RETRY_WAIT", 0)
    calls = []
    original = app.ctx.client.comments

    async def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise EmptyResponse("empty 200", path="list")
        return await original(*args, **kwargs)

    app.ctx.client.comments = flaky
    page = run(app, app.comments.browse("9"))
    assert len(calls) == 2 and page["comments"][0]["cid"] == "a"     # asked once more

    # Replies are asked once, with the logged-in page as the fallback.
    run(app, app.comments.browse("9", 0, "", "a", 3))
    assert app.ctx.client.reply_fallbacks[-1] is True

    async def always_empty(*args, **kwargs):
        raise EmptyResponse("empty 200", path="reply")

    app.ctx.client.comment_replies = always_empty
    failed = run(app, app.comments.browse("9", 0, "", "a", 3))
    assert failed["error"] == "回复暂时读不到" and app.comments._pause_until == 0


def test_comment_emojis_are_cached_for_a_week(app):
    asked = []

    async def emoji_list():
        asked.append(1)
        return {"[微笑]": "https://p.example/s.png"}

    app.ctx.client.emoji_list = emoji_list
    assert run(app, app.comments.emojis()) == {"[微笑]": "https://p.example/s.png"}
    assert run(app, app.comments.emojis()) == {"[微笑]": "https://p.example/s.png"}
    assert len(asked) == 1

    async def offline():
        raise RuntimeError("offline")

    app.ctx.client.emoji_list = offline
    (app.ctx.cache_dir / "comment_emojis.json").write_text(
        '{"saved_at": 0, "emojis": {"[微笑]": "u"}}', encoding="utf-8")
    assert run(app, app.comments.emojis()) == {"[微笑]": "u"}   # stale beats nothing


def test_reading_the_following_list_never_waits_behind_creator_checks(app):
    client = app.ctx.client
    client.profiles["self"] = {"sec_uid": "ME", "nickname": "我", "following_count": 1}

    async def following_page(owner, cursor=0):
        return Page.parse({"status_code": 0, "has_more": 0, "followings": [
            {"sec_uid": "C1", "nickname": "主播一"}]}, ("followings",))

    client.following_page = following_page
    release = asyncio.Event()

    async def busy_checks(_task):
        await release.wait()

    app.ctx.call(_submit(lambda: app.ctx.tasks.submit(
        "monitor-check", busy_checks, scope="monitor", lane="monitor-discover", key="checks")))
    assert app.ctx.call(_submit(app.monitor.fetch_following))
    run(app, _finish(app, "monitor-following-fetch"))
    ready = [event for event in app.ctx.events.drain() if event["type"] == "following_ready"]
    assert [item["sec_uid"] for item in ready[0]["result"]["items"]] == ["C1"]
    app.ctx.call(_submit(release.set))


def test_cancelling_a_queued_following_read_says_so(app):
    release = asyncio.Event()

    async def hold(_task):
        await release.wait()

    app.ctx.call(_submit(lambda: app.ctx.tasks.submit(
        "hold", hold, scope="monitor", lane="following", key="hold")))
    assert app.ctx.call(_submit(app.monitor.fetch_following))
    assert app.ctx.call(_submit(app.monitor.cancel_following))
    cancelled = [event for event in app.ctx.events.drain() if event["type"] == "following_cancelled"]
    assert cancelled and app.monitor._following_request == ""
    app.ctx.call(_submit(release.set))


def test_landscape_covers_of_portrait_videos_are_queued_once(app, tmp_path):
    from PIL import Image

    db = app.ctx.db
    shapes = {"wide": ((440, 330), (1080, 1920)), "tall": ((323, 430), (1080, 1920)),
              "flat": ((440, 330), (1920, 1080))}
    for aweme_id, (cover_size, (width, height)) in shapes.items():
        payload = video_payload(aweme_id)
        payload["video"].update(width=width, height=height)
        cover = tmp_path / f"{aweme_id}_cover.jpg"
        Image.new("RGB", cover_size).save(cover)
        db.write_sync(works.record_success, aweme_id, payload=payload,
                      files=[{"path": str(cover), "kind": "cover"}])
    assert run(app, app.repair_covers()) == 1
    due = {row["aweme_id"]: row for row in db.read_sync(
        lambda c: [dict(r) for r in c.execute("SELECT * FROM downloads")])}
    assert due["wide"]["extras_missing"] == "cover_fix"
    assert due["wide"]["extras_retry_at"] > time.time()
    assert due["tall"]["extras_missing"] == due["flat"]["extras_missing"] == ""
    assert run(app, app.repair_covers()) == 0


def test_galleries_saved_with_watermarked_pictures_are_queued_once(app, tmp_path):
    from datetime import datetime

    from tests.conftest import gallery_payload

    db = app.ctx.db
    signed = "https://p3.example/{}~tplv-dy-aweme-images:q75.jpeg?x-signature=a{}b%3D"
    marked = "https://p3.example/{}~tplv-dy-water-v2:eA==:1080:1440.jpeg?x-signature=c%3D"
    now = time.time()
    # mangled: its clean URL's signature has %2F; old: same, but the picture
    # predates the JSON (kept from an earlier run); intact: no %2F.
    cases = {"mangled": ("%2F", now), "old": ("%2F", now - 86400), "intact": ("%2B", now)}
    for aweme_id, (escape, written) in cases.items():
        payload = gallery_payload(aweme_id, images=1, live_at=())
        payload["images"][0]["url_list"] = [signed.format(aweme_id, escape)]
        payload["images"][0]["download_url_list"] = [marked.format(aweme_id)]
        payload["_backup"] = {"saved_at": datetime.fromtimestamp(now).isoformat()}
        data = tmp_path / f"{aweme_id}_data.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        picture = tmp_path / f"{aweme_id}_1.jpg"
        picture.write_bytes(b"jpeg")
        os.utime(picture, (written, written))
        db.write_sync(works.record_success, aweme_id, payload=payload,
                      files=[{"path": str(picture), "kind": "media", "position": 1},
                             {"path": str(data), "kind": "json"}])
    assert run(app, app.repair_watermarks()) == 1
    due = {row["aweme_id"]: row for row in db.read_sync(
        lambda c: [dict(r) for r in c.execute("SELECT * FROM downloads")])}
    assert due["mangled"]["extras_missing"] == "watermark_fix"
    assert due["old"]["extras_missing"] == due["intact"]["extras_missing"] == ""
    assert run(app, app.repair_watermarks()) == 0


def test_a_second_login_joins_the_library_without_replacing_the_main_account(app, monkeypatch):
    import services.account as account_module

    settings = app.ctx.settings
    settings.save_account("ME", {"sessionid": "me"}, nickname="我")
    app.ctx.client.profiles["login"] = {"sec_uid": "B", "nickname": "乙", "uid": "2"}

    async def fake_qr_login(**_):
        return {"sessionid": "b", "sessionid_ss": "b"}

    monkeypatch.setattr(account_module, "qr_login", fake_qr_login)
    assert app.ctx.call(_submit(app.account.start_qr_login))
    run(app, _finish(app, "qr-login"))
    assert settings.main_account == "ME" and settings.cookies == {"sessionid": "me"}
    assert settings.account("B")["cookies"]["sessionid"] == "b"
    assert app.ctx.client.accounts["others"] == {"B": {"sessionid": "b", "sessionid_ss": "b"},
                                                 "ME": {"sessionid": "me"}}
    rows = app.ctx.call(_submit(app.account.accounts))
    assert [(row["sec_uid"], row["is_main"]) for row in rows] == [("ME", True), ("B", False)]
    # An expired second login only takes that account out of use.
    app.ctx.call(_submit(lambda: app.account.login_lost("B")))
    run(app, asyncio.sleep(0.05))
    assert settings.account("B")["expired"] and "B" not in app.ctx.client.accounts["others"]
    assert not app.ctx.scheduler.login_paused
    assert app.ctx.call(_submit(lambda: app.account.set_main("B")))
    run(app, asyncio.sleep(0.05))
    assert app.ctx.client.accounts["main"] == "B"
