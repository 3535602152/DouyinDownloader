"""The one channel from the backend to the UI.

Services publish plain JSON-safe dictionaries (``{"type": …, …}``); the
desktop bridge drains them on every poll.  Publishing never blocks and is
safe from any thread.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class EventBus:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._throttle: Dict[str, float] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._intervals: Dict[str, float] = {}
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, **payload}
        self._queue.put(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def log(self, text: str) -> None:
        """A line for the log drawer."""
        text = str(text or "")
        self.emit("log", text=text if text.endswith("\n") else text + "\n")

    def activity(self, key: str, text: str, *, detail: str = "", running: bool = True) -> None:
        """One step of a user-started job for the footer's left lane.

        ``running=False`` marks the final state (shown briefly); an empty
        ``text`` removes the entry.
        """
        self.emit("activity", key=str(key), text=str(text or ""), detail=str(detail or ""),
                  running=bool(running))

    def error(self, scope: str, text: str) -> None:
        self.emit("error", scope=scope, text=str(text or "操作失败"))

    def emit_latest(self, key: str, interval: float, event_type: str, **payload: Any) -> None:
        """Publish at most once per ``interval`` seconds; the newest value wins."""
        now = time.monotonic()
        with self._lock:
            self._intervals[key] = interval
            due = now - self._throttle.get(key, 0.0) >= interval
            if due:
                self._throttle[key] = now
                self._pending.pop(key, None)
            else:
                self._pending[key] = {"type": event_type, **payload}
        if due:
            self.emit(event_type, **payload)

    def flush_latest(self) -> None:
        """Deliver throttled events whose interval has passed."""
        now = time.monotonic()
        ready = []
        with self._lock:
            for key, event in list(self._pending.items()):
                if now - self._throttle.get(key, 0.0) >= self._intervals.get(key, 0.5):
                    self._throttle[key] = now
                    ready.append(self._pending.pop(key))
        for event in ready:
            self._queue.put(event)

    def listen(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def drain(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        self.flush_latest()
        events: List[Dict[str, Any]] = []
        while limit is None or len(events) < limit:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events
