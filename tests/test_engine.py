from __future__ import annotations

import asyncio
import time
from pathlib import Path

from engine.downloader import PERMANENT, RETRY, SUCCESS, WorkDownloader, WorkRequest
from engine.events import EventBus
from engine.queue import DownloadQueue
from engine.runtime import CANCELLED, DONE, TaskManager
from storage import sources, works
from storage.files import DownloadContext, Layout
from tests.conftest import FakeClient, FakeFetcher, gallery_payload, video_payload


class FakeBrowser:
    enabled = True

    def __init__(self, detail=None):
        self.detail = detail
        self.asked = 0

    async def work_detail(self, aweme_id):
        self.asked += 1
        return self.detail

    async def screenshot(self, sec_uid, target):
        return False


def make_downloader(tmp_path, client=None, fetcher=None, browser=None):
    return WorkDownloader(client or FakeClient(), fetcher or FakeFetcher(),
                          layout=lambda: Layout(tmp_path), quality=lambda: "highest",
                          merge_live_photos=lambda: True, browser=browser)


async def test_video_download_saves_media_extras_and_json(tmp_path):
    client = FakeClient()
    client.details["1"] = video_payload("1")
    downloader = make_downloader(tmp_path, client)
    result = await downloader.download(WorkRequest("1", DownloadContext("single_aweme")))
    assert result.outcome == SUCCESS and result.extras_missing == ()
    kinds = sorted(entry["kind"] for entry in result.files)
    assert kinds == ["cover", "json", "media"]
    assert all(Path(entry["path"]).exists() for entry in result.files)


async def test_gallery_with_live_photos(tmp_path):
    client = FakeClient()
    client.details["2"] = gallery_payload("2", images=2, live_at=(1,))
    result = await make_downloader(tmp_path, client).download(
        WorkRequest("2", DownloadContext("single_aweme")))
    assert result.outcome == SUCCESS
    positions = sorted((entry["kind"], entry["position"]) for entry in result.files
                       if entry["kind"] in ("media", "live", "motion"))
    assert positions == [("live", 1), ("media", 1), ("media", 2), ("motion", 1)]
    assert "music" not in result.extras_missing


async def test_missing_cover_does_not_block_the_work(tmp_path):
    client, fetcher = FakeClient(), FakeFetcher()
    client.details["1"] = video_payload("1")
    fetcher.fail.add("_cover")
    result = await make_downloader(tmp_path, client, fetcher).download(
        WorkRequest("1", DownloadContext("single_aweme")))
    assert result.outcome == SUCCESS and result.extras_missing == ("cover",)


async def test_cover_keeps_the_authors_cover_ahead_of_a_faster_first_frame(tmp_path):
    class LastFirstFetcher(FakeFetcher):
        """Picks the last candidate, as speed ranking may."""

        async def fetch(self, candidates, dest, **kwargs):
            return await super().fetch(list(candidates)[::-1], dest, **kwargs)

    client, fetcher = FakeClient(), LastFirstFetcher()
    payload = video_payload("1")
    payload["video"]["origin_cover"] = {"url_list": ["https://p9.example/1_first_frame.jpeg"]}
    client.details["1"] = payload
    result = await make_downloader(tmp_path, client, fetcher).download(
        WorkRequest("1", DownloadContext("single_aweme")))
    cover = next(entry["path"] for entry in result.files if entry["kind"] == "cover")
    assert Path(cover).read_bytes().endswith(b"1_cover.jpeg")


async def test_cover_fix_replaces_the_existing_cover(tmp_path):
    client, fetcher = FakeClient(), FakeFetcher()
    payload = video_payload("1")
    payload["video"]["cover_original_scale"] = {"url_list": ["https://p3.example/1_full.jpeg"]}
    client.details["1"] = payload
    downloader = make_downloader(tmp_path, client, fetcher)
    fetcher.fail.add("_full")
    first = await downloader.download(WorkRequest("1", DownloadContext("single_aweme")))
    old = Path(next(entry["path"] for entry in first.files if entry["kind"] == "cover"))
    assert old.read_bytes().endswith(b"1_cover.jpeg")

    fetcher.fail.clear()
    fixed = await downloader.download(WorkRequest("1", DownloadContext("single_aweme"),
                                                  extras_only=("cover_fix",)))
    assert fixed.extras_missing == ()
    cover = Path(next(entry["path"] for entry in fixed.files if entry["kind"] == "cover"))
    assert cover == old and cover.read_bytes().endswith(b"1_full.jpeg")
    assert not list(cover.parent.glob("*_cover_new*"))

    fetcher.fail.add("p3.example")
    failed = await downloader.download(WorkRequest("1", DownloadContext("single_aweme"),
                                                   extras_only=("cover_fix",)))
    assert failed.extras_missing == ("cover_fix",)
    assert cover.read_bytes().endswith(b"1_full.jpeg")


