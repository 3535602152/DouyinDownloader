from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from douyin.errors import HttpError, PaginationError
from services.scanning import (
    Policy,
    Scanner,
    creator_policy,
    incremental_due,
    next_scan_at,
    plan,
    subscription_policy,
)
from storage import sources, works
from tests.conftest import FakeClient, page_of, video_payload

NOW = int(time.time())


def make_context(db, client):
    queue = SimpleNamespace(wake=lambda: None, hand_over=lambda items: None)
    return SimpleNamespace(db=db, client=client, queue=queue,
                           settings=SimpleNamespace(full_scan_interval=24))


def fast(policy: Policy) -> Policy:
    policy.page_wait = lambda full, backlog: 0.0
    return policy


def test_plan_schedule():
    policy = subscription_policy("like")
    fresh = {"enabled": 1, "paused_until": 0, "last_full_at": 0}
    assert plan(fresh, policy, NOW) == "full"
    done = {"enabled": 1, "last_full_at": NOW - 3600, "last_incremental_at": NOW - 3600}
    assert plan(done, policy, NOW) is None
    assert plan({**done, "last_full_at": NOW - 86400, "last_incremental_at": NOW - 7 * 3600},
                policy, NOW) == "incremental"
    assert plan({**done, "last_full_at": NOW - 4 * 86400}, policy, NOW) == "full"
    assert plan({**done, "paused_until": NOW + 10}, policy, NOW) is None
    resumable = {**done, "full_epoch": NOW - 100, "full_seen": 40}
    assert plan(resumable, policy, NOW) == "full"
    assert next_scan_at(done, policy) == NOW - 3600 + 6 * 3600
    creator = creator_policy(SimpleNamespace(settings=SimpleNamespace(full_scan_interval=0)))
    assert plan({"enabled": 1, "last_full_at": NOW - 10 * 86400, "incremental_try_at": NOW - 30},
                creator, NOW) is None                     # periodic full scans switched off


def test_a_failing_full_scan_does_not_postpone_the_check():
    creator = creator_policy(SimpleNamespace(settings=SimpleNamespace(full_scan_interval=24)))
    # A full scan stuck mid-way retries every few minutes (last_check_at).
    stuck = {"enabled": 1, "last_full_at": NOW - 5 * 86400,
             "last_incremental_at": NOW - 3600, "last_check_at": NOW - 60,
             "full_epoch": NOW - 86400, "full_seen": 300}
    assert incremental_due(stuck, creator, NOW)
    assert plan(stuck, creator, NOW) == "incremental"
    # A failed check itself still waits an interval before it runs again.
    tried = {**stuck, "incremental_try_at": NOW - 60}
    assert not incremental_due(tried, creator, NOW)
    assert plan(tried, creator, NOW) == "full"
    assert incremental_due({**tried, "incremental_try_at": NOW - creator.incremental_interval - 60},
                           creator, NOW)
    # A recent success is what decides, as before.
    assert not incremental_due({**stuck, "last_incremental_at": NOW - 60}, creator, NOW)


async def test_a_failed_check_records_its_attempt(db):
    client = FakeClient()
    source_id, _ = await db.write(sources.add, "creator", "SEC")
    client.pages["posts:SEC"] = [PaginationError("作品列表返回空页但仍有下一页")]
    policy = fast(creator_policy(make_context(db, client)))
    scanner = Scanner(make_context(db, client), None)
    outcome = await scanner.scan(await db.read(sources.get, source_id), "incremental", policy)
    assert not outcome.complete
    saved = await db.read(sources.get, source_id)
    assert saved["incremental_try_at"] >= NOW and saved["last_incremental_at"] == 0
    assert not incremental_due(saved, policy, int(time.time()))


async def test_incremental_stops_at_known_works(db):
    client = FakeClient()
    source_id, _ = await db.write(sources.add, "creator", "C", nickname="主播")
    await db.write(sources.record_listing, source_id, ["1", "2", "3"], seen_at=NOW - 100)
    await db.write(works.queue, ["1", "2", "3"], source_id)
    new = [video_payload(str(n), sec_uid="C") for n in (9, 8)]
    known = [video_payload(str(n), sec_uid="C") for n in (3, 2, 1)]
    foreign = video_payload("7", sec_uid="SOMEONE")
    client.pages["posts:C"] = [page_of(new + [foreign] + known, has_more=True, cursor=5),
                               page_of([video_payload("0", sec_uid="C")])]
    source = await db.read(sources.get, source_id)
    policy = fast(creator_policy(make_context(db, client)))
    outcome = await Scanner(make_context(db, client), None).scan(source, "incremental", policy)
    assert outcome.complete and outcome.pages == 1              # stopped at the known run
    assert sorted(outcome.queued) == ["8", "9"]                   # foreign work rejected
    assert await db.read(works.statuses, ["7"]) == {}


