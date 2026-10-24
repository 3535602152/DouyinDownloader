from __future__ import annotations

from types import SimpleNamespace

from douyin.pages import Page
from services.feed import FollowFeed
from services.scanning import Scanner
from storage import sources, works
from tests.conftest import video_payload


class Account:
    async def current_sec_uid(self):
        return "ME"


class FeedClient:
    def __init__(self, feed_pages, fail=False):
        self.feed_pages = list(feed_pages)
        self.fail = fail
        self.calls = []

    async def following_page(self, sec_uid, cursor=0, count=20):
        self.calls.append(("following", sec_uid))
        return Page.parse({"status_code": 0, "has_more": 0,
                           "followings": [{"sec_uid": "C"}, {"sec_uid": "OTHER"}]},
                          ("followings",))

    async def follow_feed(self, cursor=0, count=20):
        self.calls.append(("feed", cursor))
        if self.fail:
            raise RuntimeError("empty 200")
        return self.feed_pages.pop(0)


def feed_page(items, has_more=False, cursor=0):
    return Page.parse({"status_code": 0, "has_more": int(has_more), "cursor": cursor,
                       "aweme_list": items}, ("aweme_list",))


def context(db, client):
    handed = []
    queue = SimpleNamespace(wake=lambda: None, hand_over=handed.extend)
    return SimpleNamespace(db=db, client=client, queue=queue,
                           events=SimpleNamespace(log=lambda text: None)), handed


async def test_the_feed_queues_new_works_of_monitored_creators(db):
    creator, _ = await db.write(sources.add, "creator", "C", nickname="主播")
    client = FeedClient([feed_page([video_payload("1", sec_uid="C"),
                                    video_payload("2", sec_uid="NOT-MONITORED")])])
    ctx, handed = context(db, client)
    feed = FollowFeed(ctx, Account(), Scanner._record_page)
    assert await feed.run() == 1
    assert await db.read(works.statuses, ["1", "2"]) == {"1": "pending"}
    assert [work["aweme_id"] for work in handed] == ["1"]      # the payload goes along
    # Followed creators are covered by the feed; others keep their own checks.
    assert feed.covers({"sec_uid": "C"}) and not feed.covers({"sec_uid": "NOT-FOLLOWED"})
    assert not feed.due()


async def test_a_failing_feed_covers_nobody(db):
    await db.write(sources.add, "creator", "C")
    ctx, _handed = context(db, FeedClient([], fail=True))
    feed = FollowFeed(ctx, Account(), Scanner._record_page)
    assert await feed.run() == 0
    assert not feed.covers({"sec_uid": "C"})                   # back to per-creator checks
