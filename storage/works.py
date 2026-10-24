"""Works, the shared download ledger, and local files.

Every function takes an open connection as its first argument and is meant
to be run through :class:`storage.db.Database` (``db.read(fn, …)`` /
``db.write(fn, …)``).

Download states (``downloads.status``):

``pending``  waiting for its first (or a manual) attempt
``active``   being downloaded right now
``retry``    failed, due again at ``next_retry_at``
``done``     media + JSON saved; cover/soundtrack may still be missing
             (``extras_missing``), retried at low priority
``failed``   given up: inaccessible, or too many attempts
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from douyin import aweme as aweme_tools
from storage.db import chunks, marks, one, rows, scalar

READY = "(d.status = 'pending' OR (d.status = 'retry' AND d.next_retry_at <= ?))"
EXTRAS_MAX_ATTEMPTS = 5
EXTRAS_RETRY_SECONDS = 30 * 60


def _now() -> int:
    return int(time.time())


# ── works ───────────────────────────────────────────────────────────


def save_works(db: sqlite3.Connection, payloads: Iterable[Mapping[str, Any]]) -> None:
    """Insert or refresh works from listing/detail payloads (slimmed)."""
    now = _now()
    for payload in payloads:
        aweme_id = aweme_tools.aweme_id(payload)
        if not aweme_id:
            continue
        owner = aweme_tools.author(payload)
        metadata = aweme_tools.slim_json(payload)
        db.execute(
            "INSERT INTO works (aweme_id, author_sec_uid, author_uid, author_name, title, "
            "create_time, media_type, is_pinned, metadata, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(aweme_id) DO UPDATE SET "
            "author_sec_uid = CASE WHEN excluded.author_sec_uid <> '' "
            "THEN excluded.author_sec_uid ELSE works.author_sec_uid END, "
            "author_uid = CASE WHEN excluded.author_uid <> '' THEN excluded.author_uid "
            "ELSE works.author_uid END, "
            "author_name = CASE WHEN excluded.author_name <> '' THEN excluded.author_name "
            "ELSE works.author_name END, "
            "title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE works.title END, "
            "create_time = MAX(works.create_time, excluded.create_time), "
            "media_type = excluded.media_type, "
            "is_pinned = CASE WHEN ? THEN excluded.is_pinned ELSE works.is_pinned END, "
            "metadata = CASE WHEN length(excluded.metadata) >= length(works.metadata) * 0.6 "
            "THEN excluded.metadata ELSE works.metadata END, "
            "visibility = CASE WHEN works.visibility = 'hidden' THEN 'visible' "
            "ELSE works.visibility END, updated_at = excluded.updated_at",
            (aweme_id, str(owner.get("sec_uid") or ""), str(owner.get("uid") or ""),
             str(owner.get("nickname") or ""), aweme_tools.title(payload),
             aweme_tools.create_time(payload), aweme_tools.media_type(payload),
             int(aweme_tools.is_pinned(payload)), metadata, now,
             int(aweme_tools.has_pin_state(payload))),
        )


def get_work(db: sqlite3.Connection, aweme_id: str) -> Optional[Dict[str, Any]]:
    row = one(db, "SELECT w.*, d.status, d.error, d.error_code, d.downloaded_at, d.context, "
                  "d.origin, d.extras_missing FROM works w "
                  "LEFT JOIN downloads d ON d.aweme_id = w.aweme_id WHERE w.aweme_id = ?",
              (str(aweme_id),))
    if row is not None:
        row["files"] = files_of(db, [aweme_id]).get(str(aweme_id), [])
    return row


def set_visibility(db: sqlite3.Connection, aweme_id: str, visibility: str, note: str = "") -> None:
    db.execute("UPDATE works SET visibility = ?, visibility_note = ?, updated_at = ? "
               "WHERE aweme_id = ?", (visibility, note, _now(), str(aweme_id)))


# ── ledger ──────────────────────────────────────────────────────────


def statuses(db: sqlite3.Connection, aweme_ids: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for batch in chunks([str(value) for value in aweme_ids if value]):
        for row in db.execute(f"SELECT aweme_id, status FROM downloads WHERE aweme_id IN "
                              f"({marks(batch)})", batch):
            result[str(row[0])] = str(row[1])
    return result


def queue(db: sqlite3.Connection, aweme_ids: Sequence[str], origin: int = 0) -> List[str]:
    """Create pending ledger rows; returns the IDs that were not known yet.

    Rows that exist are left alone (a finished work is not downloaded again,
    a failed one is only retried on request).
    """
    now = _now()
    fresh = []
    for aweme_id in dict.fromkeys(str(value) for value in aweme_ids if value):
        cursor = db.execute("INSERT OR IGNORE INTO downloads (aweme_id, status, origin, "
                            "queued_at) VALUES (?, 'pending', ?, ?)",
                            (aweme_id, int(origin), now))
        if cursor.rowcount:
            fresh.append(aweme_id)
    return fresh


def drop_backlog(db: sqlite3.Connection) -> int:
    """Forget every work still waiting to be downloaded; a scan queues it again.

    Works being downloaded, finished or given up are kept.  Interrupted full
    scans restart from the first page, so the next one lists every work.
    """
    db.execute("UPDATE sources SET full_epoch = 0, full_cursor = 0, full_pages = 0, "
               "full_seen = 0")
    return db.execute("DELETE FROM downloads WHERE status IN ('pending', 'retry')").rowcount


def requeue(db: sqlite3.Connection, aweme_ids: Sequence[str], *, include_done: bool = False,
            error: str = "") -> List[str]:
    """Put failed (optionally finished) works back into the queue."""
    states = ("retry", "failed", "done") if include_done else ("retry", "failed")
    changed = []
    for batch in chunks([str(value) for value in aweme_ids if value]):
        found = [str(row[0]) for row in db.execute(
            f"SELECT aweme_id FROM downloads WHERE aweme_id IN ({marks(batch)}) "
            f"AND status IN ({marks(states)})", (*batch, *states))]
        for aweme_id in found:
            db.execute("UPDATE downloads SET status = 'pending', attempts = 0, next_retry_at = 0, "
                       "error = ?, error_code = '', queued_at = ? WHERE aweme_id = ?",
                       (error, _now(), aweme_id))
        changed.extend(found)
    return changed


def claim(db: sqlite3.Connection, aweme_id: str, origin: int, context: Mapping[str, Any]) -> bool:
    """Mark a work active; False when someone else already finished or holds it."""
    cursor = db.execute(
        "UPDATE downloads SET status = 'active', origin = ?, context = ? "
        "WHERE aweme_id = ? AND status IN ('pending', 'retry', 'failed')",
        (int(origin), json.dumps(dict(context), ensure_ascii=False), str(aweme_id)))
    if cursor.rowcount:
        return True
    cursor = db.execute(
        "INSERT OR IGNORE INTO downloads (aweme_id, status, origin, context, queued_at) "
        "VALUES (?, 'active', ?, ?, ?)",
        (str(aweme_id), int(origin), json.dumps(dict(context), ensure_ascii=False), _now()))
    return bool(cursor.rowcount)


def release(db: sqlite3.Connection, aweme_ids: Sequence[str]) -> None:
    for batch in chunks([str(value) for value in aweme_ids]):
        db.execute(f"UPDATE downloads SET status = 'pending' WHERE status = 'active' "
                   f"AND aweme_id IN ({marks(batch)})", batch)


def save_files(db: sqlite3.Connection, aweme_id: str, files: Iterable[Mapping[str, Any]]) -> None:
    for entry in files:
        path = str(entry.get("path") or "")
        if not path:
            continue
        db.execute("INSERT INTO work_files (aweme_id, path, kind, position, size) "
                   "VALUES (?, ?, ?, ?, ?) ON CONFLICT(aweme_id, path) DO UPDATE SET "
                   "kind = excluded.kind, position = excluded.position, size = excluded.size",
                   (str(aweme_id), path, str(entry.get("kind") or "media"),
                    int(entry.get("position") or 0), int(entry.get("size") or 0)))


def record_success(db: sqlite3.Connection, aweme_id: str, *, files: Sequence[Mapping],
                   extras_missing: Sequence[str] = (), context: Optional[Mapping] = None,
                   origin: Optional[int] = None, payload: Optional[Mapping] = None) -> None:
    if payload:
        save_works(db, [payload])
        set_visibility(db, aweme_id, "visible")
    save_files(db, aweme_id, files)
    now = _now()
    db.execute(
        "INSERT INTO downloads (aweme_id, status, origin, context, queued_at, downloaded_at) "
        "VALUES (?, 'done', ?, ?, ?, ?) ON CONFLICT(aweme_id) DO UPDATE SET status = 'done', "
        "origin = excluded.origin, context = excluded.context, attempts = 0, next_retry_at = 0, "
        "error = '', error_code = '', downloaded_at = excluded.downloaded_at, "
        "extras_missing = ?, extras_attempts = 0, extras_retry_at = ?",
        (str(aweme_id), int(origin or 0), json.dumps(dict(context or {}), ensure_ascii=False),
         now, now, ",".join(extras_missing), now + EXTRAS_RETRY_SECONDS if extras_missing else 0),
    )


def record_failure(db: sqlite3.Connection, aweme_id: str, *, error: str, permanent: bool,
                   attempts: int, retry_at: int = 0, code: str = "") -> str:
    status = "failed" if permanent else "retry"
    db.execute(
        "INSERT INTO downloads (aweme_id, status, attempts, next_retry_at, error, error_code, "
        "queued_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(aweme_id) DO UPDATE SET "
        "status = CASE WHEN downloads.status = 'done' THEN 'done' ELSE excluded.status END, "
        "attempts = excluded.attempts, next_retry_at = excluded.next_retry_at, "
        "error = excluded.error, error_code = excluded.error_code",
        (str(aweme_id), status, int(attempts), int(retry_at), str(error or "")[:500], code,
         _now()))
    return status


def record_extras(db: sqlite3.Connection, aweme_id: str, *, files: Sequence[Mapping],
                  missing: Sequence[str], attempts: int) -> None:
    covers = [str(entry.get("path") or "") for entry in files if entry.get("kind") == "cover"]
    if covers:
        # The new cover supersedes covers recorded in other folders.
        db.execute(f"DELETE FROM work_files WHERE aweme_id = ? AND kind = 'cover' "
                   f"AND path NOT IN ({marks(covers)})", (str(aweme_id), *covers))
    for entry in files:
        if entry.get("kind") in ("media", "motion") and entry.get("path"):
            # A re-fetched picture may have a new extension: it replaces the old one.
            db.execute("DELETE FROM work_files WHERE aweme_id = ? AND kind = ? "
                       "AND position = ? AND path <> ?",
                       (str(aweme_id), entry["kind"], int(entry.get("position") or 0),
                        str(entry["path"])))
    save_files(db, aweme_id, files)
    retry_at = _now() + EXTRAS_RETRY_SECONDS if missing else 0
    db.execute("UPDATE downloads SET extras_missing = ?, extras_attempts = ?, "
               "extras_retry_at = ? WHERE aweme_id = ?",
               (",".join(missing), int(attempts), retry_at, str(aweme_id)))


def cover_files(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Downloaded video works' cover files, with the video's size from metadata."""
    result = []
    for row in rows(db, "SELECT f.aweme_id, f.path, w.metadata FROM work_files f "
                        "JOIN works w ON w.aweme_id = f.aweme_id "
                        "JOIN downloads d ON d.aweme_id = f.aweme_id "
                        "WHERE f.kind = 'cover' AND w.media_type = 'video' "
                        "AND d.status = 'done'"):
        video = aweme_tools.decode(row["metadata"]).get("video") or {}
        result.append({"aweme_id": str(row["aweme_id"]), "path": str(row["path"]),
                       "width": int(video.get("width") or 0),
                       "height": int(video.get("height") or 0)})
    return result


