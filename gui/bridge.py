"""The JavaScript API of the desktop window (``window.pywebview.api``).

A thin adapter: it validates arguments, calls the services on the backend
loop, and turns local paths into URLs the WebView can load.  Method names,
arguments, return values and event shapes are the contract with
``gui/web/app.js``.  Attributes are private so pywebview exposes methods only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.fs import image_data_url
from common.log import get_logger
from douyin import cookies as cookie_tools
from gui import window_frame
from gui.media_server import LocalMediaServer
from services.app import App
from services.common import explain
from services.views import creator_payload
from storage.files import IMAGE_SUFFIXES
from storage.preferences import preference_state, schema, validate_patch
from storage.settings import QUALITY_LABELS, USER_MODES

logger = get_logger(__name__)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WebBridge:
    def __init__(self, data_dir: Path, *, app: Optional[App] = None,
                 media_server: Optional[LocalMediaServer] = None):
        self._app = app or App.create(Path(data_dir))
        self._ctx = self._app.ctx
        self._media = media_server if media_server is not None else LocalMediaServer()
        self._window = None
        self._closed = False

    # ── plumbing ────────────────────────────────────────────────────

    def _call(self, coro, timeout: Optional[float] = 120) -> Any:
        return self._ctx.runtime.call(coro, timeout)

    def _run(self, fn, *args, **kwargs) -> Any:
        """Run a synchronous service method on the backend loop."""
        async def invoke():
            return fn(*args, **kwargs)

        return self._call(invoke(), timeout=30)

    def _source(self, path: str, *, inline_image: bool = True) -> str:
        candidate = Path(str(path or ""))
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                return ""
        except OSError:
            return ""
        if inline_image and candidate.suffix.lower() in IMAGE_SUFFIXES:
            data = image_data_url(candidate)
            if data:
                return data
        return self._media.url(candidate)

    def _attach(self, work: Dict[str, Any], *, include_media: bool) -> Dict[str, Any]:
        """Local paths → loadable sources, keeping online sources as fallback."""
        result = dict(work)
        paths = [path for path in work.get("media_paths") or [] if path]
        local = [self._source(path, inline_image=False) for path in paths] if include_media else []
        remote = list(work.get("media_sources") or [])
        if local and work.get("media_state") == "partial":
            result["media_sources"] = list(dict.fromkeys([*local, *remote]))
        else:
            result["media_sources"] = local or remote
        if local:
            result["live_sources"] = [self._source(path, inline_image=False) if path else ""
                                      for path in work.get("live_paths") or []]
        else:
            result.setdefault("live_sources", [])
        music = self._source(work.get("music_path") or "", inline_image=False) \
            if include_media and work.get("music_path") else ""
        if music:
            result["music_sources"], result["music_source"] = [music], music
        cover = self._source(work.get("cover_path") or "", inline_image=include_media) \
            if work.get("cover_path") else ""
        if cover:
            result["cover_source"], result["cover_media_type"] = cover, "image"
        else:
            result["cover_source"] = str(work.get("cover_source") or "")
        return result

    # ── window ──────────────────────────────────────────────────────

    def get_window_frame(self) -> Dict[str, bool]:
        """Whether the page draws the caption, and if the window is maximized."""
        return window_frame.frame_state(self._window)

    def window_command(self, action: str) -> bool:
        """Caption buttons: ``minimize``, ``maximize`` (toggles) or ``close``."""
        return window_frame.system_command(self._window, str(action or ""))

    def window_start_resize(self, edge: str) -> bool:
        """Top-edge strip of the custom caption: ``top``, ``topleft`` or ``topright``."""
        return window_frame.start_resize(self._window, str(edge or ""))

    def set_titlebar_theme(self, theme: str) -> bool:
        """Match the native Windows caption to the WebView's theme."""
        if os.name != "nt" or self._window is None:
            return False
        try:
            import ctypes

            hwnd = ctypes.c_void_p(int(self._window.native.Handle.ToInt64()))
            setter = ctypes.WinDLL("dwmapi", use_last_error=True).DwmSetWindowAttribute
            setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            setter.restype = ctypes.c_long
            dark = theme == "dark"
            default = 0xFFFFFFFF
            for attribute, value in ((20, ctypes.c_int(int(dark))),
                                     (35, ctypes.c_uint(0x00141414 if dark else default)),
                                     (36, ctypes.c_uint(0x00F0F0F0 if dark else default)),
                                     (34, ctypes.c_uint(0x00181818 if dark else default))):
                if setter(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) < 0:
                    return False
            return True
        except Exception:
            return False

    def choose_directory(self) -> Optional[str]:
        try:
            import webview

            if webview.windows:
                chosen = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
                if chosen:
                    return str(chosen[0])
        except Exception as exc:
            self._ctx.events.error("settings", str(exc))
        return None

    def open_directory(self, path: str) -> bool:
        target = Path(str(path or "") or self._ctx.settings.download_root)
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        except (OSError, AttributeError) as exc:
            self._ctx.events.error("settings", str(exc))
            return False

    # ── state & settings ────────────────────────────────────────────

    def poll(self) -> List[dict]:
        events = self._ctx.events.drain()
        result, refresh = [], False
        for event in events:
            if event.get("type") == "monitor_refresh":
                if refresh:
                    continue
                refresh = True
            result.append(event)
        return result

    def state(self) -> dict:
        settings = self._ctx.settings
        config = settings.snapshot()
        modes = config.get("mode") or ["post"]
        mode = str(modes[0] if isinstance(modes, list) else modes)
        increase = config.get("increase") or {}
        cookies = settings.cookies
        ui = settings.ui()
        return {
            **preference_state(settings),
            "settings_schema": schema(),
            "theme": settings.theme,
            "path": str(settings.download_root),
            "links": list(config.get("link") or []),
            "mode": mode,
            "limit": _int((config.get("number") or {}).get(mode)),
            "increase": bool(increase.get(mode, False)),
            "increase_by_mode": {key: bool(increase.get(key, False))
                                 for key in ("post", "like", "allmix", "mix", "music",
                                             "collect", "collectmix")},
            "last_user_account": ui.get("last_user_account"),
            # An unfinished user download is resumed by the backend at start-up;
            # the page no longer asks about it.
            "pending_user_download": None,
            "quality": QUALITY_LABELS.get(settings.quality, "最高"),
            "monitor_works": settings.work_concurrency,
            "full_scan_interval": settings.full_scan_interval,
            "scan_strategy": "fast" if settings.scan_fast else "stable",
            "browser_fallback": settings.browser_fallback,
            "start_time": "", "end_time": "",
            "download_pinned": True, "homepage_screenshot": True,
            "cookie_configured": cookie_tools.is_logged_in(cookies),
            "cookie_invalid": cookie_tools.login_state(cookies) == "invalid",
            "cookie_count": len(cookies),
            "account_profile": self._app.account.profile(),
            "accounts": self._app.account.accounts(),
            "tasks": self._run(self._ctx.tasks.states),
        }

    def set_theme(self, theme: str) -> bool:
        theme = str(theme or "").strip().lower()
        if theme not in ("light", "dark", "system"):
            return False
        self._ctx.settings.update_ui(theme=theme)
        return True

    def save_settings(self, values: dict) -> dict:
        values = dict(values or {})
        settings = self._ctx.settings
        config_patch, ui_patch = validate_patch(values)
        cookie_text = str(values.get("cookie") or "").strip()
        new_cookies = None
        if cookie_text:
            new_cookies = cookie_tools.sanitize(cookie_tools.parse_header(cookie_text))
            if cookie_tools.login_state(new_cookies) != "ok":
                raise ValueError("Cookie 无效：缺少 sessionid 或 sessionid_ss")
        if config_patch:
            settings.update(**config_patch)
        if ui_patch:
            settings.update_ui(**ui_patch)
        if values.get("clear_cookie") or values.get("clearCookie"):
            self._call(self._app.account.apply_cookies({}))
        elif new_cookies is not None:
            self._call(self._app.account.apply_cookies(new_cookies))
            self._run(self._app.account.refresh_profile)
        state = self.state()
        self._ctx.events.emit("settings_saved", state=state)
        return state

    def refresh(self) -> dict:
        if self._app.account.needs_refresh():
            self._run(self._app.account.refresh_profile)
        return self.state()

    def get_risk_report(self) -> dict:
        try:
            return self._call(self._app.library.risk_report())
        except Exception as exc:
            return {"error": f"风控统计读取失败：{explain(exc)}"}

    # ── account ─────────────────────────────────────────────────────

    def refresh_account_profile(self) -> bool:
        return bool(self._run(self._app.account.refresh_profile))

    def start_qr_login(self, in_window: bool = False) -> bool:
        # pywebview uses argument names in generated JS functions. "window"
        # would shadow the global window and break _jsApiCallback before dispatch.
        return bool(self._run(self._app.account.start_qr_login, bool(in_window)))

    def cancel_qr_login(self) -> bool:
        return bool(self._run(self._app.account.cancel_qr_login))

    def accounts(self) -> List[dict]:
        """The account library (main account first)."""
        return self._run(self._app.account.accounts)

    def set_main_account(self, sec_uid: str) -> bool:
        return bool(self._run(self._app.account.set_main, str(sec_uid or "")))

    def remove_account(self, sec_uid: str) -> bool:
        return bool(self._run(self._app.account.remove, str(sec_uid or "")))

    def lock_user_account(self, url: Optional[str] = None, account: str = "") -> dict:
        profile = self._call(self._app.account.lock(None if url is None else str(url),
                                                    str(account or "")))
        public = {key: value for key, value in profile.items() if key != "user_info"}
        creator = creator_payload({}, profile.get("user_info"))
        creator["favoriting_count"] = profile.get("favoriting_count", 0)
        if profile.get("avatar_data"):
            creator["avatar_url"] = profile["avatar_data"]
        public["creator"] = creator
        return public

    def current_account(self) -> dict:
        return self.lock_user_account()

    def unlock_user_account(self) -> bool:
        self._run(self._app.user_content.cancel_browse)
        self._app.account.unlock()
        return True

    def discard_user_download_checkpoint(self) -> bool:
        self._ctx.settings.update_ui(pending_user_download=None)
        return True

    def remember_user_mode(self, mode: str) -> bool:
        return self._app.account.remember_mode(str(mode or "").strip())

    # ── link page ───────────────────────────────────────────────────

    def preview_links(self, values: dict) -> bool:
        links = (values or {}).get("links") or []
        if isinstance(links, str):
            links = links.splitlines()
        try:
            return bool(self._run(self._app.links.preview, [str(link) for link in links]))
        except Exception as exc:
            self._ctx.events.error("link", explain(exc))
            return False

    def start_download(self, values: dict, selected: List[str]) -> bool:
        try:
            return bool(self._run(self._app.links.download, [str(v) for v in selected or []]))
        except Exception as exc:
            self._ctx.events.error("link", explain(exc))
            return False

    def pause(self) -> bool:
        self._run(self._ctx.tasks.pause)
        return True

    def resume(self) -> bool:
        self._run(self._ctx.tasks.resume)
        return True

    def stop(self) -> bool:
        self._ctx.settings.update_ui(pending_user_download=None)

        def stop_foreground() -> None:
            self._ctx.tasks.resume()
            self._ctx.tasks.cancel_where(lambda task: task.kind == "user" and task.scope in (
                "link", "user", "settings") and task.name != "qr-login")

        self._run(stop_foreground)
        return True

    # ── account content ─────────────────────────────────────────────

    def preview_user(self, values: dict) -> bool:
        values = dict(values or {})
        try:
            if not values.get("catalogue"):
                raise ValueError("请先确认账号，再浏览作品或喜欢")
            account = self._app.account.locked or {}
            return bool(self._run(
                self._app.user_content.browse, account, str(values.get("mode") or "like"),
                str(values.get("request_id") or ""), str(values.get("client_id") or ""),
                _int(values.get("request_order"))))
        except Exception as exc:
            self._ctx.events.error("user", explain(exc))
            return False

    def cancel_user_preview(self, request_id: str = "") -> bool:
        return bool(self._run(self._app.user_content.cancel_browse, str(request_id or "")))

    def account_work_detail(self, request_id: str, aweme_id: str, force_remote=False) -> dict:
        content = self._app.user_content
        if request_id != content.request_id:
            return {"error": "主页已切换，请重新打开作品"}
        author = content.catalogue.get(str(aweme_id))
        if not author:
            return {"error": "作品作者信息不完整，请刷新主页后重试"}
        return self.creator_work_detail(author, str(aweme_id), force_remote)

    def start_user_download(self, values: dict, selected: Optional[List[str]] = None) -> bool:
        values = dict(values or {})
        try:
            account = self._app.account.locked
            if not account:
                raise ValueError("请先确认账号")
            mode = str(values.get("mode") or "post")
            if mode not in USER_MODES:
                raise ValueError("不支持的下载模式")
            return bool(self._run(self._app.user_content.download, account, values))
        except Exception as exc:
            self._ctx.events.error("user", explain(exc))
            return False

    # ── creators ────────────────────────────────────────────────────

    def monitors(self) -> Optional[List[dict]]:
        try:
            return self._call(self._app.monitor.rows())
        except Exception as exc:
            self._ctx.events.error("monitor", explain(exc))
            return None

    def _creator_images(self, result: dict) -> dict:
        """Cached avatar / cover files → local URLs (they also work offline)."""
        creator = dict(result.get("creator") or {})
        for kind in ("avatar", "cover"):
            path = creator.pop(f"{kind}_path", "")
            local = self._source(path, inline_image=False) if path else ""
            if local:
                creator[f"{kind}_url"] = local
        return {**result, "creator": creator}

    def creator_profile(self, sec_uid: str) -> dict:
        owner = str(sec_uid or "").strip()
        if not owner:
            return {"creator": {}, "error": "缺少主播 UID"}
        return self._creator_images(self._call(self._app.creators.profile(owner)))

    def creator_home(self, sec_uid: str, refresh: bool = False, cursor: int = 0,
                     offset: int = 0, hidden_only: bool = False) -> dict:
        owner = str(sec_uid or "").strip()
        if not owner:
            return {"creator": {}, "works": [], "has_more": False, "source": "local",
                    "error": "缺少主播 UID"}
        result = self._call(self._app.creators.home(owner, bool(refresh),
                                                    max(0, _int(cursor)), max(0, _int(offset)),
                                                    bool(hidden_only)))
        result["works"] = [self._attach(work, include_media=False) for work in result["works"]]
        return self._creator_images(result)

    def work_comments(self, aweme_id: str, cursor: int = 0, author_sec_uid: str = "",
                      comment_id: str = "", count: int = 0, local: bool = False,
                      author_only: bool = False) -> dict:
        """A page of a work's comments, or of one comment's replies.

        Live by default; ``local`` reads the saved copy and ``author_only``
        the author's saved comments and replies.
        """
        work_id = str(aweme_id or "").strip()
        if not work_id:
            return {"comments": [], "has_more": False, "error": "缺少作品 ID"}
        args = (work_id, max(0, _int(cursor)), str(author_sec_uid or "").strip(),
                str(comment_id or "").strip(), max(0, _int(count)))
        if not (local or author_only):
            return self._call(self._app.comments.browse(*args))
        result = self._call(self._app.comments.saved(*args, author_only=bool(author_only)))
        shown = [reply for comment in result.get("comments") or []
                 for reply in [comment, *(comment.get("author_replies") or [])]]
        for comment in shown:
            sticker = self._source(comment.pop("sticker_path", ""), inline_image=False)
            if sticker:
                comment["sticker"] = sticker
                comment["sticker_mirrors"] = [sticker, *comment.get("sticker_mirrors", [])]
            for image in comment.get("images") or []:
                # Saved picture files first: a hidden work's links may be gone.
                thumb, original = (self._source(path, inline_image=False) if path else ""
                                   for path in image.pop("local_paths", ("", "")))
                image["mirrors"] = list(dict.fromkeys(filter(None, [thumb, original,
                                                                    *image["mirrors"]])))
                image["preview"] = thumb or original or image["preview"]
                image["full"] = original or image["full"]
        return result

    def comment_emojis(self) -> dict:
        """Emoji code → picture URL for comment text."""
        return self._call(self._app.comments.emojis())

    def creator_work_detail(self, sec_uid: str, aweme_id: str, force_remote=False) -> dict:
        owner, work_id = str(sec_uid or "").strip(), str(aweme_id or "").strip()
        if not work_id:
            return {}
        work = self._call(self._app.creators.work_detail(owner, work_id, bool(force_remote)))
        if work.get("error") and not work.get("media_paths") and not work.get("media_sources"):
            work.setdefault("media_sources", [])
            return work
        return self._attach(work, include_media=True)

    def add_monitor(self, raw: str) -> bool:
        return bool(self._run(self._app.monitor.add_urls, str(raw or "")))

    def fetch_following(self, account: str = ""):
        """Start reading a following list; returns its request id (``False``: not started)."""
        return self._run(self._app.monitor.fetch_following, str(account or "")) or False

    def cancel_following_fetch(self) -> bool:
        return bool(self._run(self._app.monitor.cancel_following))

    def import_following(self, request_id: str, sec_uids: List[str]) -> bool:
        return bool(self._run(self._app.monitor.import_following, str(request_id or ""),
                              [str(value) for value in sec_uids or []]))

    def set_monitor_enabled(self, sec_uids: List[str], enabled: bool) -> bool:
        return bool(self._call(self._app.monitor.set_enabled([str(v) for v in sec_uids or []],
                                                             bool(enabled))))

    def set_monitor_comments(self, sec_uids: List[str], enabled: bool) -> bool:
        return bool(self._call(self._app.monitor.set_comments([str(v) for v in sec_uids or []],
                                                              bool(enabled))))

    def check_monitors(self, sec_uids: List[str]) -> bool:
        return bool(self._call(self._app.monitor.check([str(v) for v in sec_uids or []])))

    def full_scan_monitors(self, sec_uids: List[str], validate_local_files: bool = False) -> bool:
        return bool(self._call(self._app.monitor.full_scan([str(v) for v in sec_uids or []],
                                                           bool(validate_local_files))))

    def full_scan_progress(self) -> Optional[dict]:
        return self._call(self._app.monitor.full_scan_progress())

    def monitor_download_board(self, limit: int = 30) -> dict:
        return self._call(self._ctx.queue.board(max(1, min(100, _int(limit, 30)))))

    def user_download_board(self, limit: int = 30, full: Optional[List[int]] = None) -> dict:
        """The download panel's 用户 tab: like/collect downloads grouped by
        subscription; the groups in ``full`` list up to ``limit`` works."""
        return self._call(self._ctx.queue.user_board(
            max(1, min(100, _int(limit, 30))), {_int(value) for value in full or []}))

    def comment_board(self, limit: int = 30) -> dict:
        """The download panel's 评论 tab: the work being read and the queue."""
        return self._call(self._app.comments.board(max(1, min(100, _int(limit, 30)))))

    def rebuild_download_backlog(self) -> int:
        """Drop the waiting works and full-scan every source again."""
        return int(self._call(self._app.rebuild_backlog()) or 0)

    def scan_board(self, limit: int = 30) -> dict:
        """The footer's scan panel: running and waiting scans, and failed sources."""
        return self._call(self._app.activity.board(max(1, min(100, _int(limit, 30)))))

    def scan_status(self) -> dict:
        """The current left-lane line (the page asks once after loading)."""
        return self._run(self._app.activity.line)

    def validate_local_files(self, sec_uids: Optional[List[str]] = None) -> bool:
        return bool(self._run(self._app.monitor.validate_local_files,
                              [str(value) for value in sec_uids or []]))

    def delete_monitors(self, sec_uids: List[str], delete_local_works: bool = False) -> bool:
        return bool(self._call(self._app.monitor.delete([str(v) for v in sec_uids or []],
                                                        bool(delete_local_works))))

    # ── subscriptions ───────────────────────────────────────────────

    def subscriptions(self) -> List[dict]:
        return self._call(self._app.subscriptions.rows())

    def add_subscription(self, url: Optional[str] = None,
                         kinds: Optional[List[str]] = None, account: str = "") -> dict:
        """Subscribe to a library account (``account``) or anyone's homepage (``url``)."""
        wanted = [str(kind) for kind in (kinds or ["like"]) if str(kind) in ("like", "collect")]
        if not wanted:
            raise ValueError("请至少选择喜欢或收藏")
        raw, account = str(url or "").strip(), str(account or "").strip()
        if account:
            profile = self._call(self._app.account.library_profile(account))
        else:
            profile = self._call(self._app.account.lock(raw or None))
        # A library account's collection can be read with its own login.
        profile["is_self"] = self._ctx.settings.account(profile["sec_uid"]) is not None
        if not profile["is_self"] and wanted == ["collect"]:
            raise ValueError("收藏只能订阅账号库里的账号，请先在设置里登录这个账号")
        avatar = str(profile.get("avatar_data") or "")
        profile = {**profile, "avatar_url": avatar if len(avatar) <= 64 * 1024 else ""}
        ids = self._call(self._app.subscriptions.add(profile, wanted))
        return {"ids": ids, "nickname": profile["nickname"], "is_self": profile["is_self"],
                "collect_skipped": "collect" in wanted and not profile["is_self"]}

    def set_subscription_enabled(self, ids: List[int], enabled: bool) -> bool:
        return self._call(self._app.subscriptions.set_enabled(
            [_int(value) for value in ids or []], bool(enabled))) > 0

    def scan_subscriptions(self, ids: List[int], full: bool = False) -> bool:
        return self._run(self._app.subscriptions.request_scan,
                         [_int(value) for value in ids or []], bool(full)) > 0

    def retry_subscriptions(self, ids: List[int]) -> int:
        return int(self._call(self._app.subscriptions.retry_failed(
            [_int(value) for value in ids or []])))

    def delete_subscriptions(self, ids: List[int]) -> bool:
        return self._call(self._app.subscriptions.delete(
            [_int(value) for value in ids or []])) > 0

    # ── history ─────────────────────────────────────────────────────

    def work_details(self, aweme_id: str) -> dict:
        work = self._call(self._app.library.work_details(str(aweme_id)))
        # The UI receives the same ledger facts as history; payload and cookies
        # are deliberately not part of this small projection.
        return {key: work.get(key) for key in (
            "aweme_id", "title", "status", "error", "error_code", "files", "downloaded_at")}

    def open_work_directory(self, aweme_id: str) -> bool:
        work = self._call(self._app.library.work_details(str(aweme_id)))
        for file in work.get("files") or []:
            path = Path(str(file.get("path") or ""))
            if path.is_file():
                return self.open_directory(str(path.parent))
        return False

    def history(self, filters: Optional[dict] = None) -> Optional[dict]:
        try:
            return self._call(self._app.library.history(dict(filters or {})))
        except Exception as exc:
            self._ctx.events.error("history", explain(exc))
            return None

    # ── comments ────────────────────────────────────────────────────

    def load_comments(self, aweme_id: str) -> bool:
        raw = str(aweme_id or "").strip()
        if not raw:
            self._ctx.events.error("comments", "请输入作品链接、分享文案或作品 ID")
            return False
        try:
            return bool(self._run(self._app.comments.load, raw))
        except Exception as exc:
            self._ctx.events.error("comments", explain(exc))
            return False

    def download_comment_images(self, keys: List[str], save_dir: str) -> bool:
        return bool(self._run(self._app.comments.download_images,
                              [str(key) for key in keys or []], str(save_dir or "")))

    def zoom_comment_image(self, key: str, comment_id: str, index: int) -> bool:
        return bool(self._run(self._app.comments.zoom, str(key)))

    # ── shutdown ────────────────────────────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._media.close()
        finally:
            self._app.close()
