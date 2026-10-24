"""Manual full-scan batches (the footer's 全量扫描 progress).

A batch lists the sources to scan and collects the works it queued; its
download progress is read from the shared ledger, so a work downloaded by
any path counts as done for the batch too.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from storage.db import chunks, marks, one, rows, scalar


def _now() -> int:
    return int(time.time())


def _bump(db: sqlite3.Connection, batch_id: int) -> None:
    db.execute("UPDATE scan_batches SET revision = revision + 1 WHERE id = ?", (batch_id,))


def active(db: sqlite3.Connection) -> Optional[int]:
    row = one(db, "SELECT id FROM scan_batches WHERE status = 'scanning' ORDER BY id DESC LIMIT 1")
    return int(row["id"]) if row else None


def begin(db: sqlite3.Connection, source_ids: Sequence[int]) -> Tuple[int, List[int]]:
    """Start a batch (or extend the running one); returns ``(batch, newly added)``."""
    batch_id = active(db)
    if batch_id is None:
        batch_id = int(db.execute("INSERT INTO scan_batches (created_at) VALUES (?)",
                                  (_now(),)).lastrowid)
    added = []
    for source_id in dict.fromkeys(int(value) for value in source_ids):
        state = scalar(db, "SELECT state FROM scan_batch_sources WHERE batch_id = ? "
                           "AND source_id = ?", (batch_id, source_id), None)
        if state in ("pending", "scanning"):
            continue
        db.execute("INSERT OR REPLACE INTO scan_batch_sources (batch_id, source_id, state) "
                   "VALUES (?, ?, 'pending')", (batch_id, source_id))
        added.append(source_id)
    _bump(db, batch_id)
    return batch_id, added


def start_source(db: sqlite3.Connection, batch_id: int, source_id: int) -> None:
    db.execute("UPDATE scan_batch_sources SET state = 'scanning', scanned = 0 "
               "WHERE batch_id = ? AND source_id = ?", (batch_id, source_id))
    _bump(db, batch_id)


def progress(db: sqlite3.Connection, batch_id: int, source_id: int, scanned: int) -> None:
    db.execute("UPDATE scan_batch_sources SET scanned = ? WHERE batch_id = ? AND source_id = ?",
               (int(scanned), batch_id, source_id))
    _bump(db, batch_id)


def add_works(db: sqlite3.Connection, batch_id: int, aweme_ids: Sequence[str]) -> None:
    for aweme_id in aweme_ids:
        db.execute("INSERT OR IGNORE INTO scan_batch_works (batch_id, aweme_id) VALUES (?, ?)",
                   (batch_id, str(aweme_id)))
    _bump(db, batch_id)


def finish_source(db: sqlite3.Connection, batch_id: int, source_id: int, error: str = "") -> None:
    db.execute("UPDATE scan_batch_sources SET state = ?, error = ? WHERE batch_id = ? "
               "AND source_id = ? AND state IN ('pending', 'scanning')",
               ("failed" if error else "success", error, batch_id, source_id))
    left = scalar(db, "SELECT COUNT(*) FROM scan_batch_sources WHERE batch_id = ? "
                      "AND state IN ('pending', 'scanning')", (batch_id,))
    if not left:
        db.execute("UPDATE scan_batches SET status = 'done' WHERE id = ? AND status = 'scanning'",
                   (batch_id,))
    _bump(db, batch_id)


def interrupt(db: sqlite3.Connection, batch_id: int, reason: str) -> None:
    db.execute("UPDATE scan_batch_sources SET state = 'failed', error = ? WHERE batch_id = ? "
               "AND state IN ('pending', 'scanning')", (reason, batch_id))
    db.execute("UPDATE scan_batches SET status = 'interrupted', error = ? WHERE id = ? "
               "AND status = 'scanning'", (reason, batch_id))
    _bump(db, batch_id)


def interrupt_stale(db: sqlite3.Connection, reason: str) -> None:
    """A batch left scanning by the previous run can never finish; close it
    so the next full scan starts a new batch (each source keeps its cursor)."""
    batch_id = active(db)
    if batch_id is not None:
        interrupt(db, batch_id, reason)


def snapshot(db: sqlite3.Connection, batch_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The footer's view of a batch (the latest one by default)."""
    batch = one(db, "SELECT * FROM scan_batches WHERE id = ?", (batch_id,)) if batch_id \
        else one(db, "SELECT * FROM scan_batches ORDER BY id DESC LIMIT 1")
    if batch is None:
        return None
    members = rows(db, "SELECT b.state, b.scanned, b.error, s.nickname, s.sec_uid "
                       "FROM scan_batch_sources b LEFT JOIN sources s ON s.id = b.source_id "
                       "WHERE b.batch_id = ?", (batch["id"],))
    counts = one(db, "SELECT COUNT(*) AS total, "
                     "SUM(CASE WHEN d.status = 'done' OR d.aweme_id IS NULL THEN 1 ELSE 0 END) "
                     "AS success, SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed "
                     "FROM scan_batch_works w LEFT JOIN downloads d ON d.aweme_id = w.aweme_id "
                     "WHERE w.batch_id = ?", (batch["id"],)) or {}
    scanning = next((member for member in members if member["state"] == "scanning"), None)
    scan_done = sum(1 for member in members if member["state"] in ("success", "failed"))
    success, failed = int(counts.get("success") or 0), int(counts.get("failed") or 0)
    total = int(counts.get("total") or 0)
    status = str(batch["status"])
    if status == "done":
        status = "completed" if success + failed >= total else "downloading"
    return {
        "task_id": int(batch["id"]),
        "revision": int(batch["revision"]),
        "status": status,
        "error": str(batch["error"] or ""),
        "scan_total": len(members),
        "scan_done": scan_done,
        "scan_failed": sum(1 for member in members if member["state"] == "failed"),
        "download_total": total,
        "download_done": success + failed,
        "download_success": success,
        "download_failed": failed,
        "catalogue_complete": scan_done == len(members),
        "current_creator_name": str((scanning or {}).get("nickname")
                                    or (scanning or {}).get("sec_uid") or ""),
        "current_scanned": int((scanning or {}).get("scanned") or 0),
    }


def forget_sources(db: sqlite3.Connection, source_ids: Sequence[int]) -> None:
    for batch in chunks([int(value) for value in source_ids]):
        db.execute(f"UPDATE scan_batch_sources SET state = 'failed', error = '已删除' "
                   f"WHERE source_id IN ({marks(batch)}) AND state IN ('pending', 'scanning')",
                   batch)
