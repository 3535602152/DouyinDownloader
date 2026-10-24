from __future__ import annotations

import json
import sqlite3
import time

import pytest

from storage import comments, diagnostics, folders, scans, sources, works
from storage.db import Database
from storage.files import DownloadContext, Layout, classify_file, render_template, validate_template
from tests.conftest import video_payload

NOW = int(time.time())


# ── layout ──────────────────────────────────────────────────────────


def test_layout_matches_existing_folders(tmp_path):
    layout = Layout(tmp_path)
    common = dict(aweme_id="7", title="标题", author="作者", author_sec_uid="SEC",
                  create_time=1700000000, media_type="video")
    folder, stem = layout.work_target(DownloadContext("following", "主播", "CRE"), **common)
    assert folder.relative_to(tmp_path).parts[:3] == ("关注主播", "主播", "主页作品")
    assert stem == "2023-11-15_标题_7" or stem.endswith("_标题_7")
    folder, _ = layout.work_target(DownloadContext("subscription", "账号", "ACC", "like"), **common)
    assert folder.relative_to(tmp_path).parts[:3] == ("喜欢", "账号", "作者")
    folder, _ = layout.work_target(DownloadContext("single_aweme"), **common)
    assert folder.relative_to(tmp_path).parts[:3] == ("单独下载", "单个作品", "作者")
    folder, _ = layout.work_target(DownloadContext("self", mode="collect",
                                                   collection_name="默认收藏"), **common)
    assert folder.relative_to(tmp_path).parts[:4] == ("我的内容", "收藏作品", "默认收藏", "作者")


def test_templates():
    validate_template("{date}_{id}")
    with pytest.raises(ValueError):
        validate_template("{date}")
    with pytest.raises(ValueError):
        validate_template("{nope}_{id}")
    long = render_template("{title}_{id}", {"title": "长" * 200, "id": "1234567890"})
    assert long.endswith("_1234567890") and len(long) <= 80


@pytest.mark.parametrize("name, expected", [
    ("a_data.json", ("json", 0)), ("a_comments.json", ("comments", 0)),
    ("a_cover.jpg", ("cover", 0)), ("a_music.mp3", ("music", 0)),
    ("a_3.webp", ("media", 3)), ("a_live_2.mp4", ("live", 2)), ("a_live_2.jpg", ("motion", 2)),
    ("a.mp4", ("media", 0)), ("readme.txt", ("", 0)),
])
def test_classify_file(name, expected):
    assert classify_file(name) == expected


def test_creator_folder_follows_renames(tmp_path):
    root = tmp_path / "关注主播"
    first = folders.resolve_folder(root, "旧名字", "SEC")
    (first / "work.mp4").write_bytes(b"x")
    assert folders.resolve_folder(root, "新名字", "SEC") == first   # never renames while in use
    result = folders.reconcile_folder(root, "SEC", "新名字")
    assert result.changed and result.folder.name == "新名字"
    assert (root / "新名字" / "work.mp4").exists() and not first.exists()
    other = folders.resolve_folder(root, "新名字", "OTHER", uid="99")
    assert other.name == "新名字 (99)"


# ── ledger ──────────────────────────────────────────────────────────


def test_ledger_lifecycle(db):
    source_id, created = db.write_sync(sources.add, "creator", "SEC_A", nickname="A")
    assert created
    payload = video_payload("1", sec_uid="SEC_A")
    db.write_sync(works.save_works, [payload])
    db.write_sync(sources.record_listing, source_id, ["1"], seen_at=NOW)
    assert db.write_sync(works.queue, ["1"], source_id) == ["1"]
    assert db.write_sync(works.queue, ["1"], source_id) == []            # queued once
    assert db.read_sync(works.ready_sources, ["creator"], NOW) == [source_id]
    assert db.write_sync(works.claim, "1", source_id, {"source_type": "following"})
    assert not db.write_sync(works.claim, "1", source_id, {})            # already active
    db.write_sync(works.record_failure, "1", error="x", permanent=False, attempts=1,
                  retry_at=NOW + 60)
    assert db.read_sync(works.ready_sources, ["creator"], NOW) == []     # not due yet
    db.write_sync(works.record_success, "1", files=[{"path": "C:/a.mp4", "kind": "media"}],
                  extras_missing=["cover"], origin=source_id)
    counts = db.read_sync(sources.download_counts, ["creator"])[source_id]
    assert counts["done"] == 1 and counts["day"] == 1
    extra = db.read_sync(works.next_extras, NOW + 3600, set())
    assert extra and extra["extras_missing"] == "cover"
    history = db.read_sync(works.history, page=1, size=10)
    assert history["total"] == 1 and history["items"][0]["file_path"] == "C:/a.mp4"


