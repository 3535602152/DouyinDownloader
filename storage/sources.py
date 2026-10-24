"""Sources the app follows and the works they list.

A source is a monitored creator (``creator``) or an account's like/collect
list (``like`` / ``collect``).  All kinds share one scan-state model:
incremental checks, resumable full scans (``full_epoch``/``full_cursor``),
risk pauses (``paused_until``).  ``source_works`` records which work a
source lists; ``state`` becomes ``missing`` (gone from a complete full scan)
or ``removed`` (confirmed un-liked/un-collected).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from storage.db import chunks, marks, one, rows, scalar

KINDS = ("creator", "like", "collect")
UPDATABLE = {
    "nickname", "avatar_url", "uid", "is_self", "enabled", "last_check_at", "last_success_at",
    "last_incremental_at", "incremental_try_at", "last_full_at", "full_epoch", "full_cursor", "full_pages",
    "full_seen", "paused_until", "failure_count", "last_error", "last_note", "cloud_count",
    "latest_aweme_id", "latest_aweme_title", "latest_aweme_time",
}


def _now() -> int:
    return int(time.time())


def add(db: sqlite3.Connection, kind: str, sec_uid: str, *, nickname: str = "",
        avatar_url: str = "", is_self: bool = False, cloud_count: int = 0,
        uid: str = "", enable: bool = True) -> Tuple[int, bool]:
    """Create or refresh a source; returns ``(id, created)``."""
    if kind not in KINDS:
        raise ValueError(f"unknown source kind: {kind}")
    sec_uid = str(sec_uid or "").strip()
    if not sec_uid:
        raise ValueError("缺少 sec_uid")
    existing = one(db, "SELECT id FROM sources WHERE kind = ? AND sec_uid = ?", (kind, sec_uid))
    if existing:
        fields = {"nickname": nickname, "avatar_url": avatar_url, "uid": uid}
        sets = [f"{key} = ?" for key, value in fields.items() if value]
        values = [value for value in fields.values() if value]
        if cloud_count:
            sets.append("cloud_count = ?")
            values.append(int(cloud_count))
        if enable:
            sets += ["enabled = 1", "last_error = ''"]
        sets.append("is_self = ?")
        values.append(int(bool(is_self)))
        db.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = ?",
                   (*values, existing["id"]))
        return int(existing["id"]), False
    cursor = db.execute(
        "INSERT INTO sources (kind, sec_uid, uid, nickname, avatar_url, is_self, enabled, "
        "added_at, cloud_count) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (kind, sec_uid, uid, nickname, avatar_url, int(bool(is_self)), _now(), int(cloud_count)))
    return int(cursor.lastrowid), True


def get(db: sqlite3.Connection, source_id: int) -> Optional[Dict[str, Any]]:
    return one(db, "SELECT * FROM sources WHERE id = ?", (int(source_id),))


def find(db: sqlite3.Connection, kind: str, sec_uid: str) -> Optional[Dict[str, Any]]:
    return one(db, "SELECT * FROM sources WHERE kind = ? AND sec_uid = ?", (kind, str(sec_uid)))


def all_of(db: sqlite3.Connection, kinds: Sequence[str] = KINDS) -> List[Dict[str, Any]]:
    return rows(db, f"SELECT * FROM sources WHERE kind IN ({marks(kinds)}) "
                    f"ORDER BY added_at DESC, id ASC", tuple(kinds))


def ids_for(db: sqlite3.Connection, kind: str, sec_uids: Sequence[str]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for batch in chunks([str(value) for value in sec_uids]):
        for row in db.execute(f"SELECT sec_uid, id FROM sources WHERE kind = ? "
                              f"AND sec_uid IN ({marks(batch)})", (kind, *batch)):
            result[str(row[0])] = int(row[1])
    return result


def update(db: sqlite3.Connection, source_id: int, **fields: Any) -> None:
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if fields:
        db.execute(f"UPDATE sources SET {', '.join(f'{key} = ?' for key in fields)} "
                   f"WHERE id = ?", (*fields.values(), int(source_id)))


def set_enabled(db: sqlite3.Connection, source_ids: Sequence[int], enabled: bool) -> int:
    ids = [int(value) for value in source_ids]
    if not ids:
        return 0
    return db.execute(f"UPDATE sources SET enabled = ? WHERE id IN ({marks(ids)})",
                      (int(bool(enabled)), *ids)).rowcount


def set_comments(db: sqlite3.Connection, source_ids: Sequence[int], enabled: bool) -> int:
    """Whether a full scan of these sources also saves their works' comments."""
    ids = [int(value) for value in source_ids]
    if not ids:
        return 0
    return db.execute(f"UPDATE sources SET comments_enabled = ? WHERE id IN ({marks(ids)})",
                      (int(bool(enabled)), *ids)).rowcount


