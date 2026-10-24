"""Cookie parsing and login-state checks."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import unquote

_INVALID_NAME_CHARS = set('()<>@,;:\\"/[]?={} \t\r\n')
SESSION_KEYS = ("sessionid", "sessionid_ss")


def valid_name(name: str) -> bool:
    return bool(name) and isinstance(name, str) and all(
        33 <= ord(ch) <= 126 and ch not in _INVALID_NAME_CHARS for ch in name
    )


def sanitize(cookies: Optional[Mapping[Any, Any]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in (cookies or {}).items():
        if isinstance(key, str) and valid_name(key.strip()):
            result[key.strip()] = "" if value is None else str(value).strip()
    return result


def parse_header(header: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for part in str(header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if valid_name(key.strip()):
            parsed[key.strip()] = value.strip()
    return parsed


def to_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in (cookies or {}).items() if value)


def is_logged_in(cookies: Optional[Mapping[Any, Any]]) -> bool:
    """A real login session, not just the device cookies of a guest visit."""
    values = sanitize(cookies)
    if any(values.get(key) for key in SESSION_KEYS):
        return True
    return bool(values.get("sid_guard") and (values.get("uid_tt") or values.get("sid_tt")))


def login_state(cookies: Optional[Mapping[Any, Any]]) -> str:
    """``ok`` / ``invalid`` (cookies without a session) / ``empty``."""
    values = sanitize(cookies)
    if is_logged_in(values):
        return "ok"
    return "invalid" if any(values.values()) else "empty"


def ms_token_in(text: str) -> Optional[str]:
    """Find an ``msToken`` value in a URL, cookie header or JSON text."""
    for pattern in (
        r"(?:^|[;,&\s\"'])msToken=([^;,&\s\"']+)",
        r'"msToken"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, str(text or ""))
        if match and match.group(1).strip():
            return unquote(match.group(1).strip())
    return None