def test_subscriptions_follow_creators_in_the_queue(db):
    creator, _ = db.write_sync(sources.add, "creator", "C", nickname="C")
    liked, _ = db.write_sync(sources.add, "like", "ME", nickname="me", is_self=True)
    db.write_sync(works.save_works, [video_payload("1", sec_uid="C"), video_payload("2")])
    db.write_sync(sources.record_listing, creator, ["1"], seen_at=NOW)
    db.write_sync(sources.record_listing, liked, ["2", "1"], seen_at=NOW)
    db.write_sync(works.queue, ["1"], creator)
    db.write_sync(works.queue, ["2", "1"], liked)
    # "1" is shared: one ledger row, listed by both sources.
    assert db.read_sync(works.statuses, ["1", "2"]) == {"1": "pending", "2": "pending"}
    assert db.read_sync(sources.other_listings, ["1", "2"], [liked]) == {"1"}


def test_membership_and_missing_works(db):
    source_id, _ = db.write_sync(sources.add, "like", "ME")
    db.write_sync(sources.record_listing, source_id, ["1", "2"], seen_at=NOW - 10, epoch=0)
    db.write_sync(sources.record_listing, source_id, ["1"], seen_at=NOW, epoch=NOW)
    assert [row["aweme_id"] for row in db.read_sync(sources.unseen_since, source_id, NOW)] == ["2"]
    db.write_sync(sources.mark_gone, source_id, "2", "removed", "已取消喜欢")
    counts = db.read_sync(sources.download_counts, ["like"])[source_id]
    assert counts["removed"] == 1
    db.write_sync(sources.record_listing, source_id, ["2"], seen_at=NOW + 5)
    assert db.read_sync(sources.download_counts, ["like"])[source_id]["removed"] == 0


def test_creator_stats_for_optional_columns(db):
    source_id, _ = db.write_sync(sources.add, "creator", "C", nickname="C")
    old = video_payload("1", sec_uid="C", create_time=NOW - 10 * 86400)
    old["statistics"] = {"digg_count": 30}
    old["author"]["unique_id"] = "creator_c"
    fresh = video_payload("2", sec_uid="C", create_time=NOW - 3600, desc="热门")
    fresh["statistics"] = {"digg_count": 90}
    week = video_payload("3", sec_uid="C", create_time=NOW - 3 * 86400)
    week.pop("video")
    week["images"] = [{"url_list": ["https://p3.example/a.jpg"]}]
    week["statistics"] = {"digg_count": 0}
    db.write_sync(works.save_works, [old, fresh, week])
    db.write_sync(sources.record_listing, source_id, ["1", "2", "3"], seen_at=NOW)
    db.write_sync(sources.mark_gone, source_id, "1", "missing", "主页已不可见")
    for aweme_id in ("1", "2"):
        db.write_sync(works.queue, [aweme_id], source_id)
        db.write_sync(works.record_success, aweme_id,
                      files=[{"path": f"C:/{aweme_id}.mp4", "kind": "media"}], origin=source_id)
    db.write_sync(lambda c: c.execute("INSERT INTO comment_sync (aweme_id, complete) VALUES ('2', 1)"))
    stats = db.read_sync(sources.creator_stats, ["creator"], NOW)[source_id]
    assert stats["hidden"] == 1
    assert (stats["new_1d"], stats["new_7d"]) == (1, 2)
    assert stats["latest_time"] == NOW - 3600
    assert (stats["videos"], stats["galleries"]) == (2, 1)
    assert stats["duration_ms"] == 24000
    assert (stats["done"], stats["comments_saved"]) == (2, 1)
    assert (stats["top_digg"], stats["top_digg_title"], stats["avg_digg"]) == (90, "热门", 40)
    assert stats["douyin_id"] == "creator_c"


