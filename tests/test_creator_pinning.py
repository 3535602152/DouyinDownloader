from __future__ import annotations

import json
import sqlite3

import pytest

from douyin import aweme
from services import views
from services.creators import _local_page
from storage import schema, sources, works
from tests.conftest import video_payload


@pytest.mark.parametrize("monitored", [True, False])
def test_homepage_pins_old_works_before_pagination(db, monitored):
    source_id = None
    if monitored:
        source_id, _ = db.write_sync(sources.add, "creator", "C")
    payloads = [video_payload(str(i), sec_uid="C", create_time=1000 + i)
                for i in range(80)]
    payloads[0]["is_top"] = 1
    payloads[1]["is_top"] = 1
    db.write_sync(works.save_works, payloads)
    if monitored:
        db.write_sync(sources.record_listing, source_id,
                      [item["aweme_id"] for item in payloads], seen_at=2000)

    first, more = db.read_sync(_local_page, "C", 0, 36)
    assert more and len(first) == 36
    assert [row["aweme_id"] for row in first[:2]] == ["1", "0"]
    assert all(views.local_work(row, row["files"], include_media=False)["is_pinned"]
               for row in first[:2])
    second, more = db.read_sync(_local_page, "C", 36, 36)
    third, last_more = db.read_sync(_local_page, "C", 72, 36)
    assert more and not last_more and len(third) == 8
    ids = [row["aweme_id"] for row in first + second + third]
    assert len(ids) == len(set(ids)) == 80
    assert ids == ["1", "0"] + [str(i) for i in range(79, 1, -1)]

    db.write_sync(works.set_visibility, "0", "hidden")
    hidden, more = db.read_sync(_local_page, "C", 0, 36, True)
    assert not more and [row["aweme_id"] for row in hidden] == ["0"]


@pytest.mark.parametrize("key", aweme.PIN_KEYS)
def test_pin_updates_survive_details_and_allow_explicit_unpin(db, key):
    payload = video_payload("pin")
    payload.pop("is_top")
    payload[key] = "true"
    assert aweme.slim(payload)["is_top"] == 1
    db.write_sync(works.save_works, [payload])
    detail = video_payload("pin", desc="updated details")
    detail.pop("is_top")
    db.write_sync(works.save_works, [detail])
    row = db.read_sync(works.get_work, "pin")
    assert row["is_pinned"] == 1
    assert views.local_work(row, [], include_media=False)["is_pinned"]

    payload[key] = "0"
    db.write_sync(works.save_works, [payload])
    row = db.read_sync(works.get_work, "pin")
    assert row["is_pinned"] == 0
    assert not views.local_work(row, [], include_media=False)["is_pinned"]


def test_remote_explicit_unpin_overrides_local_pin():
    local = {"is_pinned": True}
    remote = {"is_pinned": False}
    assert not views.merge_remote_local(remote, local)["is_pinned"]
    assert views.merge_remote_local(remote, local, pin_known=False)["is_pinned"]


def test_pin_migration_backfills_once_and_creates_indexes(tmp_path):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE works (aweme_id TEXT PRIMARY KEY, "
                           "author_sec_uid TEXT, create_time INTEGER, metadata TEXT)")
        connection.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO app_meta VALUES ('schema_version', '2')")
        entries = [(key, json.dumps({key: "true"})) for key in aweme.PIN_KEYS]
        entries += [("ordinary", '{}'), ("false", '{"is_top":"0"}'), ("bad", 'invalid')]
        connection.executemany("INSERT INTO works VALUES (?, 'C', 1, ?)", entries)
        connection.commit()
        schema.migrate(connection, tmp_path / "test.db")
        pins = dict(connection.execute("SELECT aweme_id, is_pinned FROM works"))
        assert all(pins[key] == 1 for key in aweme.PIN_KEYS)
        assert all(pins[key] == 0 for key in ("ordinary", "false", "bad"))
        assert schema.meta(connection, "schema_version") == str(schema.SCHEMA_VERSION)
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(works)")}
        assert {"works_home_order", "works_author_home_order"} <= indexes
        # Stale metadata must not reset an explicit unpin on the next startup.
        connection.execute("UPDATE works SET is_pinned = 0")
        connection.commit()
        schema.migrate(connection, tmp_path / "test.db")
        assert not any(row[0] for row in connection.execute("SELECT is_pinned FROM works"))
    finally:
        connection.close()
