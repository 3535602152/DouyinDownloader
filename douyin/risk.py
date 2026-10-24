"""Request pacing and anti-bot backoff shared by every Douyin API call.

One :class:`RiskGate` belongs to one client.  It enforces

* a global request budget (``rate`` requests per second over all endpoints),
* a minimum spacing for the hottest endpoints, and
* an exponentially growing cooldown for an endpoint that just produced a
  risk signal (HTTP 403/429, verification page, empty body…), which decays
  again with successful responses.

Risk signals and API diagnostics are also reported as small, redacted event
dictionaries so the application can count them (``风控统计``).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from common.log import get_logger

logger = get_logger(__name__)

RiskListener = Callable[[Dict[str, Any]], None]

# reason -> (event type, http status, description)
_RISK_EVENTS = {
    "http-429": ("http_429", 429, "Request rate limited"),
    "http-403": ("http_403", 403, "Douyin API returned HTTP 403"),
    "signature-rejected": ("signature_rejected", 403, "Douyin rejected the request signature"),
    "verify-page": ("verify_ticket", None, "Verification challenge returned"),
    "empty-200": ("empty_200", 200, "Empty HTTP 200 response"),
    "ac-challenge": ("ac_challenge", 200, "Anti-bot __ac challenge page instead of JSON"),
    "non-json-200": ("non_json_200", 200, "Non-JSON HTTP 200 response"),
    "captcha": ("captcha", None, "Browser verification challenge detected"),
}


def _safe_path(path: str) -> str:
    raw = str(path or "")
    try:
        raw = urlsplit(raw).path
    except ValueError:
        pass
    return raw.split("?", 1)[0].split("#", 1)[0][:300]


def _safe_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    text = re.sub(r"(https?://[^?\s]+)\?\S+", r"\1?[redacted-query]", text)
    text = re.sub(r"(?i)\b(cookie|sessionid|mstoken|token)=([^;\s]+)", r"\1=[redacted]", text)
    return text[:200]


class RiskGate:
    BASE_COOLDOWN = 2.0
    MAX_COOLDOWN = 60.0
    FORGET_AFTER = 600.0
    PATH_INTERVAL = {
        "/aweme/v1/web/aweme/detail/": 0.3,
        "/aweme/v1/web/aweme/post/": 0.5,
    }

    def __init__(self, rate: float = 2.0, listener: Optional[RiskListener] = None):
        self._lock = threading.Lock()
        self.listener = listener
        self.set_rate(rate)
        self._hits: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._next_slot: Dict[str, float] = {}
        self._last_risk: Dict[str, float] = {}
        self._global_next = 0.0
        # Called with ("risk", path, cooldown), ("ok", path, 0) or
        # ("login", path, 0); the scheduler pauses background work on them.
        self.observers: List[Callable[[str, str, float], None]] = []

    def _notify(self, what: str, path: str, value: float = 0.0) -> None:
        for observer in list(self.observers):
            try:
                observer(what, self._key(path), value)
            except Exception:
                logger.debug("risk observer failed", exc_info=True)

    def set_rate(self, rate: float) -> None:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            value = 2.0
        self._global_interval = 1.0 / value if value > 0 else 0.0

    @staticmethod
    def _key(path: str) -> str:
        return str(path or "").split("?", 1)[0]

    def reserve(self, path: str) -> float:
        """Book the next slot for ``path``; returns the seconds to wait first.

        The global spacing is booked from now, not from the path's own
        cooldown: a path waiting out a risk cooldown must not hold every
        other path back for that long.
        """
        key = self._key(path)
        with self._lock:
            now = time.monotonic()
            turn = max(now, self._global_next)
            self._global_next = turn + self._global_interval
            start = max(turn, self._cooldown_until.get(key, 0.0))
            interval = self.PATH_INTERVAL.get(key)
            if interval:
                start = max(start, self._next_slot.get(key, 0.0))
                self._next_slot[key] = start + interval
            return max(0.0, start - now)

    def cooldown_remaining(self, path: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until.get(self._key(path), 0.0) - time.monotonic())

    def note_risk(self, path: str, reason: str, detail: str = "") -> float:
        key = self._key(path)
        with self._lock:
            now = time.monotonic()
            if now - self._last_risk.get(key, now) > self.FORGET_AFTER:
                self._hits[key] = 0
            self._last_risk[key] = now
            hits = self._hits.get(key, 0) + 1
            self._hits[key] = hits
            cooldown = min(self.MAX_COOLDOWN, self.BASE_COOLDOWN * (2 ** (hits - 1)))
            self._cooldown_until[key] = max(self._cooldown_until.get(key, 0.0), now + cooldown)
        logger.warning("Douyin risk signal: path=%s reason=%s hits=%d cooldown=%.1fs %s",
                       key, reason, hits, cooldown, detail)
        event = _RISK_EVENTS.get(reason)
        if event:
            self.report(event[0], "risk", path, http_status=event[1],
                        reason=f"{event[2]} ({detail})" if detail else event[2])
        self._notify("risk", path, cooldown)
        return cooldown

    def note_ok(self, path: str) -> None:
        key = self._key(path)
        with self._lock:
            if self._hits.get(key):
                self._hits[key] -= 1
        self._notify("ok", path)

    def report(self, event_type: str, category: str, path: str, *,
               http_status: Optional[int] = None, api_status: Optional[int] = None,
               reason: str = "", notify: bool = True) -> None:
        if event_type == "login_required" and notify:
            self._notify("login", path)
        listener = self.listener
        if listener is None:
            return
        event = {
            "timestamp": int(time.time()),
            "type": str(event_type or "unknown")[:80],
            "category": "diagnostic" if category == "diagnostic" else "risk",
            "path": _safe_path(path),
            "http_status": http_status,
            "api_status": api_status,
            "reason": _safe_reason(reason),
        }
        try:
            listener(event)
        except Exception:
            logger.exception("risk listener failed")