def test_scan_batch_snapshot(db):
    source_id, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    batch, added = db.write_sync(scans.begin, [source_id])
    assert added == [source_id]
    assert db.write_sync(scans.begin, [source_id]) == (batch, [])       # already queued
    db.write_sync(scans.start_source, batch, source_id)
    db.write_sync(scans.progress, batch, source_id, 40)
    db.write_sync(works.queue, ["1", "2"], source_id)
    db.write_sync(scans.add_works, batch, ["1", "2"])
    snapshot = db.read_sync(scans.snapshot)
    assert snapshot["status"] == "scanning" and snapshot["current_scanned"] == 40
    assert snapshot["current_creator_name"] == "主播"
    db.write_sync(scans.finish_source, batch, source_id)
    db.write_sync(works.record_success, "1", files=[])
    snapshot = db.read_sync(scans.snapshot)
    assert snapshot["status"] == "downloading"
    assert (snapshot["download_done"], snapshot["download_total"]) == (1, 2)


def test_stale_scan_batch_does_not_block_new_scans(db):
    source_id, _ = db.write_sync(sources.add, "creator", "C")
    batch, _ = db.write_sync(scans.begin, [source_id])
    db.write_sync(scans.start_source, batch, source_id)
    # The app closed mid-scan: without clean-up the source stays "scanning".
    db.write_sync(scans.interrupt_stale, "应用已关闭")
    fresh, added = db.write_sync(scans.begin, [source_id])
    assert fresh != batch and added == [source_id]


def test_dropping_the_backlog_keeps_finished_and_running_works(db):
    source_id, _ = db.write_sync(sources.add, "creator", "C")
    db.write_sync(works.queue, ["wait", "again", "busy", "done", "dead"], source_id)
    db.write_sync(works.record_failure, "again", error="x", permanent=False, attempts=1,
                  retry_at=NOW + 60)
    db.write_sync(works.record_failure, "dead", error="x", permanent=True, attempts=6,
                  retry_at=0)
    db.write_sync(works.claim, "busy", source_id, {})
    db.write_sync(works.record_success, "done", files=[])
    db.write_sync(sources.update, source_id, full_epoch=NOW, full_cursor=40, full_seen=18)
    assert db.write_sync(works.drop_backlog) == 2
    assert db.read_sync(works.statuses, ["wait", "again", "busy", "done", "dead"]) == {
        "busy": "active", "done": "done", "dead": "failed"}
    source = db.read_sync(sources.get, source_id)
    assert (source["full_epoch"], source["full_cursor"]) == (0, 0)   # restarts from page 1


def test_comments_and_risk_events(db):
    db.write_sync(comments.save, "9", [{"cid": "c1", "nickname": "n", "text": "hi"}],
                  [{"comment_id": "c1", "index": 0, "urls": ["u"], "preview_url": "p"}])
    loaded = db.read_sync(comments.load, "9")
    assert loaded["comments"][0]["text"] == "hi" and loaded["images"][0]["urls"] == ["u"]
    assert db.write_sync(comments.claim_sync, "9", force=True)
    assert not db.write_sync(comments.claim_sync, "9", force=True)       # running elsewhere
    db.write_sync(diagnostics.record, {"type": "http_429", "category": "risk",
                                       "path": "/a?token=x", "reason": "cookie=secret"})
    report = db.read_sync(diagnostics.report)
    assert report["risk_total"] == 1
    assert report["recent_events"][0]["path"] == "/a"
    assert report["recent_events"][0]["reason"] == "[sensitive details omitted]"


