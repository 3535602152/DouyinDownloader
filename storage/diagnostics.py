"""Risk-control and API diagnostic events (the 风控统计 panel)."""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Dict, Mapping, Optional

from storage.db import rows


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    if text.lstrip().startswith(("{", "[", "<")):
        return "[response details omitted]"
    text = re.sub(r"https?://\S+", "[url]", text, flags=re.IGNORECASE)
    if re.search(r"(?i)\b(cookie|authorization|token|mstoken|verify_ticket|a_bogus|"
                 r"odin_tt|sessionid|sid_guard|signature)\b\s*[:=]", text):
        return "[sensitive details omitted]"
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _status(value: Any, *, http: bool) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if http and not 100 <= number <= 599:
        return None
    return number if number >= 0 else None


def record(db: sqlite3.Connection, event: Mapping[str, Any]) -> None:
    category = "diagnostic" if event.get("category") == "diagnostic" else "risk"
    kind = re.sub(r"[^a-z0-9_.:-]", "_", str(event.get("type") or "unknown").lower())[:80]
    path = re.sub(r"[^a-zA-Z0-9._~/-]", "",
                  str(event.get("path") or "").split("?", 1)[0])[:256]
    try:
        stamp = int(float(event.get("timestamp") or time.time()))
    except (TypeError, ValueError):
        stamp = int(time.time())
    db.execute("INSERT INTO risk_events (timestamp, type, category, path, http_status, "
               "api_status, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (stamp, kind or "unknown", category, path,
                _status(event.get("http_status"), http=True),
                _status(event.get("api_status"), http=False), _text(event.get("reason"), 240)))


def report(db: sqlite3.Connection, window_seconds: int = 86400, limit: int = 100
           ) -> Dict[str, Any]:
    cutoff = int(time.time()) - int(window_seconds)
    by_type = [
        {"type": row["type"], "category": row["category"], "count_24h": int(row["recent"] or 0),
         "count_total": int(row["total"] or 0), "last_seen": row["last_seen"]}
        for row in rows(db, "SELECT type, category, SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 "
                            "END) AS recent, COUNT(*) AS total, MAX(timestamp) AS last_seen "
                            "FROM risk_events GROUP BY type, category ORDER BY category, type",
                        (cutoff,))
    ]

    def total(category: str, field: str) -> int:
        return sum(item[field] for item in by_type if item["category"] == category)

    return {
        "risk_24h": total("risk", "count_24h"),
        "risk_total": total("risk", "count_total"),
        "diagnostics_24h": total("diagnostic", "count_24h"),
        "diagnostics_total": total("diagnostic", "count_total"),
        "by_type": by_type,
        "recent_events": rows(db, "SELECT timestamp, type, category, path, http_status, "
                                  "api_status, reason FROM risk_events "
                                  "ORDER BY timestamp DESC, id DESC LIMIT ?", (int(limit),)),
    }
