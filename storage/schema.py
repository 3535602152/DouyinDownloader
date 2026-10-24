"""Tables of the application database.

The model is deliberately small:

``works``         one row per Douyin work ever seen (slim descriptive data)
``downloads``     the download ledger: at most one row per work, shared by
                  every feature, so a work is fetched once no matter how many
                  sources list it
``work_files``    every local file belonging to a work
``sources``       things the app follows: monitored creators (``creator``)
                  and account like / collect subscriptions (``like`` /
                  ``collect``), with one shared scan-state model
``source_works``  which source lists which work (membership)
``scan_batches``  manual 全量扫描 batches shown in the footer
``comments`` …    saved comments, their images and sync state
``risk_events``   anti-bot and API diagnostics (风控统计)
``creator_profiles`` the last online homepage header of each creator, with
                  local copies of its avatar and cover (offline homepages)

Tables of older app versions are left untouched; their data is imported
once by :mod:`storage.legacy`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from common.log import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 3

_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS works (
        aweme_id TEXT PRIMARY KEY,
        author_sec_uid TEXT NOT NULL DEFAULT '',
        author_uid TEXT NOT NULL DEFAULT '',
        author_name TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        create_time INTEGER NOT NULL DEFAULT 0,
        media_type TEXT NOT NULL DEFAULT '',
        is_pinned INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}',
        visibility TEXT NOT NULL DEFAULT 'unknown',
        visibility_note TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS works_author ON works (author_sec_uid, create_time)",
    """
    CREATE TABLE IF NOT EXISTS downloads (
        aweme_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',
        origin INTEGER NOT NULL DEFAULT 0,
        context TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        extras_missing TEXT NOT NULL DEFAULT '',
        extras_attempts INTEGER NOT NULL DEFAULT 0,
        extras_retry_at INTEGER NOT NULL DEFAULT 0,
        queued_at INTEGER NOT NULL DEFAULT 0,
        downloaded_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS downloads_ready ON downloads (status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS downloads_done ON downloads (downloaded_at)",
    """
    CREATE TABLE IF NOT EXISTS work_files (
        aweme_id TEXT NOT NULL,
        path TEXT NOT NULL,
        kind TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (aweme_id, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS work_files_path ON work_files (path)",
    """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        sec_uid TEXT NOT NULL,
        uid TEXT NOT NULL DEFAULT '',
        nickname TEXT NOT NULL DEFAULT '',
        avatar_url TEXT NOT NULL DEFAULT '',
        is_self INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        added_at INTEGER NOT NULL DEFAULT 0,
        last_check_at INTEGER NOT NULL DEFAULT 0,
        last_success_at INTEGER NOT NULL DEFAULT 0,
        last_incremental_at INTEGER NOT NULL DEFAULT 0,
        incremental_try_at INTEGER NOT NULL DEFAULT 0,
        last_full_at INTEGER NOT NULL DEFAULT 0,
        full_epoch INTEGER NOT NULL DEFAULT 0,
        full_cursor INTEGER NOT NULL DEFAULT 0,
        full_pages INTEGER NOT NULL DEFAULT 0,
        full_seen INTEGER NOT NULL DEFAULT 0,
        paused_until INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        last_note TEXT NOT NULL DEFAULT '',
        cloud_count INTEGER NOT NULL DEFAULT 0,
        latest_aweme_id TEXT NOT NULL DEFAULT '',
        latest_aweme_title TEXT NOT NULL DEFAULT '',
        latest_aweme_time INTEGER NOT NULL DEFAULT 0,
        comments_enabled INTEGER NOT NULL DEFAULT 0,
        UNIQUE (kind, sec_uid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_works (
        source_id INTEGER NOT NULL,
        aweme_id TEXT NOT NULL,
        first_seen_at INTEGER NOT NULL DEFAULT 0,
        last_seen_at INTEGER NOT NULL DEFAULT 0,
        seen_epoch INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'listed',
        note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source_id, aweme_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS source_works_aweme ON source_works (aweme_id)",
    """
    CREATE TABLE IF NOT EXISTS scan_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'scanning',
        error TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_batch_sources (
        batch_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        scanned INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (batch_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_batch_works (
        batch_id INTEGER NOT NULL,
        aweme_id TEXT NOT NULL,
        PRIMARY KEY (batch_id, aweme_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        aweme_id TEXT NOT NULL,
        cid TEXT NOT NULL,
        parent_cid TEXT NOT NULL DEFAULT '',
        is_reply INTEGER NOT NULL DEFAULT 0,
        nickname TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        digg_count INTEGER NOT NULL DEFAULT 0,
        create_time INTEGER NOT NULL DEFAULT 0,
        ip_label TEXT NOT NULL DEFAULT '',
        image_count INTEGER NOT NULL DEFAULT 0,
        live_image_count INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        raw TEXT NOT NULL DEFAULT '',
        fetched_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (aweme_id, cid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_images (
        aweme_id TEXT NOT NULL,
        cid TEXT NOT NULL,
        idx INTEGER NOT NULL,
        urls TEXT NOT NULL DEFAULT '[]',
        preview_url TEXT NOT NULL DEFAULT '',
        live_urls TEXT NOT NULL DEFAULT '[]',
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        thumb_path TEXT NOT NULL DEFAULT '',
        local_path TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (aweme_id, cid, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_sync (
        aweme_id TEXT PRIMARY KEY,
        complete INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_at INTEGER NOT NULL DEFAULT 0,
        last_success_at INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'unknown',
        synced_count INTEGER NOT NULL DEFAULT -1,
        full_cursor INTEGER NOT NULL DEFAULT 0,
        full_done INTEGER NOT NULL DEFAULT 0,
        full_count INTEGER NOT NULL DEFAULT -1,
        full_at INTEGER NOT NULL DEFAULT 0,
        full_fails INTEGER NOT NULL DEFAULT 0,
        full_failed_at INTEGER NOT NULL DEFAULT 0,
        full_error TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER NOT NULL,
        type TEXT NOT NULL,
        category TEXT NOT NULL,
        path TEXT NOT NULL DEFAULT '',
        http_status INTEGER,
        api_status INTEGER,
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS risk_events_time ON risk_events (timestamp)",
    """
    CREATE TABLE IF NOT EXISTS creator_profiles (
        sec_uid TEXT PRIMARY KEY,
        payload TEXT NOT NULL DEFAULT '{}',
        avatar_key TEXT NOT NULL DEFAULT '',
        avatar_file TEXT NOT NULL DEFAULT '',
        cover_key TEXT NOT NULL DEFAULT '',
        cover_file TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
)


def meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_meta(connection: sqlite3.Connection, key: str, value) -> None:
    connection.execute("INSERT INTO app_meta (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def migrate(connection: sqlite3.Connection, db_path: Path) -> None:
    """Create the tables, import legacy data once, and recover crashed claims."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _TABLES:
            connection.execute(statement)
        version = int(meta(connection, "schema_version", "0") or 0)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
        if "incremental_try_at" not in columns:
            connection.execute("ALTER TABLE sources ADD COLUMN "
                               "incremental_try_at INTEGER NOT NULL DEFAULT 0")
        if "comments_enabled" not in columns:
            connection.execute("ALTER TABLE sources ADD COLUMN "
                               "comments_enabled INTEGER NOT NULL DEFAULT 0")
        sync_columns = {row[1] for row in connection.execute("PRAGMA table_info(comment_sync)")}
        if "synced_count" not in sync_columns:
            connection.execute("ALTER TABLE comment_sync ADD COLUMN "
                               "synced_count INTEGER NOT NULL DEFAULT -1")
        for column, kind in (("full_cursor", "INTEGER NOT NULL DEFAULT 0"),
                             ("full_done", "INTEGER NOT NULL DEFAULT 0"),
                             ("full_count", "INTEGER NOT NULL DEFAULT -1"),
                             ("full_at", "INTEGER NOT NULL DEFAULT 0"),
                             ("full_fails", "INTEGER NOT NULL DEFAULT 0"),
                             ("full_failed_at", "INTEGER NOT NULL DEFAULT 0"),
                             ("full_error", "TEXT NOT NULL DEFAULT ''")):
            if column not in sync_columns:
                connection.execute(f"ALTER TABLE comment_sync ADD COLUMN {column} {kind}")
        if version < 1:
            from storage import legacy

            imported = legacy.import_all(connection, db_path)
            set_meta(connection, "legacy_import", imported)
        work_columns = {row[1] for row in connection.execute("PRAGMA table_info(works)")}
        if "is_pinned" not in work_columns:
            connection.execute("ALTER TABLE works ADD COLUMN "
                               "is_pinned INTEGER NOT NULL DEFAULT 0")
        if version < 3:
            from douyin import aweme

            # One-time backfill, including freshly imported legacy works.
            for work_id, metadata in connection.execute("SELECT aweme_id, metadata FROM works"):
                connection.execute("UPDATE works SET is_pinned = ? WHERE aweme_id = ?",
                                   (int(aweme.is_pinned(aweme.decode(metadata))), work_id))
        connection.execute("CREATE INDEX IF NOT EXISTS works_home_order ON works "
                           "(is_pinned DESC, create_time DESC, aweme_id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS works_author_home_order ON works "
                           "(author_sec_uid, is_pinned DESC, create_time DESC, aweme_id DESC)")
        set_meta(connection, "schema_version", SCHEMA_VERSION)
        # A crash cannot leave work claimed: whatever was running is pending.
        connection.execute("UPDATE downloads SET status = 'pending' WHERE status = 'active'")
        connection.execute("UPDATE comment_sync SET status = 'pending' WHERE status = 'running'")
        connection.execute("UPDATE scan_batches SET status = 'interrupted', "
                           "error = '程序已关闭，扫描未完成', revision = revision + 1 "
                           "WHERE status = 'scanning'")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
