"""The user's settings, UI state and login, in one place.

Files in the data directory, each with one purpose:

``config.yml``             download and monitoring preferences (hand-editable)
``desktop_settings.json``  UI state: theme, remembered account, resume checkpoint
``.accounts.json``         the account library: every logged-in Douyin account
``.cookies.json``          the main account's login (kept for older versions)

The account library holds several logins; one of them is the *main*
account, used for everything that does not belong to a particular account.
An account whose ``sec_uid`` is not known yet (a login migrated from
``.cookies.json``, or cookies pasted by hand) has ``sec_uid == ""`` until
the app reads its profile.

Unknown keys in ``config.yml`` are preserved on save.  Listeners are told
after every change so running services can adapt (rate, quality, …).
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from common.fs import atomic_write_json, atomic_write_text, read_json
from common.log import get_logger
from douyin import cookies as cookie_tools
from storage.preferences import (
    DEFAULT_DOWNLOAD_DIR,
    FULL_SCAN_INTERVALS,
    QUALITY_LABELS,
    config_defaults,
)
from storage.preferences import (
    QUALITY_VALUES as QUALITY_VALUES,
)

logger = get_logger(__name__)

USER_MODES = ("post", "like", "mix", "music", "collect", "collectmix")

DEFAULTS: Dict[str, Any] = {
    **config_defaults(),
    "rate_limit": 2,
    "homepage_screenshot": True,
    # Transport tuning stays separate from the user-facing preference schema.
    "monitor_connections": 24,
    "monitor_segments": 8,
    "database_path": "DouyinDownloader.db",
    "comments": {"enabled": False, "page_size": 20},
    "mode": ["post"],
    "number": {mode: 0 for mode in USER_MODES},
    "increase": {mode: mode not in ("collect", "collectmix") for mode in USER_MODES},
    "link": [],
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _account_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    cookies = raw.get("cookies")
    return {"sec_uid": str(raw.get("sec_uid") or ""), "uid": str(raw.get("uid") or ""),
            "nickname": str(raw.get("nickname") or ""),
            "cookies": cookie_tools.sanitize(cookies if isinstance(cookies, dict) else {}),
            "added_at": _int(raw.get("added_at"), 0), "expired": bool(raw.get("expired"))}


def normalize_scan_interval(value: Any) -> int:
    hours = _int(value, 24)
    return hours if hours in FULL_SCAN_INTERVALS else 24


class Settings:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "config.yml"
        self.ui_path = self.data_dir / "desktop_settings.json"
        self.cookie_path = self.data_dir / ".cookies.json"
        self.accounts_path = self.data_dir / ".accounts.json"
        self._lock = threading.RLock()
        self._listeners: List[Callable[[], None]] = []
        self._config = self._load_config()
        self._main, self._accounts = self._load_accounts()

    # ── loading ─────────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, Any]:
        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("config.yml unreadable, using defaults: %s", exc)
            raw = {}
        config = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
        # A publish-date range is chosen per download and never persisted.
        config.pop("start_time", None)
        config.pop("end_time", None)
        return config

    def _load_cookies(self) -> Dict[str, str]:
        data = read_json(self.cookie_path, None)
        if isinstance(data, dict) and data:
            return cookie_tools.sanitize(data)
        legacy = str(self._config.get("cookie") or "").strip()
        return cookie_tools.sanitize(cookie_tools.parse_header(legacy)) if legacy else {}

    def _load_accounts(self) -> Tuple[str, List[Dict[str, Any]]]:
        data = read_json(self.accounts_path, None)
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            accounts, seen = [], set()
            for raw in data["accounts"]:
                if not isinstance(raw, dict):
                    continue
                entry = _account_entry(raw)
                if entry["sec_uid"] in seen or not entry["cookies"]:
                    continue
                seen.add(entry["sec_uid"])
                accounts.append(entry)
            main = str(data.get("main") or "")
            if main not in seen:
                main = accounts[0]["sec_uid"] if accounts else ""
            return main, accounts
        cookies = self._load_cookies()
        if not any(cookies.values()):
            return "", []
        return "", [_account_entry({"cookies": cookies})]

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                logger.exception("settings listener failed")

    # ── config ──────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self._config.get(key, default))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._config)

    def update(self, **values: Any) -> None:
        """Merge ``values`` into config.yml and persist it."""
        with self._lock:
            self._config = _merge(self._config, values)
            saveable = {key: value for key, value in self._config.items()
                        if key not in ("cookie", "cookies")}
            atomic_write_text(self.config_path, yaml.safe_dump(
                saveable, allow_unicode=True, sort_keys=False, width=100000))
        self._changed()

    @property
    def download_root(self) -> Path:
        raw = str(self.get("path") or DEFAULT_DOWNLOAD_DIR).strip() or DEFAULT_DOWNLOAD_DIR
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self.data_dir / path

    @property
    def database_path(self) -> Path:
        path = Path(str(self.get("database_path") or "DouyinDownloader.db")).expanduser()
        return path if path.is_absolute() else self.data_dir / path

    @property
    def quality(self) -> str:
        value = str(self.get("video_quality") or "highest").strip().lower()
        return value if value in QUALITY_LABELS else "highest"

    @property
    def scan_fast(self) -> bool:
        return str(self.get("scan_strategy") or "stable") == "fast"

    @property
    def full_scan_interval(self) -> int:
        return normalize_scan_interval(self.get("full_scan_interval"))

    @property
    def work_concurrency(self) -> int:
        return min(3, max(1, _int(self.get("monitor_work_concurrency"), 1)))

    @property
    def connections(self) -> int:
        return min(64, max(2, _int(self.get("monitor_connections"), 24)))

    @property
    def segments(self) -> int:
        return min(32, max(1, _int(self.get("monitor_segments"), 8)))

    @property
    def rate_limit(self) -> float:
        try:
            return max(0.2, float(self.get("rate_limit") or 2))
        except (TypeError, ValueError):
            return 2.0

    @property
    def proxy(self) -> str:
        return str(self.get("proxy") or "").strip()

    @property
    def browser_fallback(self) -> bool:
        value = self.get("browser_fallback") or {}
        return bool(value.get("enabled", True)) if isinstance(value, dict) else bool(value)

    @property
    def comments_page_size(self) -> int:
        value = self.get("comments") or {}
        size = _int(value.get("page_size"), 20) if isinstance(value, dict) else 20
        return min(100, max(1, size))

    # ── accounts ────────────────────────────────────────────────────

    def _find(self, sec_uid: str) -> Optional[Dict[str, Any]]:
        return next((entry for entry in self._accounts if entry["sec_uid"] == sec_uid), None)

    def _save_accounts(self) -> None:
        atomic_write_json(self.accounts_path, {"main": self._main, "accounts": self._accounts})
        main = self._find(self._main)
        if main is not None:
            atomic_write_json(self.cookie_path, main["cookies"])
        else:
            try:
                self.cookie_path.unlink(missing_ok=True)
            except OSError:
                pass

    def accounts(self) -> List[Dict[str, Any]]:
        """Every account of the library, the main one first."""
        with self._lock:
            entries = deepcopy(self._accounts)
            main = self._main
        return sorted(entries, key=lambda entry: entry["sec_uid"] != main)

    def account(self, sec_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(str(sec_uid or ""))
            return deepcopy(entry) if entry is not None else None

    @property
    def main_account(self) -> str:
        """The main account's sec_uid (``""`` when unknown or not logged in)."""
        with self._lock:
            return self._main

    def save_account(self, sec_uid: str, cookies: Optional[Dict[str, str]] = None, *,
                     main: bool = False, **fields: Any) -> None:
        """Add an account or update one; new cookies also clear an expired mark.

        The first account becomes the main one.
        """
        sec_uid = str(sec_uid or "")
        with self._lock:
            entry = self._find(sec_uid)
            if entry is None:
                entry = _account_entry({"sec_uid": sec_uid, "added_at": int(time.time())})
                self._accounts.append(entry)
            if cookies is not None:
                entry["cookies"] = cookie_tools.sanitize(cookies)
                entry["expired"] = False
            for key in ("uid", "nickname"):
                if key in fields:
                    entry[key] = str(fields[key] or "")
            if "expired" in fields:
                entry["expired"] = bool(fields["expired"])
            if main or self._find(self._main) is None:
                self._main = sec_uid
            self._save_accounts()
        self._changed()

    def name_pending_account(self, sec_uid: str, **fields: Any) -> None:
        """Give the pending account (``sec_uid == ""``) its sec_uid.

        When that account is in the library already, the pending login
        replaces the older one.
        """
        sec_uid = str(sec_uid or "")
        with self._lock:
            pending = self._find("")
            if pending is None or not sec_uid:
                return
            self._accounts.remove(pending)
            existing = self._find(sec_uid)
            if existing is not None:
                existing.update(cookies=pending["cookies"], expired=False)
            else:
                pending["sec_uid"] = sec_uid
                self._accounts.append(pending)
            target = self._find(sec_uid)
            for key in ("uid", "nickname"):
                if key in fields:
                    target[key] = str(fields[key] or "")
            if self._main == "":
                self._main = sec_uid
            self._save_accounts()
        self._changed()

    def remove_account(self, sec_uid: str) -> bool:
        with self._lock:
            entry = self._find(str(sec_uid or ""))
            if entry is None:
                return False
            self._accounts.remove(entry)
            if self._main == entry["sec_uid"]:
                self._main = self._accounts[0]["sec_uid"] if self._accounts else ""
            self._save_accounts()
        self._changed()
        return True

    def set_main_account(self, sec_uid: str) -> bool:
        with self._lock:
            if self._find(str(sec_uid or "")) is None:
                return False
            self._main = str(sec_uid or "")
            self._save_accounts()
        self._changed()
        return True

    # ── cookies ─────────────────────────────────────────────────────

    @property
    def cookies(self) -> Dict[str, str]:
        """The main account's login."""
        with self._lock:
            entry = self._find(self._main)
            return dict(entry["cookies"]) if entry is not None else {}

    def set_cookies(self, cookies: Optional[Dict[str, str]]) -> None:
        """Replace the main account's login (``{}`` removes the main account).

        Whose cookies these are is not known here: the main account becomes
        pending (``sec_uid == ""``) until the app reads its profile.
        """
        values = cookie_tools.sanitize(cookies or {})
        with self._lock:
            entry = self._find(self._main)
            if entry is not None and entry["cookies"] == values:
                return
            for stale in (entry, self._find("")):
                if stale is not None and stale in self._accounts:
                    self._accounts.remove(stale)
            if values:
                self._accounts.insert(0, _account_entry({"cookies": values,
                                                         "added_at": int(time.time())}))
                self._main = ""
            else:
                self._main = self._accounts[0]["sec_uid"] if self._accounts else ""
            self._save_accounts()
        self._changed()

    @property
    def logged_in(self) -> bool:
        return cookie_tools.is_logged_in(self.cookies)

    # ── UI state ────────────────────────────────────────────────────

    def ui(self) -> Dict[str, Any]:
        with self._lock:
            data = read_json(self.ui_path, {})
            return data if isinstance(data, dict) else {}

    def update_ui(self, **values: Any) -> None:
        with self._lock:
            data = self.ui()
            data.update(values)
            atomic_write_json(self.ui_path, data)

    @property
    def theme(self) -> str:
        theme = self.ui().get("theme")
        return theme if theme in ("light", "dark", "system") else "light"