async def test_inaccessible_is_confirmed_before_giving_up(tmp_path):
    client = FakeClient()
    client.details["1"] = (None, "作品已删除", "inaccessible")
    downloader = make_downloader(tmp_path, client)
    first = await downloader.download(WorkRequest("1", DownloadContext("single_aweme")))
    second = await downloader.download(WorkRequest("1", DownloadContext("single_aweme"),
                                                   attempt=2))
    assert (first.outcome, second.outcome) == (RETRY, PERMANENT)
    assert second.code == "INACCESSIBLE"


async def test_expired_media_is_refreshed_through_the_browser(tmp_path):
    client, fetcher = FakeClient(), FakeFetcher()
    stale = video_payload("1")
    client.details["1"] = stale
    fetcher.fail.add("douyinvod.com/1.mp4")
    fresh = video_payload("1")
    fresh["video"]["play_addr"]["url_list"] = ["https://v5.fresh.com/1.mp4"]
    browser = FakeBrowser(fresh)
    result = await make_downloader(tmp_path, client, fetcher, browser).download(
        WorkRequest("1", DownloadContext("single_aweme")))
    assert result.outcome == SUCCESS and browser.asked == 1


async def test_task_manager_lanes_dedupe_and_cancel():
    from engine.runtime import Runtime

    events = EventBus()
    manager = TaskManager(Runtime.__new__(Runtime), events)
    order = []
    release = asyncio.Event()

    async def slow(task):
        order.append(task.key)
        await release.wait()

    first = manager.submit("a", slow, scope="link", lane="link", key="one")
    assert manager.submit("a", slow, scope="link", lane="link", key="one") is first
    second = manager.submit("b", slow, scope="link", lane="link", key="two")
    other = manager.submit("c", slow, scope="user", lane="user", key="three")
    await asyncio.sleep(0)
    assert order == ["one", "three"]                     # lanes run in parallel, FIFO inside
    assert manager.cancel("two")
    release.set()
    await first.done_event.wait()
    await other.done_event.wait()
    states = [(event["task"], event["state"]) for event in events.drain()]
    assert ("b", CANCELLED) in states and ("a", DONE) in states
    assert second.state == CANCELLED


async def test_queue_serves_creators_before_subscriptions(tmp_path, db):
    client = FakeClient()
    for aweme_id, owner in (("1", "C"), ("2", "X")):
        client.details[aweme_id] = video_payload(aweme_id, sec_uid=owner)
    creator, _ = await db.write(sources.add, "creator", "C", nickname="主播")
    liked, _ = await db.write(sources.add, "like", "ME", nickname="我")
    await db.write(sources.record_listing, liked, ["2"], seen_at=int(time.time()))
    await db.write(works.queue, ["2"], liked)
    await db.write(sources.record_listing, creator, ["1"], seen_at=int(time.time()))
    await db.write(works.queue, ["1"], creator)

    from engine.runtime import Runtime

    events = EventBus()
    tasks = TaskManager(Runtime.__new__(Runtime), events)
    downloader = make_downloader(tmp_path, client)
    queue = DownloadQueue(db, downloader, events, tasks, layout=lambda: Layout(tmp_path),
                          concurrency=lambda: 1, homepage_screenshot=lambda: False)
    finished = []
    queue.listeners.append(lambda source_id, result: finished.append(result.aweme_id))
    with tasks.hold_foreground():
        await queue._fill()
        assert not queue._running                      # a user download blocks the queue
    await queue._publish_progress()
    assert queue.round_progress() == {"done": 0, "total": 2}
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    await queue._publish_progress()
    # The total counts the works still waiting, not only the started ones.
    assert queue.round_progress() == {"done": 1, "total": 2}
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    assert finished == ["1", "2"]
    assert await db.read(works.statuses, ["1", "2"]) == {"1": "done", "2": "done"}
    files = (await db.read(works.files_of, ["1"]))["1"]
    assert any("关注主播" in entry["path"] for entry in files)
    files = (await db.read(works.files_of, ["2"]))["2"]
    assert any("喜欢" in entry["path"] for entry in files)
    assert queue.round_progress() == {"done": 2, "total": 2}


