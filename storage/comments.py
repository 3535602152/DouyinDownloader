"""Saved comments, their images and per-work sync state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from storage.db import one, rows, scalar
from storage.schema import meta, set_meta

# A paced sync of a work with many comments can take hours.
CLAIM_TIMEOUT = 12 * 3600
_COMMENT_COUNT = ("CASE WHEN json_valid(w.metadata) THEN COALESCE(json_extract(w.metadata, "
                  "'$.statistics.comment_count'), -1) ELSE -1 END")


def _now() -> int:
    return int(time.time())


def save(db: sqlite3.Connection, aweme_id: str, comments: Sequence[Mapping[str, Any]],
         images: Sequence[Mapping[str, Any]], *, replace: bool = True) -> None:
    """Save one work's comments and images (keeps known local image paths).

    ``replace`` drops what was saved before; otherwise these are merged in
    (the background sync adds the rest of a work's comments page by page).
    A comment's ``position`` orders it, else its place in ``comments``.
    """
    now = _now()
    kept = {(row["cid"], row["idx"]): row for row in rows(
        db, "SELECT cid, idx, thumb_path, local_path FROM comment_images WHERE aweme_id = ?",
        (aweme_id,))}
    if replace:
        db.execute("DELETE FROM comments WHERE aweme_id = ?", (aweme_id,))
        db.execute("DELETE FROM comment_images WHERE aweme_id = ?", (aweme_id,))
    for index, comment in enumerate(comments):
        position = int(comment["position"]) if comment.get("position") is not None else index
        db.execute(
            "INSERT OR REPLACE INTO comments (aweme_id, cid, parent_cid, is_reply, nickname, "
            "text, digg_count, create_time, ip_label, image_count, live_image_count, position, "
            "raw, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aweme_id, str(comment["cid"]), str(comment.get("parent_cid") or ""),
             int(bool(comment.get("is_reply"))), str(comment.get("nickname") or ""),
             str(comment.get("text") or ""), int(comment.get("digg_count") or 0),
             int(comment.get("create_time") or 0), str(comment.get("ip_label") or ""),
             int(comment.get("image_count") or 0), int(comment.get("live_image_count") or 0),
             position, json.dumps(comment.get("raw") or {}, ensure_ascii=False), now))
    for image in images:
        key = (str(image["comment_id"]), int(image["index"]))
        previous = kept.get(key, {})
        db.execute(
            "INSERT OR REPLACE INTO comment_images (aweme_id, cid, idx, urls, preview_url, "
            "live_urls, width, height, thumb_path, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (aweme_id, key[0], key[1], json.dumps(list(image.get("urls") or [])),
             str(image.get("preview_url") or ""),
             json.dumps(list(image.get("live_video_urls") or [])),
             int(image.get("width") or 0), int(image.get("height") or 0),
             str(image.get("thumb_path") or previous.get("thumb_path") or ""),
             str(image.get("local_path") or previous.get("local_path") or "")))


def load(db: sqlite3.Connection, aweme_id: str) -> Dict[str, List[Dict[str, Any]]]:
    comments = rows(db, "SELECT * FROM comments WHERE aweme_id = ? ORDER BY position",
                    (aweme_id,))
    images = []
    for row in rows(db, "SELECT * FROM comment_images WHERE aweme_id = ? ORDER BY cid, idx",
                    (aweme_id,)):
        images.append({
            "comment_id": row["cid"], "index": int(row["idx"]),
            "urls": json.loads(row["urls"] or "[]"),
            "live_video_urls": json.loads(row["live_urls"] or "[]"),
            "preview_url": row["preview_url"], "width": row["width"], "height": row["height"],
            "is_live": row["live_urls"] not in ("", "[]"),
            "thumb_path": row["thumb_path"], "local_path": row["local_path"],
        })
    return {"comments": comments, "images": images}


def saved_page(db: sqlite3.Connection, aweme_id: str, *, parent_cid: str = "", author: str = "",
               offset: int = 0, limit: int = 10) -> Dict[str, Any]:
    """One page of saved comments in Douyin's order, with their images by cid.

    The top-level comments carry ``saved_replies``; ``parent_cid`` pages one
    comment's replies.  ``author`` (a sec_uid) pages the top-level comments
    the author wrote or replied under, with the author's replies by cid.
    """
    by_author = "json_extract({0}raw, '$.user.sec_uid') = ?"
    if author:
        where = (f"c.aweme_id = ? AND c.is_reply = 0 AND ({by_author.format('c.')} OR EXISTS "
                 "(SELECT 1 FROM comments r WHERE r.aweme_id = c.aweme_id AND r.is_reply = 1 "
                 f"AND r.parent_cid = c.cid AND {by_author.format('r.')}))")
        args: tuple = (aweme_id, author, author)
    else:
        where = "c.aweme_id = ? AND c.is_reply = ? AND c.parent_cid = ?"
        args = (aweme_id, int(bool(parent_cid)), parent_cid)
    replies = ("0" if author or parent_cid else
               "(SELECT COUNT(*) FROM comments r WHERE r.aweme_id = c.aweme_id "
               "AND r.is_reply = 1 AND r.parent_cid = c.cid)")
    found = rows(db, f"SELECT c.*, {replies} AS saved_replies FROM comments c WHERE {where} "
                     "ORDER BY c.position LIMIT ? OFFSET ?", (*args, limit + 1, max(0, offset)))
    page = found[:limit]
    author_replies: Dict[str, List[Dict[str, Any]]] = {}
    if author and page:
        marks = ",".join("?" * len(page))
        for row in rows(db, f"SELECT * FROM comments WHERE aweme_id = ? AND is_reply = 1 "
                            f"AND parent_cid IN ({marks}) AND {by_author.format('')} "
                            "ORDER BY position", (aweme_id, *(item["cid"] for item in page), author)):
            author_replies.setdefault(row["parent_cid"], []).append(row)
    cids = [item["cid"] for item in page] + [row["cid"] for found_replies in
                                             author_replies.values() for row in found_replies]
    images: Dict[str, List[Dict[str, Any]]] = {}
    if cids:
        marks = ",".join("?" * len(cids))
        for row in rows(db, f"SELECT * FROM comment_images WHERE aweme_id = ? AND cid IN ({marks}) "
                            "ORDER BY cid, idx", (aweme_id, *cids)):
            images.setdefault(row["cid"], []).append({
                "index": int(row["idx"]),
                "urls": json.loads(row["urls"] or "[]"), "preview_url": row["preview_url"],
                "width": row["width"], "height": row["height"],
                "thumb_path": row["thumb_path"], "local_path": row["local_path"]})
    total = int(scalar(db, f"SELECT COUNT(*) FROM comments c WHERE {where}", args) or 0)
    # The comment sync's downloaded pictures and stickers, by file name.
    files = {Path(row["path"]).name: row["path"] for row in rows(
        db, "SELECT path FROM work_files WHERE aweme_id = ? AND kind = 'comments'", (aweme_id,))}
    return {"comments": page, "author_replies": author_replies, "images": images,
            "files": files, "has_more": len(found) > limit, "total": total}


def set_image_path(db: sqlite3.Connection, aweme_id: str, cid: str, index: int, *,
                   thumb_path: str = "", local_path: str = "") -> None:
    if thumb_path:
        db.execute("UPDATE comment_images SET thumb_path = ? WHERE aweme_id = ? AND cid = ? "
                   "AND idx = ?", (thumb_path, aweme_id, cid, int(index)))
    if local_path:
        db.execute("UPDATE comment_images SET local_path = ? WHERE aweme_id = ? AND cid = ? "
                   "AND idx = ?", (local_path, aweme_id, cid, int(index)))


def claim_sync(db: sqlite3.Connection, aweme_id: str, *, interval_hours: int = 0,
               force: bool = False) -> bool:
    """Start syncing one work's comments unless another task owns it / it is fresh."""
    now = _now()
    db.execute("INSERT OR IGNORE INTO comment_sync (aweme_id) VALUES (?)", (aweme_id,))
    row = one(db, "SELECT * FROM comment_sync WHERE aweme_id = ?", (aweme_id,)) or {}
    if row.get("status") == "running" and now - int(row.get("last_attempt_at") or 0) \
            < CLAIM_TIMEOUT:
        return False
    if not force and row.get("complete"):
        if interval_hours <= 0 or now - int(row.get("last_success_at") or 0) \
                < interval_hours * 3600:
            return False
    db.execute("UPDATE comment_sync SET status = 'running', attempts = attempts + 1, "
               "last_attempt_at = ? WHERE aweme_id = ?", (now, aweme_id))
    return True


def finish_sync(db: sqlite3.Connection, aweme_id: str, *, success: bool, error: str = "",
                visibility: str = "unknown") -> None:
    """Record a sync; a success remembers the work's comment count it matched."""
    if success:
        db.execute(f"UPDATE comment_sync SET status = 'done', complete = 1, last_success_at = ?, "
                   f"last_error = '', visibility = ?, synced_count = COALESCE((SELECT "
                   f"{_COMMENT_COUNT} FROM works w WHERE w.aweme_id = ?), -1) "
                   f"WHERE aweme_id = ?", (_now(), visibility, aweme_id, aweme_id))
    else:
        db.execute("UPDATE comment_sync SET status = 'failed', last_error = ?, visibility = ? "
                   "WHERE aweme_id = ?", (str(error or "")[:500], visibility, aweme_id))


def release_sync(db: sqlite3.Connection, aweme_id: str) -> None:
    """A sync stopped before it finished (not a failure): due again at once."""
    db.execute("UPDATE comment_sync SET status = 'pending' WHERE aweme_id = ? "
               "AND status = 'running'", (aweme_id,))


# Downloaded works of creators with comments on, not being read right now.
_SYNCABLE = ("FROM sources s "
             "JOIN source_works sw ON sw.source_id = s.id AND sw.state = 'listed' "
             "JOIN downloads d ON d.aweme_id = sw.aweme_id AND d.status = 'done' "
             "LEFT JOIN works w ON w.aweme_id = d.aweme_id "
             "LEFT JOIN comment_sync c ON c.aweme_id = d.aweme_id "
             "WHERE s.kind = 'creator' AND s.comments_enabled = 1 "
             "AND COALESCE(c.visibility, '') <> 'inaccessible' "
             "AND NOT (COALESCE(c.status, '') = 'running' AND c.last_attempt_at > :claimed) ")
_WORK_COLUMNS = (f"d.aweme_id, s.id AS source_id, s.nickname, s.sec_uid, "
                 f"COALESCE(w.title, '') AS title, COALESCE(w.create_time, 0) AS create_time, "
                 f"{_COMMENT_COUNT} AS comment_count, (SELECT path FROM work_files f "
                 f"WHERE f.aweme_id = d.aweme_id AND f.kind IN ('json', 'media') "
                 f"ORDER BY f.kind = 'media', f.position LIMIT 1) AS path")


def _due_sql(columns: str) -> str:
    """Works whose first comments need reading (the first pass).

    Never read, never finished, or the comment count a scan saw changed since
    the last read (at most once per ``refresh_after``); a failed work waits
    ``retry_after`` before it is tried again.
    """
    return (f"SELECT {columns} {_SYNCABLE}"
            f"AND (COALESCE(c.status, '') <> 'failed' OR c.last_attempt_at < :retry) "
            f"AND (c.aweme_id IS NULL OR c.complete = 0 OR ({_COMMENT_COUNT} >= 0 "
            f"AND {_COMMENT_COUNT} <> c.synced_count AND c.last_success_at <= :refresh))")


def _due_args(refresh_after: int, retry_after: int) -> Dict[str, int]:
    now = _now()
    return {"claimed": now - CLAIM_TIMEOUT, "retry": now - int(retry_after),
            "refresh": now - int(refresh_after)}


def due_list(db: sqlite3.Connection, *, refresh_after: int, retry_after: int,
             limit: int = 1) -> List[Dict[str, Any]]:
    """The works to read next: the newest work (by publish time) first."""
    return rows(db, _due_sql(_WORK_COLUMNS)
                + " GROUP BY d.aweme_id ORDER BY COALESCE(w.create_time, 0) DESC, d.aweme_id DESC "
                  "LIMIT :limit", {**_due_args(refresh_after, retry_after), "limit": int(limit)})


def next_due(db: sqlite3.Connection, *, refresh_after: int, retry_after: int
             ) -> Optional[Dict[str, Any]]:
    found = due_list(db, refresh_after=refresh_after, retry_after=retry_after, limit=1)
    return found[0] if found else None


def due_count(db: sqlite3.Connection, *, refresh_after: int, retry_after: int) -> int:
    return int(scalar(db, _due_sql("COUNT(DISTINCT d.aweme_id)"),
                      _due_args(refresh_after, retry_after)) or 0)


# ── the second pass: every comment and reply ──────────────────────────


def _full_sql(columns: str) -> str:
    """Works whose first pass is done and whose remaining comments are due.

    Not finished yet (it resumes at ``full_cursor``), or finished but the
    comment count grew by at least ``grow_min`` and the last full read is
    ``refresh_after`` old; a failed slice waits ``retry_after``.
    """
    return (f"SELECT {columns} {_SYNCABLE}"
            f"AND c.complete = 1 AND {_COMMENT_COUNT} <> 0 AND c.full_failed_at < :retry "
            f"AND (c.full_done = 0 OR ({_COMMENT_COUNT} - c.full_count "
            f">= :grow_min AND c.full_at <= :refresh))")


def _full_args(refresh_after: int, retry_after: int, grow_min: int) -> Dict[str, int]:
    return {**_due_args(refresh_after, retry_after), "grow_min": int(grow_min)}


def full_list(db: sqlite3.Connection, *, refresh_after: int, retry_after: int, grow_min: int,
              limit: int = 1) -> List[Dict[str, Any]]:
    """The works to complete next: the fewest comments first, then the newest."""
    return rows(db, _full_sql(f"{_WORK_COLUMNS}, c.full_cursor, c.full_done, "
                              f"(SELECT COUNT(*) FROM comments m WHERE m.aweme_id = d.aweme_id) "
                              f"AS saved")
                + f" GROUP BY d.aweme_id ORDER BY {_COMMENT_COUNT} < 0, {_COMMENT_COUNT}, "
                  f"COALESCE(w.create_time, 0) DESC LIMIT :limit",
                {**_full_args(refresh_after, retry_after, grow_min), "limit": int(limit)})


def next_full(db: sqlite3.Connection, *, refresh_after: int, retry_after: int, grow_min: int
              ) -> Optional[Dict[str, Any]]:
    found = full_list(db, refresh_after=refresh_after, retry_after=retry_after,
                      grow_min=grow_min, limit=1)
    return found[0] if found else None


def full_count(db: sqlite3.Connection, *, refresh_after: int, retry_after: int,
               grow_min: int) -> int:
    return int(scalar(db, _full_sql("COUNT(DISTINCT d.aweme_id)"),
                      _full_args(refresh_after, retry_after, grow_min)) or 0)


def claim_full(db: sqlite3.Connection, aweme_id: str) -> Optional[int]:
    """Start (or resume) reading all of one work's comments; returns the cursor.

    A finished work read again (its comment count grew) starts over from
    the first page; what is saved stays and is merged with what is read.
    """
    now = _now()
    row = one(db, "SELECT * FROM comment_sync WHERE aweme_id = ?", (aweme_id,))
    if not row or (row["status"] == "running" and now - int(row["last_attempt_at"] or 0)
                   < CLAIM_TIMEOUT):
        return None
    if row["full_done"]:
        db.execute("UPDATE comment_sync SET full_done = 0, full_cursor = 0 WHERE aweme_id = ?",
                   (aweme_id,))
        row["full_cursor"] = 0
    db.execute("UPDATE comment_sync SET status = 'running', last_attempt_at = ? "
               "WHERE aweme_id = ?", (now, aweme_id))
    return int(row["full_cursor"] or 0)


def full_progress(db: sqlite3.Connection, aweme_id: str, cursor: int) -> None:
    """A page of the second pass is saved: resume after it next time."""
    db.execute("UPDATE comment_sync SET full_cursor = ?, full_fails = 0 WHERE aweme_id = ?",
               (int(cursor), aweme_id))


def finish_full(db: sqlite3.Connection, aweme_id: str, *, done: bool, error: str = "",
                give_up_after: int = 3) -> None:
    """End one slice of the second pass.

    ``done``: the last page was read (or, with ``error``, Douyin kept failing
    at the same page ``give_up_after`` times: what was read is kept).
    Otherwise the slice stopped early and the work is resumed later.
    """
    now = _now()
    if error:
        db.execute("UPDATE comment_sync SET full_fails = full_fails + 1, full_failed_at = ?, "
                   "full_error = ? WHERE aweme_id = ?", (now, str(error)[:500], aweme_id))
        fails = int(scalar(db, "SELECT full_fails FROM comment_sync WHERE aweme_id = ?",
                           (aweme_id,)) or 0)
        done = fails >= give_up_after
    if done:
        db.execute(f"UPDATE comment_sync SET full_done = 1, full_at = ?, full_fails = 0, "
                   f"full_count = COALESCE((SELECT {_COMMENT_COUNT} FROM works w "
                   f"WHERE w.aweme_id = ?), -1) WHERE aweme_id = ?", (now, aweme_id, aweme_id))
        if not error:
            db.execute("UPDATE comment_sync SET full_error = '' WHERE aweme_id = ?", (aweme_id,))
    db.execute("UPDATE comment_sync SET status = 'done' WHERE aweme_id = ? AND status = 'running'",
               (aweme_id,))


def saved_count(db: sqlite3.Connection, aweme_id: str) -> int:
    return int(scalar(db, "SELECT COUNT(*) FROM comments WHERE aweme_id = ?", (aweme_id,)) or 0)


def pause_state(db: sqlite3.Connection) -> Dict[str, int]:
    """The comment sync's own risk pause (survives a restart)."""
    return {"until": int(meta(db, "comment_pause_until", "0") or 0),
            "level": int(meta(db, "comment_pause_level", "0") or 0)}


def set_pause(db: sqlite3.Connection, until: int, level: int) -> None:
    set_meta(db, "comment_pause_until", int(until))
    set_meta(db, "comment_pause_level", int(level))