def delete(db: sqlite3.Connection, source_ids: Sequence[int]) -> int:
    ids = [int(value) for value in source_ids]
    if not ids:
        return 0
    db.execute(f"DELETE FROM source_works WHERE source_id IN ({marks(ids)})", ids)
    db.execute(f"DELETE FROM scan_batch_sources WHERE source_id IN ({marks(ids)})", ids)
    return db.execute(f"DELETE FROM sources WHERE id IN ({marks(ids)})", ids).rowcount


# ── membership ──────────────────────────────────────────────────────


def known(db: sqlite3.Connection, source_id: int, aweme_ids: Iterable[str]) -> Set[str]:
    found: Set[str] = set()
    for batch in chunks([str(value) for value in aweme_ids if value]):
        found.update(str(row[0]) for row in db.execute(
            f"SELECT aweme_id FROM source_works WHERE source_id = ? "
            f"AND aweme_id IN ({marks(batch)})", (int(source_id), *batch)))
    return found


def record_listing(db: sqlite3.Connection, source_id: int, aweme_ids: Sequence[str], *,
                   seen_at: int, epoch: int = 0) -> List[str]:
    """Remember that ``source_id`` lists these works; returns the new members.

    A work listed again after being marked missing/removed is listed again.
    ``seen_at`` (the scan's start time) orders subscription downloads:
    newer scans first, then list order within a scan.
    """
    fresh = []
    for aweme_id in dict.fromkeys(str(value) for value in aweme_ids if value):
        cursor = db.execute(
            "INSERT OR IGNORE INTO source_works (source_id, aweme_id, first_seen_at, "
            "last_seen_at, seen_epoch) VALUES (?, ?, ?, ?, ?)",
            (int(source_id), aweme_id, int(seen_at), int(seen_at), int(epoch)))
        if cursor.rowcount:
            fresh.append(aweme_id)
            continue
        db.execute(
            "UPDATE source_works SET last_seen_at = ?, seen_epoch = MAX(seen_epoch, ?), "
            "first_seen_at = CASE WHEN state <> 'listed' THEN ? ELSE first_seen_at END, "
            "state = 'listed', note = '' WHERE source_id = ? AND aweme_id = ?",
            (int(seen_at), int(epoch), int(seen_at), int(source_id), aweme_id))
    return fresh


def unseen_since(db: sqlite3.Connection, source_id: int, epoch: int) -> List[Dict[str, Any]]:
    """Listed works a full scan stamped ``epoch`` did not see again."""
    return rows(db, "SELECT aweme_id FROM source_works WHERE source_id = ? "
                    "AND state = 'listed' AND seen_epoch < ? AND first_seen_at < ?",
                (int(source_id), int(epoch), int(epoch)))


def mark_seen(db: sqlite3.Connection, source_id: int, aweme_id: str, epoch: int) -> None:
    db.execute("UPDATE source_works SET seen_epoch = MAX(seen_epoch, ?) "
               "WHERE source_id = ? AND aweme_id = ?", (int(epoch), int(source_id), aweme_id))


def mark_gone(db: sqlite3.Connection, source_id: int, aweme_id: str, state: str,
              note: str) -> None:
    if state not in ("missing", "removed"):
        raise ValueError(state)
    db.execute("UPDATE source_works SET state = ?, note = ? WHERE source_id = ? "
               "AND aweme_id = ?", (state, note, int(source_id), str(aweme_id)))


def other_listings(db: sqlite3.Connection, aweme_ids: Sequence[str],
                   excluding: Sequence[int]) -> Set[str]:
    """Works still listed by a source other than ``excluding``."""
    found: Set[str] = set()
    skip = [int(value) for value in excluding] or [-1]
    for batch in chunks([str(value) for value in aweme_ids]):
        found.update(str(row[0]) for row in db.execute(
            f"SELECT DISTINCT aweme_id FROM source_works WHERE state = 'listed' "
            f"AND aweme_id IN ({marks(batch)}) AND source_id NOT IN ({marks(skip)})",
            (*batch, *skip)))
    return found


def members(db: sqlite3.Connection, source_ids: Sequence[int]) -> List[str]:
    ids = [int(value) for value in source_ids]
    if not ids:
        return []
    return [str(row[0]) for row in db.execute(
        f"SELECT DISTINCT aweme_id FROM source_works WHERE source_id IN ({marks(ids)})", ids)]


