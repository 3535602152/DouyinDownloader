"""The last online profile of each creator, so a homepage still shows it offline.

``payload`` is the homepage header as built by ``services.views.creator_payload``;
the avatar and cover are kept as local files next to it.  ``*_key`` is the
image URL without its signature query, which changes on every request while
the picture behind it does not.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Mapping, Optional

IMAGE_KINDS = ("avatar", "cover")


def get(connection: sqlite3.Connection, sec_uid: str) -> Optional[Dict[str, Any]]:
    row = connection.execute("SELECT * FROM creator_profiles WHERE sec_uid = ?",
                             (sec_uid,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        payload = json.loads(result.get("payload") or "{}")
    except ValueError:
        payload = {}
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def save_payload(connection: sqlite3.Connection, sec_uid: str,
                 payload: Mapping[str, Any]) -> bool:
    """Store the profile when it differs from the cached one; True if it changed."""
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    row = connection.execute("SELECT payload FROM creator_profiles WHERE sec_uid = ?",
                             (sec_uid,)).fetchone()
    if row is not None and row[0] == text:
        return False
    connection.execute(
        "INSERT INTO creator_profiles (sec_uid, payload, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(sec_uid) DO UPDATE SET payload = excluded.payload, "
        "updated_at = excluded.updated_at",
        (sec_uid, text, int(time.time())))
    return True


def save_image(connection: sqlite3.Connection, sec_uid: str, kind: str, key: str,
               path: str) -> None:
    if kind not in IMAGE_KINDS:
        raise ValueError(kind)
    connection.execute(
        "INSERT INTO creator_profiles (sec_uid, payload, updated_at) VALUES (?, '{}', ?) "
        "ON CONFLICT(sec_uid) DO NOTHING", (sec_uid, int(time.time())))
    connection.execute(f"UPDATE creator_profiles SET {kind}_key = ?, {kind}_file = ? "
                       "WHERE sec_uid = ?", (key, path, sec_uid))