def test_comment_sync_queue_follows_the_switch_and_comment_counts(db):
    due = {"refresh_after": 0, "retry_after": 3600}

    def due_ids():
        return db.read_sync(comments.due_count, **due)

    source_id, _ = db.write_sync(sources.add, "creator", "C", nickname="主播")
    db.write_sync(sources.record_listing, source_id, ["a", "b", "c"], seen_at=1)
    db.write_sync(works.save_works, [{**video_payload("a"), "statistics": {"comment_count": 3}}])
    db.write_sync(works.record_success, "a", files=[
        {"path": "a.mp4", "kind": "media"}, {"path": "a_data.json", "kind": "json"}])
    db.write_sync(works.record_success, "b", files=[{"path": "b.mp4", "kind": "media"}])
    assert due_ids() == 0                                   # the switch is off
    assert db.write_sync(sources.set_comments, [source_id], True) == 1
    assert due_ids() == 2                                   # "c" is not downloaded
    first = db.read_sync(comments.next_due, **due)
    assert (first["aweme_id"], first["path"], first["comment_count"]) == ("a", "a_data.json", 3)
    assert first["nickname"] == "主播"
    # Read once: not due again until a scan sees another comment count.
    assert db.write_sync(comments.claim_sync, "a", force=True)
    assert due_ids() == 1                                   # running
    db.write_sync(comments.finish_sync, "a", success=True)
    assert due_ids() == 1
    db.write_sync(works.save_works, [{**video_payload("a"), "statistics": {"comment_count": 5}}])
    assert due_ids() == 2
    # A failure waits; an interrupted read is due at once.
    db.write_sync(comments.claim_sync, "b", force=True)
    db.write_sync(comments.finish_sync, "b", success=False, error="x")
    assert db.read_sync(comments.next_due, **due)["aweme_id"] == "a"
    db.write_sync(comments.claim_sync, "a", force=True)
    db.write_sync(comments.release_sync, "a")
    assert db.read_sync(comments.next_due, **due)["aweme_id"] == "a"
    db.write_sync(comments.set_pause, 123, 2)
    assert db.read_sync(comments.pause_state) == {"until": 123, "level": 2}


def test_comment_sync_reads_the_newest_works_first(db):
    source_id, _ = db.write_sync(sources.add, "creator", "C")
    db.write_sync(sources.set_comments, [source_id], True)
    db.write_sync(sources.record_listing, source_id, ["old", "new", "mid"], seen_at=1)
    for aweme_id, stamp in (("old", 1600000000), ("new", 1800000000), ("mid", 1700000000)):
        db.write_sync(works.save_works, [video_payload(aweme_id, create_time=stamp)])
        db.write_sync(works.record_success, aweme_id, files=[{"path": f"{aweme_id}.mp4"}])
    listed = db.read_sync(comments.due_list, refresh_after=0, retry_after=0, limit=5)
    assert [row["aweme_id"] for row in listed] == ["new", "mid", "old"]


def test_comment_second_pass_queue(db):
    full = {"refresh_after": 0, "retry_after": 3600, "grow_min": 5}

    def queued():
        return [row["aweme_id"] for row in db.read_sync(comments.full_list, **full, limit=9)]

    def count(aweme_id, value):
        db.write_sync(works.save_works, [{**video_payload(aweme_id),
                                          "statistics": {"comment_count": value}}])

    source_id, _ = db.write_sync(sources.add, "creator", "C")
    db.write_sync(sources.set_comments, [source_id], True)
    db.write_sync(sources.record_listing, source_id, ["big", "small", "none"], seen_at=1)
    for aweme_id, value in (("big", 900), ("small", 60), ("none", 0)):
        count(aweme_id, value)
        db.write_sync(works.record_success, aweme_id, files=[{"path": f"{aweme_id}.mp4"}])
    assert queued() == []                                   # their first pass comes first
    for aweme_id in ("big", "small", "none"):
        db.write_sync(comments.claim_sync, aweme_id, force=True)
        db.write_sync(comments.finish_sync, aweme_id, success=True)
    assert queued() == ["small", "big"]                     # fewest first; none without any
    assert db.read_sync(comments.full_count, **full) == 2
    # Resumes at the saved page; finished, it waits for the count to grow.
    assert db.write_sync(comments.claim_full, "small") == 0
    assert queued() == ["big"]                              # running
    db.write_sync(comments.full_progress, "small", 40)
    db.write_sync(comments.finish_full, "small", done=False)
    assert db.write_sync(comments.claim_full, "small") == 40
    db.write_sync(comments.finish_full, "small", done=True)
    assert queued() == ["big"]
    count("small", 64)
    assert queued() == ["big"]                              # +4 is not enough
    count("small", 65)
    assert queued() == ["small", "big"]
    assert db.write_sync(comments.claim_full, "small") == 0     # read again from the top
    db.write_sync(comments.finish_full, "small", done=True)
    # A page that keeps failing: retried later, given up after the third time.
    for _ in range(2):
        db.write_sync(comments.claim_full, "big")
        db.write_sync(comments.finish_full, "big", done=False, error="x")
        assert queued() == []                               # waits retry_after
        db.write_sync(lambda c: c.execute("UPDATE comment_sync SET full_failed_at = 0"))
        assert queued() == ["big"]
    db.write_sync(comments.claim_full, "big")
    db.write_sync(comments.finish_full, "big", done=False, error="x")
    db.write_sync(lambda c: c.execute("UPDATE comment_sync SET full_failed_at = 0"))
    assert queued() == []


