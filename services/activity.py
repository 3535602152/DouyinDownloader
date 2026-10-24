"""What the background scanners are doing, for the footer and the scan panel.

The monitor, the subscriptions and the comment sync report their current
step here; this module turns it into the one line the footer's left lane
shows (``scan_status`` events) and into the rows of the scan panel
(``board``): the scans running now, the sources waiting for a scan, and the
sources whose last scan failed with a readable reason.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Set

from services.context import Context
from services.scanning import creator_policy, next_scan_at, plan, subscription_policy
from storage import sources as source_store
from storage.db import rows

KIND_NAMES = {"creator": "主播", "like": "喜欢", "collect": "收藏"}

# The endpoint that triggered a risk pause, in words.
PATH_NAMES = {
    "/aweme/v1/web/aweme/detail/": "作品详情（下载）",
    "/aweme/v1/web/aweme/post/": "主播作品列表（扫描）",
    "/aweme/v1/web/aweme/favorite/": "喜欢列表（扫描）",
    "/aweme/v1/web/aweme/listcollection/": "收藏列表（扫描）",
    "/aweme/v1/web/user/profile/other/": "主播资料（扫描）",
    "/aweme/v1/web/follow/feed/": "关注动态（扫描）",
    "/aweme/v1/web/user/following/list/": "关注列表（扫描）",
    "/aweme/v1/web/comment/list/": "评论",
    "/aweme/v1/web/comment/list/reply/": "评论回复",
}

# (pattern, readable reason); the first match wins, unknown errors show raw.
_REASONS = [
    (r"触发风控", "触发风控，已暂停"),
    (r"ac-challenge|__ac", "被反爬验证页拦截"),
    (r"登录|login", "登录已失效"),
    (r"sign invalid|签名", "接口签名被拒绝"),
    (r"\b403\b", "接口拒绝访问（403）"),
    (r"\b429\b", "请求过于频繁（429）"),
    (r"empty 200|返回空|为空", "接口返回空数据"),
    (r"timeout|timed out|超时", "网络超时"),
    (r"cannot connect|connection|连接|network", "网络连接失败"),
    (r"游标|cursor|翻页|pagination", "翻页数据异常"),
    (r"核对已消失", "核对已消失的作品时中断"),
]


def readable_reason(error: str) -> str:
    """A short Chinese reason, or ``""`` when the error is not recognised."""
    lowered = str(error or "").lower()
    for pattern, reason in _REASONS:
        if re.search(pattern, lowered, re.IGNORECASE):
            return reason
    return ""


class ScanActivity:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.full: Dict[str, Any] = {}          # index, total, failed, name, seen, batch
        self.check: Dict[str, Any] = {}         # name, failed (this round)
        self.subscription: Dict[str, Any] = {}  # kind, name, seen, total
        # Subscriptions that are not scanned (another account is logged in).
        self.skipped: Callable[[], Set[int]] = lambda: set()
        # A creator's check policy (followed creators are checked through the feed).
        self.creator_policy: Optional[Callable[[Dict[str, Any]], Any]] = None
        ctx.scheduler.listeners.append(self.publish)

    # ── reporting ───────────────────────────────────────────────────

    def set(self, part: str, **values: Any) -> None:
        getattr(self, part).update(values)
        self.publish()

    def clear(self, part: str) -> None:
        getattr(self, part).clear()
        self.publish()

    def line(self) -> Dict[str, Any]:
        """The left lane: pause first, then full scan, check, subscription."""
        scheduler = self.ctx.scheduler
        if scheduler.login_paused:
            return {"text": "未登录", "detail": "", "running": False, "countdown": 0}
        risk = scheduler.pause_remaining()
        if risk > 0:
            path = str(getattr(scheduler, "pause_path", "") or "")
            return {"text": "风控", "detail": PATH_NAMES.get(path, path),
                    "running": False, "countdown": int(risk + 0.999)}
        if self.full:
            text = f"全量扫描 {self.full['index']}/{self.full['total']}"
            if self.full.get("failed"):
                text += f" 失败{self.full['failed']}"
            return {"text": text, "detail": f"{self.full['name']} {self.full.get('seen', 0)}",
                    "running": True, "countdown": 0}
        waiting = float(self.check.get("waiting_until") or 0) - time.time()
        if self.check and waiting > 0 and not self.full:
            return {"text": "等待下一个主播", "detail": "", "running": False,
                    "countdown": int(waiting + 0.999)}
        if self.check:
            text = "正在扫描"
            if self.check.get("failed"):
                text += f" 失败{self.check['failed']}"
            return {"text": text, "detail": self.check.get("name", ""), "running": True,
                    "countdown": 0}
        if self.subscription:
            label = KIND_NAMES.get(self.subscription.get("kind"), "订阅")
            seen, total = int(self.subscription.get("seen") or 0), \
                int(self.subscription.get("total") or 0)
            text = f"{label}扫描 {seen}/{total}" if total else f"{label}扫描 {seen}"
            return {"text": text, "detail": "", "running": True, "countdown": 0}
        return {"text": "", "detail": "", "running": False, "countdown": 0}

    def publish(self) -> None:
        self.ctx.events.emit_latest("scan-status", 0.5, "scan_status", **self.line())

    # ── scan panel ──────────────────────────────────────────────────

    def _running_rows(self) -> List[Dict[str, Any]]:
        result = []
        if self.full:
            result.append({"name": self.full["name"], "kind": "全量扫描",
                           "progress": f"已扫描 {self.full.get('seen', 0)}",
                           "state": "running"})
        if self.check.get("name"):
            result.append({"name": self.check.get("name", ""), "kind": "增量扫描",
                           "progress": "扫描中", "state": "running"})
        if self.subscription:
            label = KIND_NAMES.get(self.subscription.get("kind"), "订阅")
            seen, total = int(self.subscription.get("seen") or 0), \
                int(self.subscription.get("total") or 0)
            result.append({"name": self.subscription.get("name", ""), "kind": f"{label}扫描",
                           "progress": f"{seen}/{total}" if total else f"已扫描 {seen}",
                           "state": "running"})
        return result

    async def board(self, limit: int = 30) -> Dict[str, Any]:
        now = int(time.time())
        scheduler = self.ctx.scheduler
        running = self._running_rows()
        busy_names = {row["name"] for row in running}
        sources = await self.ctx.db.read(source_store.all_of, ["creator", "like", "collect"])
        pending_full = await self.ctx.db.read(_pending_full_scan)
        waiting_reason = "未登录" if scheduler.login_paused else \
            "风控暂停" if scheduler.pause_remaining() > 0 else ""
        queued, later, failures = [], [], []
        creator = creator_policy(self.ctx)
        skipped = self.skipped()
        for source in sources:
            name = str(source.get("nickname") or source["sec_uid"])
            kind = str(source["kind"])
            policy = (self.creator_policy(source) if self.creator_policy else creator) \
                if kind == "creator" else subscription_policy(kind)
            if source.get("last_error"):
                error = str(source["last_error"])
                failures.append({"name": name, "kind": KIND_NAMES.get(kind, kind),
                                 "time": int(source.get("last_check_at") or 0),
                                 "reason": readable_reason(error), "error": error,
                                 "count": int(source.get("failure_count") or 0)})
            if not source.get("enabled") or name in busy_names or int(source["id"]) in skipped:
                continue
            if int(source["id"]) in pending_full:
                queued.append((0, name, "全量扫描"))
                continue
            jitter = sum(ord(ch) for ch in str(source["sec_uid"])[-12:]) % 61 \
                if kind == "creator" else 0
            mode = plan(source, policy, now, jitter)
            label = KIND_NAMES.get(kind, "")
            if mode:
                order = 1 if kind == "creator" else 2
                queued.append((order, name, ("全量扫描" if mode == "full" else "增量扫描")
                               if kind == "creator" else f"{label}{'全量' if mode == 'full' else ''}扫描"))
            else:
                later.append(next_scan_at(source, policy))
        queued.sort(key=lambda entry: entry[0])
        failures.sort(key=lambda row: -row["time"])
        rows_out = running + [{"name": name, "kind": kind, "state": "waiting",
                               "progress": waiting_reason or "等待中"}
                              for _order, name, kind in queued]
        return {
            "rows": rows_out[:limit],
            "remaining": max(0, len(rows_out) - limit),
            "running": len(running),
            "waiting": len(queued),
            "later": len(later),
            "next_at": min(later) if later else 0,
            "paused": waiting_reason,
            "countdown": int(scheduler.pause_remaining() + 0.999),
            "failures": failures[:100],
            "failure_count": len(failures),
        }


def _pending_full_scan(db) -> set:
    """Sources still waiting in the newest unfinished full-scan batch."""
    return {int(row["source_id"]) for row in rows(
        db, "SELECT b.source_id FROM scan_batch_sources b JOIN scan_batches s "
            "ON s.id = b.batch_id WHERE s.status = 'scanning' AND b.state = 'pending'")}