async def test_queue_retries_failures_later(tmp_path, db):
    client = FakeClient()
    client.details["1"] = None   # empty detail, browser unavailable
    creator, _ = await db.write(sources.add, "creator", "C")
    await db.write(sources.record_listing, creator, ["1"], seen_at=int(time.time()))
    await db.write(works.queue, ["1"], creator)
    from engine.runtime import Runtime

    events = EventBus()
    queue = DownloadQueue(db, make_downloader(tmp_path, client), events,
                          TaskManager(Runtime.__new__(Runtime), events),
                          layout=lambda: Layout(tmp_path), concurrency=lambda: 1,
                          homepage_screenshot=lambda: False)
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    assert await db.read(works.statuses, ["1"]) == {"1": "retry"}
    assert await db.read(works.ready_sources, ["creator"], int(time.time())) == []
    # One failed attempt asks Douyin for the detail once, not once per layer.
    assert client.calls.count(("detail", "1")) == 1


async def test_throttled_detail_pauses_the_queue(tmp_path, db):
    from engine.runtime import Runtime

    client = FakeClient()
    client.cooldown = 60.0            # the detail endpoint is in risk cooldown
    browser = FakeBrowser(detail=None)
    creator, _ = await db.write(sources.add, "creator", "C")
    await db.write(sources.record_listing, creator, ["1", "2"], seen_at=int(time.time()))
    await db.write(works.queue, ["1", "2"], creator)
    events = EventBus()
    queue = DownloadQueue(db, make_downloader(tmp_path, client, browser=browser), events,
                          TaskManager(Runtime.__new__(Runtime), events),
                          layout=lambda: Layout(tmp_path), concurrency=lambda: 1,
                          homepage_screenshot=lambda: False)
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    await queue._fill()               # paused: nothing else is claimed
    assert not queue._running and browser.asked == 1
    assert client.calls == []          # no API request while cooling down
    assert queue._paused_until - time.monotonic() > 50
    statuses = await db.read(works.statuses, ["1", "2"])
    assert statuses == {"1": "retry", "2": "pending"}
    assert any("风控" in event.get("text", "") for event in events.drain())



async def test_a_scanned_work_downloads_without_asking_for_its_detail(tmp_path, db):
    from engine.runtime import Runtime

    client = FakeClient()
    creator, _ = await db.write(sources.add, "creator", "C")
    await db.write(sources.record_listing, creator, ["1"], seen_at=int(time.time()))
    await db.write(works.queue, ["1"], creator)
    events = EventBus()
    queue = DownloadQueue(db, make_downloader(tmp_path, client), events,
                          TaskManager(Runtime.__new__(Runtime), events),
                          layout=lambda: Layout(tmp_path), concurrency=lambda: 1,
                          homepage_screenshot=lambda: False)
    # The scan hands over the listing payload, signed media URLs included.
    queue.hand_over([video_payload("1", sec_uid="C")])
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    assert await db.read(works.statuses, ["1"]) == {"1": "done"}
    assert ("detail", "1") not in client.calls


def test_a_fast_mirror_never_promotes_a_watermarked_copy():
    from engine.fetcher import HostStats

    stats = HostStats()
    stats.record("fast.example", 10_000_000, 1.0)       # the watermark's host is faster
    stats.record("slow.example", 300_000, 1.0)
    clean = ("https://slow.example/a~tplv-dy-aweme-images:q75.webp", {})
    marked = ("https://fast.example/a~tplv-dy-water-v2:xx:2160:2880.webp", {})
    other = ("https://fast.example/b~tplv-dy-aweme-images:q75.jpeg", {})
    assert stats.rank([clean, marked, other]) == [other, clean, marked]


def test_signed_urls_are_sent_exactly_as_given():
    from engine.fetcher import exact_url

    url = ("https://p3.example/a~tplv-dy-aweme-images:q75.jpeg?x-expires=1"
           "&x-signature=mrnMep%2Fl5Cp%2BRd7HfZk%3D")
    assert str(exact_url(url)) == url
    assert str(exact_url("https://p3.example/a b.jpeg")) == "https://p3.example/a%20b.jpeg"


async def test_watermark_fix_fetches_the_pictures_again(tmp_path):
    client, fetcher = FakeClient(), FakeFetcher()
    client.details["2"] = gallery_payload("2", images=2, live_at=(1,))
    downloader = make_downloader(tmp_path, client, fetcher)
    first = await downloader.download(WorkRequest("2", DownloadContext("single_aweme")))
    picture = Path(next(entry["path"] for entry in first.files
                        if entry["kind"] == "media" and entry["position"] == 1))
    marked = picture.with_suffix(".webp")          # the watermarked copy, another type
    picture.rename(marked)

    fixed = await downloader.download(WorkRequest("2", DownloadContext("single_aweme"),
                                                  extras_only=("watermark_fix",)))
    assert fixed.outcome == SUCCESS and fixed.extras_missing == ()
    positions = sorted((entry["kind"], entry["position"]) for entry in fixed.files)
    assert positions == [("media", 1), ("media", 2), ("motion", 1)]
    assert picture.exists() and not marked.exists()

    fetcher.fail.add("p3.example")
    failed = await downloader.download(WorkRequest("2", DownloadContext("single_aweme"),
                                                   extras_only=("watermark_fix",)))
    assert failed.extras_missing == ("watermark_fix",) and picture.exists()