# ── legacy import ───────────────────────────────────────────────────


def _legacy_database(path, download_dir):
    video = download_dir / "关注主播" / "旧" / "1_x.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE aweme (id INTEGER PRIMARY KEY, aweme_id TEXT UNIQUE, aweme_type TEXT,
            title TEXT, author_id TEXT, author_name TEXT, author_sec_uid TEXT,
            create_time INTEGER, download_time INTEGER, file_path TEXT, metadata TEXT);
        CREATE TABLE aweme_asset (aweme_id TEXT, author_sec_uid TEXT, file_path TEXT,
            asset_type TEXT, file_size INTEGER);
        CREATE TABLE watch_creator (sec_uid TEXT PRIMARY KEY, nickname TEXT, avatar_url TEXT,
            homepage_url TEXT, enabled INTEGER, added_at INTEGER, baseline_time INTEGER,
            last_check_at INTEGER, last_success_at INTEGER, last_new_at INTEGER,
            last_check_note TEXT, last_error TEXT, failure_count INTEGER,
            public_aweme_count INTEGER, last_full_scan_at INTEGER);
        CREATE TABLE watch_aweme (sec_uid TEXT, aweme_id TEXT, create_time INTEGER,
            discovered_at INTEGER, status TEXT, downloaded INTEGER, downloaded_at INTEGER,
            metadata TEXT, visibility_status TEXT, download_status TEXT, last_error_code TEXT,
            last_error_message TEXT, retry_count INTEGER, next_retry_at INTEGER,
            last_seen_at INTEGER, failure_reason TEXT);
        CREATE TABLE risk_event (id INTEGER PRIMARY KEY, timestamp INTEGER, type TEXT,
            category TEXT, path TEXT, http_status INTEGER, api_status INTEGER, reason TEXT);
    """)
    meta = json.dumps(video_payload("1", sec_uid="SEC"))
    legacy.execute("INSERT INTO aweme VALUES (1, '1', 'video', 't', '9', '旧', 'SEC', 1700000000,"
                   " 1700000500, ?, ?)", (str(video), meta))
    legacy.execute("INSERT INTO aweme_asset VALUES ('1', 'SEC', ?, 'cover', 1)",
                   (str(video.with_name("1_x_cover.jpg")),))
    legacy.execute("INSERT INTO watch_creator VALUES ('SEC', '主播', '', '', 1, 1, 0, 5, 5, 0, "
                   "'正常', '', 0, 12, 1700000000)")
    rows = [
        ("SEC", "1", 1700000000, 1, "downloaded", 1, 1700000500, meta, "visible", "success",
         "", "", 0, 0, 1, ""),
        ("SEC", "2", 1700000100, 1, "pending", 0, None,
         json.dumps(video_payload("2", sec_uid="SEC")), "visible", "retryable_failed", "",
         "超时", 2, 99, 1, ""),
        ("SEC", "3", 1700000200, 1, "invisible", 0, None, "", "deleted", "permanent_failed",
         "AWEME_DELETED", "作品已删除", 1, 0, 1, ""),
        ("SEC", "4", 1700000300, 1, "pending", 0, None,
         json.dumps(video_payload("4", sec_uid="SOMEONE_ELSE")), "visible", "pending", "", "",
         0, 0, 1, ""),
    ]
    legacy.executemany("INSERT INTO watch_aweme VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                       " ?, ?)", rows)
    legacy.execute("INSERT INTO risk_event VALUES (1, 1700000000, 'http_403', 'risk', '/x', 403,"
                   " NULL, 'r')")
    legacy.commit()
    legacy.close()
    subs = sqlite3.connect(path.with_name(f"{path.stem}.subscriptions.db"))
    subs.executescript("""
        CREATE TABLE sub_source (id INTEGER PRIMARY KEY, account_sec_uid TEXT, kind TEXT,
            nickname TEXT, avatar_url TEXT, is_self INTEGER, enabled INTEGER, added_at INTEGER,
            cloud_count INTEGER, last_incremental_at INTEGER, last_full_at INTEGER,
            full_epoch INTEGER, full_cursor INTEGER, full_pages INTEGER, full_seen INTEGER,
            paused_until INTEGER, last_error TEXT, last_note TEXT);
        CREATE TABLE sub_work (source_id INTEGER, aweme_id TEXT, author_sec_uid TEXT,
            author_name TEXT, title TEXT, create_time INTEGER, first_seen_at INTEGER,
            last_seen_epoch INTEGER, status TEXT, retry_count INTEGER, next_retry_at INTEGER,
            error TEXT, files TEXT, metadata TEXT, updated_at INTEGER);
        INSERT INTO sub_source VALUES (1, 'ME', 'like', '我', '', 1, 1, 1, 30, 0, 0, 5, 100, 3,
            60, 0, '', '');
        INSERT INTO sub_work VALUES (1, '1', 'SEC', '主播', 't', 0, 1, 0, 'local', 0, 0, '',
            '[]', '{}', 0);
        INSERT INTO sub_work VALUES (1, '5', 'X', 'x', 't5', 0, 1, 0, 'removed', 0, 0, '已取消',
            '[]', '{}', 0);
    """)
    subs.commit()
    subs.close()


def test_legacy_import(tmp_path):
    path = tmp_path / "DouyinDownloader.db"
    _legacy_database(path, tmp_path / "dl")
    db = Database(path)
    try:
        statuses = db.read_sync(works.statuses, ["1", "2", "3", "4"])
        assert statuses == {"1": "done", "2": "retry", "3": "failed"}   # 4 was a foreign work
        files = db.read_sync(works.files_of, ["1"])["1"]
        assert {entry["kind"] for entry in files} == {"media", "cover"}
        creators = db.read_sync(sources.all_of, ["creator"])
        assert creators[0]["nickname"] == "主播" and creators[0]["last_full_at"] == 1700000000
        subscription = db.read_sync(sources.all_of, ["like"])[0]
        assert subscription["full_cursor"] == 100 and subscription["is_self"] == 1
        counts = db.read_sync(sources.download_counts, ["like"])[subscription["id"]]
        assert counts["done"] == 1 and counts["removed"] == 1
        hidden = db.read_sync(works.get_work, "3")
        assert hidden["visibility"] == "hidden"
        assert db.read_sync(diagnostics.report)["risk_total"] == 1
    finally:
        db.close()
    # A second start does not import again.
    again = Database(path)
    try:
        assert again.read_sync(diagnostics.report)["risk_total"] == 1
    finally:
        again.close()


def test_the_account_library_starts_from_the_old_login(tmp_path):
    from storage.settings import Settings

    (tmp_path / ".cookies.json").write_text(json.dumps({"sessionid": "old"}), encoding="utf-8")
    settings = Settings(tmp_path)
    assert settings.cookies == {"sessionid": "old"} and settings.main_account == ""
    settings.name_pending_account("ME", nickname="我")
    settings.save_account("B", {"sessionid": "b"}, nickname="乙")
    assert settings.main_account == "ME"
    reloaded = Settings(tmp_path)
    assert [entry["sec_uid"] for entry in reloaded.accounts()] == ["ME", "B"]
    assert reloaded.cookies == {"sessionid": "old"}
    assert reloaded.set_main_account("B") and reloaded.cookies == {"sessionid": "b"}
    assert json.loads((tmp_path / ".cookies.json").read_text(encoding="utf-8")) == \
        {"sessionid": "b"}
    reloaded.save_account("ME", expired=True)
    assert reloaded.account("ME")["expired"]
    reloaded.save_account("ME", {"sessionid": "new"})
    assert not reloaded.account("ME")["expired"]
    assert reloaded.remove_account("B") and reloaded.main_account == "ME"
    reloaded.set_cookies({})
    assert reloaded.accounts() == [] and not (tmp_path / ".cookies.json").exists()


def test_a_refetched_picture_replaces_the_record_of_the_old_one(db):
    old = {"path": "D:/w/2_1.webp", "kind": "media", "position": 1}
    db.write_sync(works.record_success, "2", files=[old, {"path": "D:/w/2_2.jpg",
                                                          "kind": "media", "position": 2}])
    db.write_sync(works.record_extras, "2", missing=[], attempts=1,
                  files=[{"path": "D:/w/2_1.jpg", "kind": "media", "position": 1}])
    paths = sorted(entry["path"] for entry in db.read_sync(works.files_of, ["2"])["2"])
    assert paths == ["D:/w/2_1.jpg", "D:/w/2_2.jpg"]
