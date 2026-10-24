"""One-time import of the data kept by older app versions.

Older versions spread the same facts over several tables (and a second
database file for subscriptions): the download archive ``aweme`` +
``aweme_asset``, the creator monitor's ``watch_creator`` + ``watch_aweme``,
the subscription store's ``sub_source`` + ``sub_work`` and the comment
tables.  They are read once, in the migration transaction, and folded into
the new model.  Nothing old is modified or dropped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from common.log import get_logger
from douyin import aweme as aweme_tools
from storage.files import classify_file

logger = get_logger(__name__)

_DONE, _PENDING, _RETRY, _FAILED = "done", "pending", "retry", "failed"


def _tables(connection: sqlite3.Connection, schema: str = "main") -> Set[str]:
    return {str(row[0]) for row in connection.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")}


def _columns(connection: sqlite3.Connection, table: str, schema: str = "main") -> Set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA {schema}.table_info({table})")}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _slim(text: Any) -> Dict[str, Any]:
    payload = aweme_tools.decode(text)
    return aweme_tools.slim(payload) if payload else {}


class _Importer:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.now = int(time.time())
        self.counts: Dict[str, int] = {}

    def count(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def work(self, aweme_id: str, *, payload: Optional[Dict[str, Any]] = None,
             author_sec_uid: str = "", author_name: str = "", title: str = "",
             create_time: int = 0, media_type: str = "", visibility: str = "unknown",
             note: str = "") -> None:
        payload = payload or {}
        owner = payload.get("author") if isinstance(payload.get("author"), dict) else {}
        if not media_type:
            media_type = aweme_tools.media_type(payload) if payload else ""
        self.db.execute(
            "INSERT INTO works (aweme_id, author_sec_uid, author_uid, author_name, title, "
            "create_time, media_type, metadata, visibility, visibility_note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(aweme_id) DO UPDATE SET "
            "author_sec_uid = CASE WHEN works.author_sec_uid = '' THEN excluded.author_sec_uid "
            "ELSE works.author_sec_uid END, "
            "author_name = CASE WHEN works.author_name = '' THEN excluded.author_name "
            "ELSE works.author_name END, "
            "title = CASE WHEN works.title = '' THEN excluded.title ELSE works.title END, "
            "create_time = MAX(works.create_time, excluded.create_time), "
            "media_type = CASE WHEN works.media_type = '' THEN excluded.media_type "
            "ELSE works.media_type END, "
            "metadata = CASE WHEN length(works.metadata) < length(excluded.metadata) "
            "THEN excluded.metadata ELSE works.metadata END, "
            "visibility = CASE WHEN excluded.visibility = 'hidden' THEN 'hidden' "
            "ELSE works.visibility END, "
            "visibility_note = CASE WHEN excluded.visibility_note <> '' "
            "THEN excluded.visibility_note ELSE works.visibility_note END",
            (aweme_id,
             author_sec_uid or str(owner.get("sec_uid") or ""),
             str(owner.get("uid") or ""),
             author_name or str(owner.get("nickname") or ""),
             title or str(payload.get("desc") or ""),
             create_time or _int(payload.get("create_time")),
             media_type,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
             visibility, note, self.now),
        )

    def download(self, aweme_id: str, status: str, *, attempts: int = 0, retry_at: int = 0,
                 error: str = "", code: str = "", downloaded_at: int = 0, origin: int = 0,
                 context: str = "", queued_at: int = 0) -> None:
        rank = {_DONE: 4, _FAILED: 2, _RETRY: 1, _PENDING: 0}
        existing = self.db.execute("SELECT status FROM downloads WHERE aweme_id = ?",
                                   (aweme_id,)).fetchone()
        if existing is not None and rank.get(str(existing[0]), 0) >= rank.get(status, 0):
            if status == _DONE and context:
                self.db.execute("UPDATE downloads SET context = CASE WHEN context = '' THEN ? "
                                "ELSE context END WHERE aweme_id = ?", (context, aweme_id))
            return
        self.db.execute(
            "INSERT OR REPLACE INTO downloads (aweme_id, status, origin, context, attempts, "
            "next_retry_at, error, error_code, queued_at, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aweme_id, status, int(origin), context, int(attempts), int(retry_at),
             str(error or "")[:500], str(code or ""), int(queued_at or self.now),
             int(downloaded_at or 0)),
        )

    def file(self, aweme_id: str, path: str, kind: str = "", size: int = 0) -> None:
        path = str(path or "").strip()
        if not path or path.startswith(".unavailable"):
            return
        guessed_kind, position = classify_file(Path(path).name)
        kind = {"metadata": "json"}.get(kind, kind) or guessed_kind
        if kind == "media":
            kind = guessed_kind if guessed_kind in ("media", "live", "motion") else "media"
        if kind not in ("media", "live", "motion", "cover", "music", "json", "comments"):
            return
        self.db.execute(
            "INSERT OR IGNORE INTO work_files (aweme_id, path, kind, position, size) "
            "VALUES (?, ?, ?, ?, ?)", (aweme_id, path, kind, position, int(size or 0)))

    def source(self, kind: str, sec_uid: str, **fields: Any) -> int:
        columns = ["kind", "sec_uid", *fields]
        self.db.execute(
            f"INSERT OR IGNORE INTO sources ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            (kind, sec_uid, *fields.values()),
        )
        return int(self.db.execute("SELECT id FROM sources WHERE kind = ? AND sec_uid = ?",
                                   (kind, sec_uid)).fetchone()[0])

    def member(self, source_id: int, aweme_id: str, *, first_seen: int, last_seen: int = 0,
               epoch: int = 0, state: str = "listed", note: str = "") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO source_works (source_id, aweme_id, first_seen_at, "
            "last_seen_at, seen_epoch, state, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, aweme_id, int(first_seen or self.now), int(last_seen or first_seen or 0),
             int(epoch), state, note),
        )


def _import_archive(im: _Importer, tables: Set[str]) -> None:
    if "aweme" not in tables:
        return
    contexts: Dict[str, str] = {}
    if "download_source" in tables:
        for row in im.db.execute(
            "SELECT aweme_id, source_type, source_sec_uid, source_name, mode, collection_id, "
            "collection_name FROM download_source ORDER BY id"
        ):
            contexts[str(row[0])] = json.dumps({
                "source_type": row[1], "source_sec_uid": row[2] or None,
                "source_name": row[3] or "", "mode": row[4] or "post",
                "collection_id": row[5] or None, "collection_name": row[6] or None,
            }, ensure_ascii=False)
    columns = _columns(im.db, "aweme")
    sec_uid_column = "author_sec_uid" if "author_sec_uid" in columns else "''"
    for row in im.db.execute(
        f"SELECT aweme_id, aweme_type, title, author_name, {sec_uid_column}, create_time, "
        "download_time, file_path, metadata FROM aweme"
    ).fetchall():
        aweme_id = str(row[0] or "").strip()
        if not aweme_id:
            continue
        kind = str(row[1] or "")
        im.work(aweme_id, payload=_slim(row[8]), author_sec_uid=str(row[4] or ""),
                author_name=str(row[3] or ""), title=str(row[2] or ""),
                create_time=_int(row[5]),
                media_type=kind if kind in ("video", "gallery", "music") else "")
        im.download(aweme_id, _DONE, downloaded_at=_int(row[6]) or im.now,
                    context=contexts.get(aweme_id, ""))
        im.file(aweme_id, str(row[7] or ""))
        im.count("archive")
    if "aweme_asset" in tables:
        for row in im.db.execute(
            "SELECT aweme_id, file_path, asset_type, file_size FROM aweme_asset"
        ).fetchall():
            im.file(str(row[0]), str(row[1] or ""), str(row[2] or ""), _int(row[3]))


_WATCH_STATUS = {
    "success": _DONE, "retryable_failed": _RETRY, "permanent_failed": _FAILED,
    "pending": _PENDING, "downloading": _PENDING,
}


def _import_monitor(im: _Importer, tables: Set[str]) -> None:
    if "watch_creator" not in tables:
        return
    creator_columns = _columns(im.db, "watch_creator")
    optional = [name for name in ("last_full_scan_at", "public_aweme_count", "latest_aweme_id",
                                  "latest_aweme_title", "latest_aweme_time", "last_check_note")
                if name in creator_columns]
    select = ", ".join(["sec_uid", "nickname", "avatar_url", "enabled", "added_at",
                        "last_check_at", "last_success_at", "last_error", "failure_count",
                        *optional])
    source_ids: Dict[str, int] = {}
    for row in im.db.execute(f"SELECT {select} FROM watch_creator").fetchall():
        values = dict(row)
        sec_uid = str(values.get("sec_uid") or "").strip()
        if not sec_uid:
            continue
        source_ids[sec_uid] = im.source(
            "creator", sec_uid,
            nickname=str(values.get("nickname") or ""),
            avatar_url=str(values.get("avatar_url") or ""),
            enabled=1 if _int(values.get("enabled")) else 0,
            added_at=_int(values.get("added_at")) or im.now,
            last_check_at=_int(values.get("last_check_at")),
            last_success_at=_int(values.get("last_success_at")),
            last_incremental_at=_int(values.get("last_success_at")),
            last_full_at=_int(values.get("last_full_scan_at")),
            last_error=str(values.get("last_error") or ""),
            last_note=str(values.get("last_check_note") or ""),
            failure_count=_int(values.get("failure_count")),
            cloud_count=_int(values.get("public_aweme_count")),
            latest_aweme_id=str(values.get("latest_aweme_id") or ""),
            latest_aweme_title=str(values.get("latest_aweme_title") or ""),
            latest_aweme_time=_int(values.get("latest_aweme_time")),
        )
        im.count("creators")
    if "watch_aweme" not in tables:
        return
    columns = _columns(im.db, "watch_aweme")
    wanted = ["sec_uid", "aweme_id", "create_time", "discovered_at", "status", "downloaded",
              "downloaded_at", "metadata", "visibility_status", "download_status",
              "last_error_code", "last_error_message", "retry_count", "next_retry_at",
              "last_seen_at", "failure_reason"]
    select = ", ".join(name if name in columns else f"NULL AS {name}" for name in wanted)
    cursor = im.db.execute(f"SELECT {select} FROM watch_aweme")
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        for row in batch:
            values = dict(row)
            sec_uid = str(values["sec_uid"] or "")
            aweme_id = str(values["aweme_id"] or "").strip()
            source_id = source_ids.get(sec_uid)
            if not source_id or not aweme_id or aweme_id.startswith("__"):
                continue
            payload = _slim(values["metadata"])
            if payload and aweme_tools.author_sec_uid(payload) \
                    and not aweme_tools.belongs_to(payload, sec_uid):
                continue  # a related/recommended work that leaked into the ledger
            legacy_status = str(values["status"] or "")
            hidden = legacy_status == "invisible" or str(values["visibility_status"] or "") \
                not in ("", "visible", "unknown")
            note = str(values["last_error_message"] or values["failure_reason"] or "")
            im.work(aweme_id, payload=payload, author_sec_uid=sec_uid,
                    create_time=_int(values["create_time"]),
                    visibility="hidden" if hidden else "unknown", note=note if hidden else "")
            im.member(source_id, aweme_id, first_seen=_int(values["discovered_at"]),
                      last_seen=_int(values["last_seen_at"]))
            if legacy_status in ("baseline", "ignored"):
                continue
            status = _WATCH_STATUS.get(str(values["download_status"] or ""), _PENDING)
            if _int(values["downloaded"]) or legacy_status in ("downloaded", "existing"):
                status = _DONE
            if hidden and status != _DONE:
                status = _FAILED
            im.download(aweme_id, status, attempts=_int(values["retry_count"]),
                        retry_at=_int(values["next_retry_at"]), error=note,
                        code=str(values["last_error_code"] or ""),
                        downloaded_at=_int(values["downloaded_at"]), origin=source_id,
                        queued_at=_int(values["discovered_at"]))
            im.count("monitor_works")


_SUB_STATUS = {"pending": _PENDING, "retry": _RETRY, "done": _DONE, "failed": _FAILED}


def _import_subscriptions(im: _Importer, db_path: Path) -> None:
    path = db_path.with_name(f"{db_path.stem}.subscriptions.db")
    if not path.is_file():
        return
    try:
        subs = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        subs.row_factory = sqlite3.Row
        tables = _tables(subs)
        source_rows = subs.execute("SELECT * FROM sub_source").fetchall() \
            if "sub_source" in tables else []
        work_rows = subs.execute("SELECT * FROM sub_work").fetchall() \
            if "sub_work" in tables else []
        subs.close()
    except sqlite3.Error as exc:
        logger.warning("Could not read legacy subscriptions %s: %s", path, exc)
        return
    ids: Dict[int, int] = {}
    for row in source_rows:
        values = dict(row)
        ids[_int(values["id"])] = im.source(
            str(values["kind"]), str(values["account_sec_uid"]),
            nickname=str(values.get("nickname") or ""),
            avatar_url=str(values.get("avatar_url") or ""),
            is_self=_int(values.get("is_self")), enabled=_int(values.get("enabled")),
            added_at=_int(values.get("added_at")) or im.now,
            cloud_count=_int(values.get("cloud_count")),
            last_incremental_at=_int(values.get("last_incremental_at")),
            last_full_at=_int(values.get("last_full_at")),
            last_check_at=max(_int(values.get("last_incremental_at")),
                              _int(values.get("last_full_at"))),
            full_epoch=_int(values.get("full_epoch")),
            full_cursor=_int(values.get("full_cursor")),
            full_pages=_int(values.get("full_pages")),
            full_seen=_int(values.get("full_seen")),
            paused_until=_int(values.get("paused_until")),
            last_error=str(values.get("last_error") or ""),
            last_note=str(values.get("last_note") or ""),
        )
        im.count("subscriptions")
    for row in work_rows:
        values = dict(row)
        source_id = ids.get(_int(values["source_id"]))
        aweme_id = str(values.get("aweme_id") or "").strip()
        if not source_id or not aweme_id:
            continue
        im.work(aweme_id, payload=_slim(values.get("metadata")),
                author_sec_uid=str(values.get("author_sec_uid") or ""),
                author_name=str(values.get("author_name") or ""),
                title=str(values.get("title") or ""),
                create_time=_int(values.get("create_time")))
        status = str(values.get("status") or "")
        gone = status in ("missing", "removed")
        im.member(source_id, aweme_id, first_seen=_int(values.get("first_seen_at")),
                  epoch=_int(values.get("last_seen_epoch")),
                  state=status if gone else "listed",
                  note=str(values.get("error") or "") if gone else "")
        mapped = _SUB_STATUS.get(status)
        if mapped:
            im.download(aweme_id, mapped, attempts=_int(values.get("retry_count")),
                        retry_at=_int(values.get("next_retry_at")),
                        error=str(values.get("error") or ""),
                        downloaded_at=_int(values.get("updated_at")) if mapped == _DONE
                        else 0,
                        origin=source_id, queued_at=_int(values.get("first_seen_at")))
        try:
            files = json.loads(values.get("files") or "[]")
        except ValueError:
            files = []
        for entry in files if isinstance(files, list) else []:
            if isinstance(entry, dict):
                im.file(aweme_id, str(entry.get("file_path") or ""),
                        str(entry.get("asset_type") or ""))
        im.count("subscription_works")


def _import_comments(im: _Importer, tables: Set[str]) -> None:
    if "aweme_comment" in tables:
        im.db.execute(
            "INSERT OR IGNORE INTO comments (aweme_id, cid, parent_cid, is_reply, nickname, "
            "text, digg_count, create_time, ip_label, image_count, live_image_count, raw, "
            "fetched_at) SELECT aweme_id, comment_id, parent_comment_id, is_reply, nickname, "
            "text, digg_count, create_time, ip_label, image_count, live_image_count, "
            "COALESCE(raw_json, ''), fetched_at FROM aweme_comment"
        )
    if "aweme_comment_image" in tables:
        im.db.execute(
            "INSERT OR IGNORE INTO comment_images (aweme_id, cid, idx, urls, preview_url, "
            "live_urls, width, height, thumb_path, local_path) SELECT aweme_id, comment_id, "
            "image_index, urls_json, preview_url, live_video_urls_json, width, height, "
            "thumb_path, local_path FROM aweme_comment_image"
        )
    if "aweme_comment_sync" in tables:
        im.db.execute(
            "INSERT OR IGNORE INTO comment_sync (aweme_id, complete, status, attempts, "
            "last_attempt_at, last_success_at, last_error, visibility) SELECT aweme_id, "
            "is_complete, status, attempt_count, COALESCE(last_attempt_at, 0), "
            "COALESCE(last_success_at, 0), COALESCE(last_error, ''), visibility "
            "FROM aweme_comment_sync"
        )


def _import_risk_events(im: _Importer, tables: Set[str]) -> None:
    if "risk_event" in tables:
        im.db.execute(
            "INSERT INTO risk_events (timestamp, type, category, path, http_status, "
            "api_status, reason) SELECT timestamp, type, category, path, http_status, "
            "api_status, reason FROM risk_event ORDER BY id"
        )


def import_all(connection: sqlite3.Connection, db_path: Path) -> str:
    """Import every legacy table present; returns a short summary."""
    tables = _tables(connection)
    importer = _Importer(connection)
    started = time.monotonic()
    _import_archive(importer, tables)
    _import_monitor(importer, tables)
    _import_subscriptions(importer, Path(db_path))
    _import_comments(importer, tables)
    _import_risk_events(importer, tables)
    summary = ", ".join(f"{key}={value}" for key, value in sorted(importer.counts.items()))
    if importer.counts:
        logger.info("Imported legacy data in %.1fs: %s", time.monotonic() - started, summary)
    return summary or "none"