async def test_a_scaled_gallery_is_read_again_with_a_renewed_device_id(tmp_path):
    client, fetcher = FakeClient(), FakeFetcher()
    scaled = gallery_payload("2", images=1, live_at=())
    scaled["images"][0]["url_list"] = ["https://p3.example/2~tplv-dy-aweme-images-v2:1080:1440:q75.jpeg"]
    client.details["2"] = gallery_payload("2", images=1, live_at=())
    downloader = make_downloader(tmp_path, client, fetcher)

    client.renewable = True
    result = await downloader.download(WorkRequest("2", DownloadContext("single_aweme"),
                                                   payload=scaled, fresh=True))
    assert result.outcome == SUCCESS
    assert ("renew_device_id", None) in client.calls and ("detail", "2") in client.calls
    assert "https://p3.example/2_0.jpeg" in fetcher.fetched

    client.calls.clear()
    client.renewable = False                     # renewed a moment ago: keep a fresh read
    await downloader.download(WorkRequest("3", DownloadContext("single_aweme"),
                                          payload={**scaled, "aweme_id": "3"}, fresh=True))
    assert ("detail", "3") not in client.calls


def make_queue(tmp_path, db, client=None):
    from engine.runtime import Runtime

    events = EventBus()
    tasks = TaskManager(Runtime.__new__(Runtime), events)
    return DownloadQueue(db, make_downloader(tmp_path, client), events, tasks,
                         layout=lambda: Layout(tmp_path), concurrency=lambda: 1,
                         homepage_screenshot=lambda: False)


async def _listed(db, kind, owner, ids, nickname):
    source_id, _ = await db.write(sources.add, kind, owner, nickname=nickname)
    await db.write(sources.record_listing, source_id, ids, seen_at=int(time.time()))
    await db.write(works.queue, ids, source_id)
    return source_id


async def test_the_boards_keep_creators_and_subscriptions_apart(tmp_path, db):
    await _listed(db, "creator", "C", ["1"], "主播")
    liked = await _listed(db, "like", "ME", ["2", "3"], "我")
    await _listed(db, "collect", "ME", ["4"], "我")
    queue = make_queue(tmp_path, db)
    board = await queue.board(10)
    assert [row["aweme_id"] for row in board["rows"]] == ["1"] and board["pending"] == 1
    users = await queue.user_board(10)
    assert users["pending"] == 3
    assert [(group["kind"], group["remaining"], len(group["rows"]), group["more"])
            for group in users["groups"]] == [("like", 2, 1, 1), ("collect", 1, 1, 0)]
    full = await queue.user_board(10, {liked})
    assert [row["aweme_id"] for row in full["groups"][0]["rows"]] == ["2", "3"]


async def test_a_due_creator_work_puts_a_subscription_download_back(tmp_path, db):
    client = FakeClient()
    for aweme_id, owner in (("1", "C"), ("2", "X")):
        client.details[aweme_id] = video_payload(aweme_id, sec_uid=owner)
    await _listed(db, "like", "ME", ["2"], "我")
    queue = make_queue(tmp_path, db, client)
    gate = asyncio.Event()
    fetch = queue.downloader.fetch_detail

    async def slow_detail(aweme_id, *args, **kwargs):
        if aweme_id == "2" and not gate.is_set():
            await gate.wait()
        return await fetch(aweme_id, *args, **kwargs)

    queue.downloader.fetch_detail = slow_detail
    await queue._fill()
    await asyncio.sleep(0)
    (liked_task,) = list(queue._running)
    await _listed(db, "creator", "C", ["1"], "主播")
    await queue._fill()                     # the like download is put back
    await asyncio.gather(liked_task)
    assert await db.read(works.statuses, ["2"]) == {"2": "pending"}
    gate.set()
    await queue._fill()
    assert [entry.aweme_id for entry in queue._running.values()] == ["1"]
    await asyncio.gather(*list(queue._running))
    await queue._fill()
    await asyncio.gather(*list(queue._running))
    assert await db.read(works.statuses, ["1", "2"]) == {"1": "done", "2": "done"}