async def test_full_scan_resumes_from_saved_cursor(db):
    client = FakeClient()
    source_id, _ = await db.write(sources.add, "like", "ME", is_self=True)
    client.pages["likes:ME"] = [page_of([video_payload("1")], has_more=True, cursor=10),
                                HttpError(403, "/favorite/")]
    policy = fast(subscription_policy("like"))
    scanner = Scanner(make_context(db, client), None)
    outcome = await scanner.scan(await db.read(sources.get, source_id), "full", policy)
    assert outcome.paused and not outcome.complete
    saved = await db.read(sources.get, source_id)
    assert saved["full_cursor"] == 10 and saved["paused_until"] > NOW + 11 * 3600
    assert "风控" in saved["last_error"]
    client.pages["likes:ME"] = [page_of([video_payload("2")])]
    outcome = await scanner.scan(saved, "full", policy)
    assert outcome.complete and ("likes:ME", 10) in client.calls
    finished = await db.read(sources.get, source_id)
    assert finished["full_epoch"] == 0 and finished["cloud_count"] == 2


async def _unliked_scan(db, owner: bool):
    client = FakeClient()
    source_id, _ = await db.write(sources.add, "like", "ME", is_self=True)
    await db.write(sources.record_listing, source_id, ["old-liked", "unliked", "hidden"],
                   seen_at=NOW - 1000)
    client.pages["likes:ME"] = [page_of([video_payload("new")])]
    client.details["old-liked"] = {**video_payload("old-liked"), "user_digged": 1}
    client.details["unliked"] = {**video_payload("unliked"), "user_digged": 0}
    client.details["hidden"] = (None, "作品已删除", "inaccessible")
    deleted = []

    async def delete(source, ids):
        deleted.extend(ids)
        return len(ids)

    policy = fast(subscription_policy("like"))
    scanner = Scanner(make_context(db, client), delete)
    scanner.verify_wait = lambda: 0.0
    async def logged_in_as_owner():
        return owner

    outcome = await scanner.scan(
        await db.read(sources.get, source_id), "full", policy,
        viewer_is_owner=logged_in_as_owner)
    counts = (await db.read(sources.download_counts, ["like"]))[source_id]
    return outcome, deleted, counts


async def test_full_scan_confirms_unliked_works(db):
    outcome, deleted, counts = await _unliked_scan(db, owner=True)
    assert (outcome.removed, outcome.missing) == (1, 1)
    assert deleted == ["unliked"]
    assert counts["removed"] == 1 and counts["missing"] == 1


async def test_another_login_never_deletes_a_subscriptions_files(db):
    # The liked flag describes whoever is logged in, not the subscribed account.
    outcome, deleted, counts = await _unliked_scan(db, owner=False)
    assert (outcome.removed, outcome.missing) == (0, 0) and deleted == []
    assert counts["removed"] == 0


@pytest.mark.parametrize("mode", ["full", "incremental"])
async def test_a_long_download_backlog_never_holds_a_scan(db, mode):
    client = FakeClient()
    source_id, _ = await db.write(sources.add, "creator", "C")
    client.pages["posts:C"] = [page_of([video_payload("1", sec_uid="C")], has_more=True,
                                       cursor=5),
                               page_of([video_payload("2", sec_uid="C")])]
    policy = fast(creator_policy(make_context(db, client)))
    outcome = await Scanner(make_context(db, client), None).scan(
        await db.read(sources.get, source_id), mode, policy, backlog=lambda: 5000)
    assert outcome.complete and sorted(outcome.queued) == ["1", "2"]


def test_the_fast_strategy_pages_without_waiting():
    settings = SimpleNamespace(full_scan_interval=24, scan_fast=False)
    policy = creator_policy(SimpleNamespace(settings=settings))
    assert policy.page_wait(False, 0) >= 3.0 and policy.page_wait(True, 0) >= 4.0
    settings.scan_fast = True
    assert policy.page_wait(False, 0) == 0.0 and policy.page_wait(True, 500) == 0.0