def member_page(db: sqlite3.Connection, source_id: int, offset: int, limit: int,
                hidden_only: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
    """One homepage page: pinned works first, then newest, before LIMIT/OFFSET."""
    hidden = " AND w.visibility = 'hidden'" if hidden_only else ""
    found = rows(db, "SELECT w.*, d.status, d.error, d.error_code FROM source_works sw "
                     "JOIN works w ON w.aweme_id = sw.aweme_id LEFT JOIN downloads d "
                     f"ON d.aweme_id = sw.aweme_id WHERE sw.source_id = ?{hidden} "
                     "ORDER BY w.is_pinned DESC, w.create_time DESC, w.aweme_id DESC "
                     "LIMIT ? OFFSET ?",
                 (int(source_id), int(limit) + 1, int(offset)))
    return found[:limit], len(found) > limit


# ── table statistics ────────────────────────────────────────────────


def download_counts(db: sqlite3.Connection, kinds: Sequence[str]) -> Dict[int, Dict[str, Any]]:
    """Per source: ledger counts of its listed works, plus membership states."""
    day_ago = _now() - 86400
    result: Dict[int, Dict[str, Any]] = {}
    for row in rows(db, f"""
        SELECT sw.source_id AS id,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'done' THEN 1 ELSE 0 END) AS done,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'pending' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'active' THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'retry' THEN 1 ELSE 0 END) AS retry,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'failed' THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN sw.state = 'listed' AND d.status = 'failed'
                   AND w.visibility = 'hidden' THEN 1 ELSE 0 END) AS hidden,
          SUM(CASE WHEN d.status = 'done' AND d.downloaded_at >= ? THEN 1 ELSE 0 END) AS day,
          SUM(CASE WHEN sw.state = 'missing' THEN 1 ELSE 0 END) AS missing,
          SUM(CASE WHEN sw.state = 'removed' THEN 1 ELSE 0 END) AS removed,
          COUNT(*) AS total
        FROM source_works sw JOIN sources s ON s.id = sw.source_id
        LEFT JOIN downloads d ON d.aweme_id = sw.aweme_id
        LEFT JOIN works w ON w.aweme_id = sw.aweme_id
        WHERE s.kind IN ({marks(kinds)}) GROUP BY sw.source_id""", (day_ago, *kinds)):
        result[int(row["id"])] = {key: int(value or 0) for key, value in row.items()}
    for row in rows(db, f"""
        SELECT sw.source_id AS id, d.error AS error, MAX(d.queued_at) AS latest
        FROM source_works sw
        JOIN sources s ON s.id = sw.source_id JOIN downloads d ON d.aweme_id = sw.aweme_id
        WHERE s.kind IN ({marks(kinds)}) AND d.status IN ('retry', 'failed') AND d.error <> ''
        GROUP BY sw.source_id""", tuple(kinds)):
        result.setdefault(int(row["id"]), {})["last_error"] = str(row["error"] or "")
    return result


def creator_stats(db: sqlite3.Connection, kinds: Sequence[str],
                  now: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
    """Per source: profile-style figures for the optional 关注主播 columns.

    Hidden works are gone from a complete full scan or reported hidden by a
    detail request.  Durations and comment coverage count downloaded works;
    like counts are the snapshot taken when the work was last listed.
    """
    now = _now() if now is None else int(now)
    meta = "CASE WHEN json_valid(w.metadata) THEN w.metadata ELSE '{}' END"
    result: Dict[int, Dict[str, Any]] = {}

    def bucket(source_id: Any) -> Dict[str, Any]:
        return result.setdefault(int(source_id), {
            "hidden": 0, "new_1d": 0, "new_7d": 0, "latest_time": 0, "videos": 0,
            "galleries": 0, "duration_ms": 0, "done": 0, "comments_saved": 0,
            "top_digg": 0, "top_digg_title": "", "avg_digg": 0, "douyin_id": ""})

    for row in rows(db, f"""
        SELECT sw.source_id AS id,
          SUM(CASE WHEN sw.state = 'missing' OR w.visibility = 'hidden' THEN 1 ELSE 0 END) AS hidden,
          SUM(CASE WHEN sw.state <> 'removed' AND w.create_time >= ? THEN 1 ELSE 0 END) AS new_1d,
          SUM(CASE WHEN sw.state <> 'removed' AND w.create_time >= ? THEN 1 ELSE 0 END) AS new_7d,
          MAX(CASE WHEN sw.state <> 'removed' THEN w.create_time ELSE 0 END) AS latest_time,
          SUM(CASE WHEN sw.state <> 'removed' AND w.media_type = 'video' THEN 1 ELSE 0 END) AS videos,
          SUM(CASE WHEN sw.state <> 'removed' AND w.media_type = 'gallery' THEN 1 ELSE 0 END) AS galleries,
          SUM(CASE WHEN d.status = 'done' AND w.media_type = 'video'
                   THEN COALESCE(json_extract({meta}, '$.video.duration'),
                                 json_extract({meta}, '$.duration'), 0) ELSE 0 END) AS duration_ms,
          SUM(CASE WHEN d.status = 'done' THEN 1 ELSE 0 END) AS done,
          SUM(CASE WHEN d.status = 'done' AND c.complete = 1 THEN 1 ELSE 0 END) AS comments_saved
        FROM source_works sw JOIN sources s ON s.id = sw.source_id
        LEFT JOIN works w ON w.aweme_id = sw.aweme_id
        LEFT JOIN downloads d ON d.aweme_id = sw.aweme_id
        LEFT JOIN comment_sync c ON c.aweme_id = sw.aweme_id
        WHERE s.kind IN ({marks(kinds)}) GROUP BY sw.source_id""",
            (now - 86400, now - 7 * 86400, *kinds)):
        bucket(row["id"]).update({key: int(value or 0) for key, value in row.items() if key != "id"})
    # Only one MAX() per query, so SQLite takes the bare columns from that row.
    for row in rows(db, f"""
        SELECT sw.source_id AS id, MAX(json_extract({meta}, '$.statistics.digg_count')) AS top,
          w.title AS title, AVG(json_extract({meta}, '$.statistics.digg_count')) AS average
        FROM source_works sw JOIN sources s ON s.id = sw.source_id
        JOIN works w ON w.aweme_id = sw.aweme_id
        WHERE s.kind IN ({marks(kinds)}) AND sw.state <> 'removed'
          AND json_extract({meta}, '$.statistics.digg_count') IS NOT NULL
        GROUP BY sw.source_id""", tuple(kinds)):
        bucket(row["id"]).update({"top_digg": int(row["top"] or 0),
                                  "top_digg_title": str(row["title"] or ""),
                                  "avg_digg": int(round(float(row["average"] or 0)))})
    for row in rows(db, f"""
        SELECT sw.source_id AS id, MAX(w.create_time) AS latest,
          json_extract({meta}, '$.author.unique_id') AS unique_id,
          CAST(json_extract({meta}, '$.author.short_id') AS TEXT) AS short_id
        FROM source_works sw JOIN sources s ON s.id = sw.source_id
        JOIN works w ON w.aweme_id = sw.aweme_id AND w.author_sec_uid = s.sec_uid
        WHERE s.kind IN ({marks(kinds)})
          AND (COALESCE(json_extract({meta}, '$.author.unique_id'), '') <> ''
               OR COALESCE(CAST(json_extract({meta}, '$.author.short_id') AS TEXT), '0')
                  NOT IN ('', '0'))
        GROUP BY sw.source_id""", tuple(kinds)):
        unique_id = str(row["unique_id"] or "").strip()
        short_id = str(row["short_id"] or "").strip()
        bucket(row["id"])["douyin_id"] = unique_id or (short_id if short_id != "0" else "")
    return result


def local_file_stats(db: sqlite3.Connection, kinds: Sequence[str]) -> List[Dict[str, Any]]:
    """Every recorded file of every source's works (checked on disk by the caller)."""
    return rows(db, f"SELECT sw.source_id, f.aweme_id, f.path, f.kind, f.size "
                    f"FROM source_works sw JOIN sources s ON s.id = sw.source_id "
                    f"JOIN work_files f ON f.aweme_id = sw.aweme_id "
                    f"WHERE s.kind IN ({marks(kinds)})", tuple(kinds))


def pending_count(db: sqlite3.Connection, source_id: int) -> int:
    return int(scalar(db, "SELECT COUNT(*) FROM source_works sw JOIN downloads d "
                          "ON d.aweme_id = sw.aweme_id WHERE sw.source_id = ? "
                          "AND sw.state = 'listed' AND d.status IN ('pending', 'retry', 'active')",
                      (int(source_id),)))


def latest_listed(db: sqlite3.Connection, source_id: int) -> Optional[Mapping[str, Any]]:
    return one(db, "SELECT w.aweme_id, w.title, w.create_time FROM source_works sw "
                   "JOIN works w ON w.aweme_id = sw.aweme_id WHERE sw.source_id = ? "
                   "ORDER BY w.create_time DESC LIMIT 1", (int(source_id),))
