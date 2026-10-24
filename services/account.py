"""Login, the account library, the profile card, and the account the user is working with.

The account library (``Settings.accounts``) holds every logged-in account;
the *main* one is used for everything that does not name an account.  The
client gets one session per account (:meth:`AccountService.sync`).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.fs import atomic_write_json, image_data_url, nonempty_file, read_json
from douyin import cookies as cookie_tools
from douyin.client import use_account
from douyin.errors import LoginRequired
from douyin.pages import user_sec_uid
from douyin.qr import qr_login
from services.common import explain, resolve_profile
from services.context import Context

USER_MODES = ("post", "like", "mix", "music", "collect", "collectmix")
SELF_ONLY_MODES = ("collect", "collectmix")


class AccountService:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._generation = 0
        self._profile = self._load_cached()
        self._synced_main: Optional[tuple] = None
        self._loaded = False
        self.locked: Optional[Dict[str, Any]] = None   # the confirmed user account

    # ── the account library ─────────────────────────────────────────

    async def start(self) -> None:
        """Hand the library to the client; name a migrated login from the cache."""
        settings = self.ctx.settings
        cached = read_json(self._cache_file, {})
        cached_sec_uid = str(cached.get("sec_uid") or "") if isinstance(cached, dict) else ""
        if settings.account("") is not None and cached_sec_uid and not settings.main_account:
            settings.name_pending_account(cached_sec_uid,
                                          nickname=str(cached.get("nickname") or ""))
        self._synced_main = (settings.main_account, settings.cookies)
        await self.sync()

    async def sync(self) -> None:
        """Give every component the library's current logins."""
        settings = self.ctx.settings
        main = (settings.main_account, settings.cookies)
        others = {entry["sec_uid"]: entry["cookies"] for entry in settings.accounts()
                  if entry["sec_uid"] and not entry["expired"]}
        await self.ctx.client.sync_accounts(main[0], main[1], others)
        if main != self._synced_main:
            main_changed = self._synced_main is None or main[0] != self._synced_main[0] \
                or not main[0]
            self._synced_main = main
            self.ctx.browser.set_cookies(main[1])
            if main_changed:
                self.clear_profile()
                if settings.logged_in and self._loaded:
                    self.refresh_profile()
            if settings.logged_in:
                self.ctx.scheduler.resume_login()
        self._loaded = True
        self.publish()

    async def apply_cookies(self, cookies: Dict[str, str]) -> None:
        """Log in with cookies pasted by hand, or (``{}``) log the main account out."""
        if cookies:
            await self.add_login(cookies)
            return
        settings = self.ctx.settings
        settings.remove_account(settings.main_account)
        await self.sync()

    async def add_login(self, cookies: Dict[str, str]) -> Dict[str, Any]:
        """Add a login to the library (or renew an account's login); returns its entry."""
        settings = self.ctx.settings
        try:
            user = await self.ctx.client.profile_with(cookies)
        except Exception:
            user = None
        sec_uid = user_sec_uid(user) if user else ""
        if not sec_uid:
            if settings.accounts() and settings.account("") is None:
                raise RuntimeError("未能读取这个登录的账号资料，请重新登录")
            settings.set_cookies(cookies)       # the first login: named once it is read
            await self.sync()
            return settings.account("") or {}
        nickname = str(user.get("nickname") or "").strip() or "抖音用户"
        settings.save_account(sec_uid, cookies, uid=str(user.get("uid") or ""),
                              nickname=nickname)
        await self._save_avatar(user, self._account_avatar(sec_uid))
        await self.sync()
        return settings.account(sec_uid) or {}

    def set_main(self, sec_uid: str) -> bool:
        if not self.ctx.settings.set_main_account(sec_uid):
            return False
        self.ctx.background(self.sync(), "切换主账号")
        return True

    def remove(self, sec_uid: str) -> bool:
        if not self.ctx.settings.remove_account(sec_uid):
            return False
        try:
            self._account_avatar(sec_uid).unlink(missing_ok=True)
        except OSError:
            pass
        self.ctx.background(self.sync(), "移除账号")
        return True

    def login_lost(self, sec_uid: str) -> None:
        """Douyin said a (non-main) account's login expired."""
        entry = self.ctx.settings.account(sec_uid)
        if entry is None or entry["expired"]:
            return
        self.ctx.settings.save_account(sec_uid, expired=True)
        self.ctx.events.log(f"账号「{entry['nickname'] or sec_uid}」的登录已失效，"
                            f"它的订阅已暂停，请在设置里重新登录。")
        self.ctx.background(self.sync(), "更新账号库")

    async def library_profile(self, sec_uid: str) -> Dict[str, Any]:
        """A library account's own profile, read with its own login."""
        entry = self.ctx.settings.account(sec_uid)
        if entry is None:
            raise RuntimeError("账号库里没有这个账号")
        if entry["expired"]:
            raise RuntimeError("这个账号的登录已失效，请先在设置里重新登录")
        use_account(sec_uid)
        try:
            profile = await resolve_profile(self.ctx.client, None)
        except LoginRequired as exc:
            raise RuntimeError(explain(exc)) from exc
        avatar = self._account_avatar(sec_uid)
        profile["avatar_data"] = image_data_url(avatar) if nonempty_file(avatar) else ""
        return profile

    def _account_avatar(self, sec_uid: str) -> Path:
        return self.ctx.cache_dir / "accounts" / f"{sec_uid}.jpg"

    def accounts(self) -> List[Dict[str, Any]]:
        """The library as the settings page shows it (no cookies)."""
        main = self.ctx.settings.main_account
        rows = []
        for entry in self.ctx.settings.accounts():
            sec_uid = entry["sec_uid"]
            avatar = self._account_avatar(sec_uid) if sec_uid else self._avatar_file
            if sec_uid == main and not nonempty_file(avatar):
                avatar = self._avatar_file
            nickname = entry["nickname"]
            if sec_uid == main and not nickname:
                nickname = self._profile.get("nickname") or ""
            rows.append({"sec_uid": sec_uid, "nickname": nickname or "抖音用户",
                         "avatar_data": image_data_url(avatar), "is_main": sec_uid == main,
                         "expired": bool(entry["expired"]), "added_at": entry["added_at"]})
        return rows

    def publish(self) -> None:
        self.ctx.events.emit("accounts", accounts=self.accounts(),
                             logged_in=self.ctx.settings.logged_in)

    # ── profile card ────────────────────────────────────────────────

    @property
    def _cache_file(self) -> Path:
        return self.ctx.cache_dir / "account_profile.json"

    @property
    def _avatar_file(self) -> Path:
        return self.ctx.cache_dir / "account.jpg"

    def _load_cached(self) -> Dict[str, Any]:
        profile = {"status": "idle", "nickname": "", "updated_at": 0, "error": ""}
        data = read_json(self.ctx.cache_dir / "account_profile.json", {})
        if isinstance(data, dict):
            profile["nickname"] = str(data.get("nickname") or "")
            profile["updated_at"] = int(data.get("updated_at") or 0)
            if profile["nickname"] or nonempty_file(self.ctx.cache_dir / "account.jpg"):
                profile["status"] = "ready"
        return profile

    def profile(self) -> Dict[str, Any]:
        if not self.ctx.settings.logged_in:
            return {"status": "idle", "nickname": "", "avatar_data": "", "updated_at": 0}
        with self._lock:
            profile = dict(self._profile)
        payload = {"status": profile["status"], "nickname": profile["nickname"],
                   "avatar_data": image_data_url(self._avatar_file),
                   "updated_at": profile["updated_at"]}
        if profile.get("error"):
            payload["error"] = profile["error"]
        return payload

    def _set(self, generation: Optional[int] = None, **values: Any) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._profile.update(values)
        self.ctx.events.emit("account_profile", profile=self.profile())

    def clear_profile(self) -> None:
        for path in (self._cache_file, self._avatar_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        with self._lock:
            self._generation += 1
            self._profile = {"status": "idle", "nickname": "", "updated_at": 0, "error": ""}
        self.ctx.events.emit("account_profile", profile=self.profile())

    def needs_refresh(self) -> bool:
        with self._lock:
            status, name = self._profile["status"], self._profile["nickname"]
        return self.ctx.settings.logged_in and status != "loading" and (
            status != "ready" or not name or not nonempty_file(self._avatar_file))

    def refresh_profile(self) -> bool:
        """Queue a profile refresh (nickname + avatar of the logged-in account)."""
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._set(generation, status="loading", error="")

        async def run(_task) -> None:
            try:
                user = await self.ctx.client.self_profile()
                if not user:
                    raise RuntimeError("抖音未返回账号资料")
                nickname = str(user.get("nickname") or "").strip() or "抖音用户"
                await self._save_avatar(user, self._avatar_file)
                stamp = int(time.time())
                sec_uid = await self._name_main(user)
                atomic_write_json(self._cache_file, {"nickname": nickname, "updated_at": stamp,
                                                     "sec_uid": sec_uid})
                if sec_uid:
                    self.ctx.settings.save_account(sec_uid, nickname=nickname)
                    self.publish()
                self._set(generation, status="ready", nickname=nickname, updated_at=stamp,
                          error="")
            except LoginRequired:
                self._set(generation, status="error", error="登录已失效，请重新登录")
                self.ctx.events.emit("account_profile_auth_error")
            except Exception as exc:
                self._set(generation, status="error", error=explain(exc) or "资料获取失败")

        self.ctx.tasks.submit("account-profile", run, scope="settings", lane="profile",
                              key="account-profile", priority=15)
        return True

    async def _name_main(self, user: Optional[Dict[str, Any]]) -> str:
        """The main account's sec_uid; names a pending main login after ``user``."""
        settings = self.ctx.settings
        if settings.main_account:
            return settings.main_account
        sec_uid = user_sec_uid(user) if user else ""
        if sec_uid and settings.account("") is not None:
            settings.name_pending_account(sec_uid, uid=str(user.get("uid") or ""),
                                          nickname=str(user.get("nickname") or ""))
            self._synced_main = (settings.main_account, settings.cookies)
            await self.sync()
        return settings.main_account

    async def current_sec_uid(self) -> str:
        """The main account's sec_uid, ``""`` when unknown (asked once)."""
        if not self.ctx.settings.logged_in:
            return ""
        if not self.ctx.settings.main_account:
            try:
                user = await self.ctx.client.self_profile()
            except Exception:
                return ""
            return await self._name_main(user)
        return self.ctx.settings.main_account

    async def _save_avatar(self, user: Dict[str, Any], target: Path) -> Optional[Path]:
        from douyin import aweme as aw

        urls = aw.avatar_urls(user)
        if not urls:
            return None
        headers = self.ctx.client.media_headers()
        temp = target.with_name(f".{target.name}.part.jpg")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            saved = await self.ctx.downloader.fetcher.fetch([(url, headers) for url in urls],
                                                            temp, segmented=False)
            os.replace(saved, target)
            return target
        except Exception as exc:
            self.ctx.events.log(f"头像缓存失败：{explain(exc)}")
            return None

    async def cached_avatar(self, user: Dict[str, Any], sec_uid: str) -> Optional[Path]:
        """A user's avatar in the local cache (downloaded once)."""
        target = self.ctx.cache_dir / "avatars" / f"{sec_uid}.jpg"
        if nonempty_file(target):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        return await self._save_avatar(user, target)

    # ── login ───────────────────────────────────────────────────────

    def start_qr_login(self, window: bool = False) -> bool:
        """Log in by QR code: shown inside the app, or (``window``) in a browser window."""
        events = self.ctx.events

        async def run(_task) -> None:
            try:
                jar = await qr_login(
                    headless=not window, user_agent=self.ctx.client.user_agent,
                    proxy=self.ctx.settings.proxy,
                    on_qr=lambda image: events.emit("login_qr", image=image),
                    on_status=lambda status: events.emit("login_qr", status=status))
            except Exception as exc:
                self.ctx.events.emit("login", ok=False, text=explain(exc))
                return
            if not cookie_tools.is_logged_in(jar):
                events.emit("login", ok=False, text="未取得有效登录会话，请重新扫码。")
                return
            try:
                entry = await self.add_login(jar)
            except Exception as exc:
                events.emit("login", ok=False, text=explain(exc))
                return
            self.ctx.events.emit("login", ok=True, count=len(jar),
                                 logged_in=cookie_tools.is_logged_in(jar),
                                 nickname=entry.get("nickname") or "")
            if not self._profile.get("nickname") and self.ctx.settings.logged_in:
                self.refresh_profile()

        # Switching between the in-app QR code and the browser window replaces
        # the attempt in progress; the lane runs the new one once it has stopped.
        key = "qr-login:window" if window else "qr-login"
        self.ctx.tasks.cancel("qr-login" if window else "qr-login:window")
        task = self.ctx.tasks.submit("qr-login", run, scope="settings", lane="qr-login", key=key)
        self.ctx.events.emit("login", ok=None if task.state == "running" else "queued",
                             window=window)
        return True

    def cancel_qr_login(self) -> bool:
        cancelled = [self.ctx.tasks.cancel(key) for key in ("qr-login", "qr-login:window")]
        return any(cancelled)

    # ── the confirmed user account ──────────────────────────────────

    async def lock(self, url: Optional[str], account: str = "") -> Dict[str, Any]:
        """Resolve a homepage (or the logged-in account) and remember it.

        ``account`` names a library account: it is read, browsed and
        downloaded with its own login (``profile["viewer"]``).
        """
        viewer = str(account or "")
        if viewer:
            entry = self.ctx.settings.account(viewer)
            if entry is None:
                raise RuntimeError("账号库里没有这个账号")
            if entry["expired"]:
                raise RuntimeError("这个账号的登录已失效，请先在设置里重新登录")
            url = None
        elif url is None and not self.ctx.settings.logged_in:
            raise RuntimeError("请先在设置中登录抖音账号")
        use_account(viewer)
        try:
            profile = await resolve_profile(self.ctx.client, url)
        except LoginRequired as exc:
            raise RuntimeError(explain(exc)) from exc
        if viewer and profile["sec_uid"] != viewer:
            raise RuntimeError("读取到的账号与账号库不一致，请在设置里重新登录这个账号")
        profile["viewer"] = viewer
        avatar = await self.cached_avatar(profile["user_info"], profile["sec_uid"])
        profile["avatar_data"] = image_data_url(avatar) if avatar else ""
        ui = self.ctx.settings.ui()
        previous = ui.get("last_user_account") or {}
        same = isinstance(previous, dict) and previous.get("sec_uid") == profile["sec_uid"] \
            and previous.get("is_self") is profile["is_self"]
        mode = str(previous.get("mode") or "") if same else ""
        if mode not in USER_MODES or (not profile["is_self"] and mode in SELF_ONLY_MODES):
            mode = "like" if profile["is_self"] else "post"
        updates: Dict[str, Any] = {"last_user_account": {
            "sec_uid": profile["sec_uid"], "is_self": profile["is_self"], "mode": mode,
            "viewer": viewer}}
        pending = ui.get("pending_user_download") or {}
        if isinstance(pending, dict) and pending and (
                pending.get("sec_uid") != profile["sec_uid"]
                or pending.get("is_self") is not profile["is_self"]):
            updates["pending_user_download"] = None
        self.ctx.settings.update_ui(**updates)
        self.locked = profile
        return profile

    def unlock(self) -> None:
        self.locked = None
        self.ctx.settings.update_ui(last_user_account=None, pending_user_download=None)

    def remember_mode(self, mode: str) -> bool:
        account = self.locked or {}
        if not account or mode not in USER_MODES:
            return False
        if not account.get("is_self") and mode in SELF_ONLY_MODES:
            return False
        self.ctx.settings.update_ui(last_user_account={
            "sec_uid": account["sec_uid"], "is_self": bool(account["is_self"]), "mode": mode,
            "viewer": str(account.get("viewer") or "")})
        return True