def gallery_json_files(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Downloaded galleries: their saved JSON and picture files by position."""
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows(db, "SELECT f.aweme_id, f.path, f.kind, f.position FROM work_files f "
                        "JOIN works w ON w.aweme_id = f.aweme_id "
                        "JOIN downloads d ON d.aweme_id = f.aweme_id "
                        "WHERE f.kind IN ('json', 'media') AND w.media_type = 'gallery' "
                        "AND d.status = 'done'"):
        entry = result.setdefault(str(row["aweme_id"]), {
            "aweme_id": str(row["aweme_id"]), "json": "", "pictures": {}})
        if row["kind"] == "json":
            entry["json"] = str(row["path"])
        else:
            entry["pictures"][int(row["position"] or 0)] = str(row["path"])
    return [entry for entry in result.values() if entry["json"] and entry["pictures"]]


def queue_cover_fix(db: sqlite3.Connection, aweme_ids: Sequence[str], start: int,
                    spacing: int, kind: str = "cover_fix") -> int:
    """Queue ``kind`` extras, one due every ``spacing`` seconds from ``start``."""
    queued = 0
    for aweme_id in aweme_ids:
        row = one(db, "SELECT extras_missing FROM downloads WHERE aweme_id = ? "
                      "AND status = 'done'", (str(aweme_id),))
        if row is None:
            continue
        kinds = [item for item in str(row["extras_missing"] or "").split(",") if item]
        if kind in kinds:
            continue
        db.execute("UPDATE downloads SET extras_missing = ?, extras_attempts = 0, "
                   "extras_retry_at = ? WHERE aweme_id = ?",
                   (",".join([*kinds, kind]), start + queued * spacing, str(aweme_id)))
        queued += 1
    return queued


def forget_downloads(db: sqlite3.Connection, aweme_ids: Sequence[str]) -> None:
    """Drop ledger rows and file records (after the files were deleted)."""
    for batch in chunks([str(value) for value in aweme_ids]):
        db.execute(f"DELETE FROM downloads WHERE aweme_id IN ({marks(batch)})", batch)
        db.execute(f"DELETE FROM work_files WHERE aweme_id IN ({marks(batch)})", batch)


def cancel_orphans(db: sqlite3.Connection) -> int:
    """Pending downloads no source and no manual request wants any more."""
    return db.execute(
        "DELETE FROM downloads WHERE status IN ('pending', 'retry') AND origin > 0 AND NOT "
        "EXISTS (SELECT 1 FROM source_works sw JOIN sources s ON s.id = sw.source_id "
        "WHERE sw.aweme_id = downloads.aweme_id AND sw.state = 'listed')").rowcount


# ── files ───────────────────────────────────────────────────────────


def files_of(db: sqlite3.Connection, aweme_ids: Sequence[str]) -> Dict[str, List[Dict]]:
    result: Dict[str, List[Dict]] = {}
    for batch in chunks([str(value) for value in aweme_ids if value]):
        for row in rows(db, f"SELECT aweme_id, path, kind, position, size FROM work_files "
                            f"WHERE aweme_id IN ({marks(batch)}) ORDER BY kind, position, path",
                        batch):
            result.setdefault(str(row["aweme_id"]), []).append(row)
    return result


def remove_files(db: sqlite3.Connection, paths: Sequence[str]) -> None:
    for batch in chunks([str(value) for value in paths]):
        db.execute(f"DELETE FROM work_files WHERE path IN ({marks(batch)})", batch)


def rewrite_paths(db: sqlite3.Connection, old_prefix: str, new_prefix: str) -> int:
    """Follow a renamed folder: every stored path under ``old_prefix`` moves."""
    changed = 0
    for sep in ("\\", "/"):
        before = old_prefix.rstrip("\\/") + sep
        after = new_prefix.rstrip("\\/") + sep
        changed += db.execute(
            "UPDATE OR REPLACE work_files SET path = ? || substr(path, ?) "
            "WHERE substr(path, 1, ?) = ?", (after, len(before) + 1, len(before), before)
        ).rowcount
        db.execute("UPDATE comment_images SET thumb_path = ? || substr(thumb_path, ?) "
                   "WHERE substr(thumb_path, 1, ?) = ?",
                   (after, len(before) + 1, len(before), before))
    return changed


def done_media(db: sqlite3.Connection, aweme_ids: Optional[Sequence[str]] = None) -> List[Dict]:
    """Finished works with their media paths (for local-file validation)."""
    sql = ("SELECT d.aweme_id, f.path, f.kind FROM downloads d "
           "LEFT JOIN work_files f ON f.aweme_id = d.aweme_id "
           "AND f.kind IN ('media', 'live', 'json') WHERE d.status = 'done'")
    if aweme_ids is None:
        return rows(db, sql)
    result: List[Dict] = []
    for batch in chunks([str(value) for value in aweme_ids]):
        result.extend(rows(db, sql + f" AND d.aweme_id IN ({marks(batch)})", batch))
    return result


# ── queue reads ─────────────────────────────────────────────────────


def ready_sources(db: sqlite3.Connection, kinds: Sequence[str], now: int) -> List[int]:
    return [int(row[0]) for row in db.execute(
        f"SELECT DISTINCT sw.source_id FROM downloads d "
        f"JOIN source_works sw ON sw.aweme_id = d.aweme_id AND sw.state = 'listed' "
        f"JOIN sources s ON s.id = sw.source_id AND s.enabled = 1 "
        f"AND s.kind IN ({marks(kinds)}) WHERE {READY} ORDER BY sw.source_id",
        (*kinds, now))]


def ready_count(db: sqlite3.Connection, kinds: Sequence[str], now: int) -> int:
    """Due works wanted by an enabled source of these kinds."""
    return int(scalar(db, f"SELECT COUNT(DISTINCT d.aweme_id) FROM downloads d "
                          f"JOIN source_works sw ON sw.aweme_id = d.aweme_id "
                          f"AND sw.state = 'listed' JOIN sources s ON s.id = sw.source_id "
                          f"AND s.enabled = 1 AND s.kind IN ({marks(kinds)}) WHERE {READY}",
                      (*kinds, now), 0) or 0)


def ready_by_source(db: sqlite3.Connection, kinds: Sequence[str], now: int) -> Dict[int, int]:
    """Due works per enabled source of these kinds."""
    return {int(row[0]): int(row[1]) for row in db.execute(
        f"SELECT sw.source_id, COUNT(*) FROM downloads d "
        f"JOIN source_works sw ON sw.aweme_id = d.aweme_id AND sw.state = 'listed' "
        f"JOIN sources s ON s.id = sw.source_id AND s.enabled = 1 "
        f"AND s.kind IN ({marks(kinds)}) WHERE {READY} GROUP BY sw.source_id",
        (*kinds, now))}


def peek_source(db: sqlite3.Connection, source_id: int, now: int, limit: int,
                exclude: Set[str]) -> List[Dict[str, Any]]:
    """The next due works of one source in download order: newest first for
    creators, list order (newest scan, then position) for subscriptions."""
    if limit <= 0:
        return []
    kind = scalar(db, "SELECT kind FROM sources WHERE id = ?", (source_id,), "")
    order = "w.create_time DESC, w.aweme_id DESC" if kind == "creator" \
        else "sw.first_seen_at DESC, sw.rowid ASC"
    found = rows(db, f"SELECT d.aweme_id, d.attempts, w.metadata, w.author_name, "
                     f"w.author_sec_uid, w.title FROM downloads d "
                     f"JOIN source_works sw ON sw.aweme_id = d.aweme_id "
                     f"AND sw.source_id = ? AND sw.state = 'listed' "
                     f"LEFT JOIN works w ON w.aweme_id = d.aweme_id "
                     f"WHERE {READY} ORDER BY {order} LIMIT ?",
                 (source_id, now, len(exclude) + limit))
    return [row for row in found if row["aweme_id"] not in exclude][:limit]


def next_for_source(db: sqlite3.Connection, source_id: int, now: int,
                    exclude: Set[str]) -> Optional[Dict[str, Any]]:
    """The next due work of one source (see ``peek_source``)."""
    found = peek_source(db, source_id, now, 1, exclude)
    return found[0] if found else None


def next_extras(db: sqlite3.Connection, now: int, exclude: Set[str]) -> Optional[Dict]:
    for row in rows(db, "SELECT d.aweme_id, d.extras_missing, d.extras_attempts, d.origin, "
                        "d.context, w.metadata FROM downloads d LEFT JOIN works w "
                        "ON w.aweme_id = d.aweme_id WHERE d.status = 'done' "
                        "AND d.extras_missing <> '' AND d.extras_attempts < ? "
                        "AND d.extras_retry_at <= ? ORDER BY d.extras_retry_at LIMIT ?",
                    (EXTRAS_MAX_ATTEMPTS, now, len(exclude) + 1)):
        if row["aweme_id"] not in exclude:
            return row
    return None


def queue_counts(db: sqlite3.Connection, now: int) -> Dict[str, int]:
    """Background queue sizes (only works an enabled source wants)."""
    row = one(db, "SELECT "
                  "SUM(CASE WHEN d.status = 'pending' OR (d.status = 'retry' "
                  "AND d.next_retry_at <= ?) THEN 1 ELSE 0 END) AS ready, "
                  "SUM(CASE WHEN d.status = 'retry' AND d.next_retry_at > ? THEN 1 ELSE 0 END) "
                  "AS waiting FROM downloads d WHERE d.status IN ('pending', 'retry') AND "
                  "EXISTS (SELECT 1 FROM source_works sw JOIN sources s ON s.id = sw.source_id "
                  "WHERE sw.aweme_id = d.aweme_id AND sw.state = 'listed' AND s.enabled = 1)",
              (now, now)) or {}
    return {"ready": int(row.get("ready") or 0), "waiting": int(row.get("waiting") or 0)}


def peek_ready(db: sqlite3.Connection, now: int, limit: int, exclude: Set[str],
               kinds: Sequence[str] = ()) -> List[Dict]:
    """A preview of what the background queue will download next (only the
    works of sources of ``kinds`` when given)."""
    only = f"AND s.kind IN ({marks(kinds)}) " if kinds else ""
    return [row for row in rows(
        db, f"SELECT d.aweme_id, w.title, w.author_name, s.nickname AS source_name, "
            f"s.kind FROM downloads d JOIN source_works sw ON sw.aweme_id = d.aweme_id "
            f"AND sw.state = 'listed' JOIN sources s ON s.id = sw.source_id AND s.enabled = 1 "
            f"{only}LEFT JOIN works w ON w.aweme_id = d.aweme_id WHERE {READY} "
            f"GROUP BY d.aweme_id ORDER BY MIN(CASE s.kind WHEN 'creator' THEN 0 ELSE 1 END), "
            f"w.create_time DESC LIMIT ?", (*kinds, now, limit + len(exclude)))
        if row["aweme_id"] not in exclude][:limit]


# ── library ─────────────────────────────────────────────────────────


def history(db: sqlite3.Connection, *, page: int, size: int, author: str = "",
            title: str = "", media_type: str = "") -> Dict[str, Any]:
    where, params = ["d.status = 'done'"], []
    if author:
        where.append("w.author_name = ?")
        params.append(author)
    if title:
        where.append("lower(w.title) LIKE ?")
        params.append(f"%{title.lower()}%")
    if media_type:
        where.append("w.media_type = ?")
        params.append(media_type)
    clause = " AND ".join(where)
    total = int(scalar(db, f"SELECT COUNT(*) FROM downloads d JOIN works w "
                           f"ON w.aweme_id = d.aweme_id WHERE {clause}", params))
    items = rows(db, f"SELECT d.aweme_id, w.media_type AS aweme_type, w.title, w.author_name, "
                     f"w.author_sec_uid, w.create_time, d.downloaded_at AS download_time, "
                     f"(SELECT path FROM work_files f WHERE f.aweme_id = d.aweme_id "
                     f"AND f.kind = 'media' ORDER BY f.position LIMIT 1) AS file_path "
                     f"FROM downloads d JOIN works w ON w.aweme_id = d.aweme_id "
                     f"WHERE {clause} ORDER BY d.downloaded_at DESC, d.aweme_id DESC "
                     f"LIMIT ? OFFSET ?", (*params, size, max(0, (page - 1) * size)))
    for item in items:
        item["create_time"] = aweme_tools.publish_date(int(item["create_time"] or 0)) or "—"
        item["file_path"] = item["file_path"] or ""
    return {"total": total, "page": page, "size": size, "items": items}
