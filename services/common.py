"""Small pieces every service shares: links, profiles, pagination, errors."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

from douyin import urls
from douyin.client import DouyinClient
from douyin.errors import DouyinError, LoginRequired, describe
from douyin.pages import CursorGuard, Page, avatar_url, to_int, user_sec_uid


def explain(exc: BaseException) -> str:
    """User-facing Chinese text for any failure."""
    if isinstance(exc, LoginRequired):
        return "抖音登录已失效，请在「设置」中重新扫码登录后再试。"
    if isinstance(exc, DouyinError):
        return describe(exc)
    if isinstance(exc, (PermissionError, OSError)):
        return f"无法写入下载目录：{exc}"
    text = str(exc).strip()
    return text or type(exc).__name__


async def resolve_link(client: DouyinClient, text: str) -> Tuple[str, Optional[urls.Link]]:
    """Expand share text / short links; returns ``(url, link or None)``."""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("链接为空")
    url = urls.extract_url(raw) or raw
    if url.isdigit():
        return url, urls.Link("video", url, aweme_id=url)
    if urls.is_short(url):
        resolved = await client.resolve_short_url(urls.normalize_short(url))
        if not resolved:
            raise RuntimeError(f"短链接解析失败：{url}")
        url = resolved
    return url, urls.classify(url)


def profile_view(user: Dict[str, Any], *, is_self: bool) -> Dict[str, Any]:
    return {
        "sec_uid": user_sec_uid(user),
        "nickname": str(user.get("nickname") or "抖音用户").strip(),
        "avatar_url": avatar_url(user),
        "is_self": bool(is_self),
        "aweme_count": to_int(user.get("aweme_count")),
        "favoriting_count": to_int(user.get("favoriting_count")),
        "total_favorited": to_int(user.get("total_favorited")),
        "uid": str(user.get("uid") or ""),
        "user_info": user,
    }


async def resolve_profile(client: DouyinClient, text: Optional[str]) -> Dict[str, Any]:
    """The profile behind a homepage link, or of the logged-in account (``None``)."""
    if text is None or str(text).strip().lower() in ("", "self"):
        if text is not None and not str(text).strip():
            raise ValueError("请输入用户主页链接")
        user = await client.self_profile()
        if not user or not user_sec_uid(user):
            raise RuntimeError("未能读取你的账号资料，请确认已登录后重试")
        return profile_view(user, is_self=True)
    _url, link = await resolve_link(client, text)
    if link is None or link.kind != "user" or not link.sec_uid:
        raise ValueError("请输入用户主页链接")
    if link.sec_uid == "self":
        return await resolve_profile(client, None)
    me = None
    try:
        me = await client.self_profile()
    except Exception:
        pass  # another account's public profile does not need the self probe
    if me and user_sec_uid(me) == link.sec_uid:
        return profile_view(me, is_self=True)
    user = await client.user_profile(link.sec_uid)
    if not user:
        raise RuntimeError("未能读取该账号资料，请检查主页链接后重试")
    view = profile_view(user, is_self=False)
    view["sec_uid"] = view["sec_uid"] or link.sec_uid
    return view


async def pages(fetch: Callable[[int], Awaitable[Page]], *, start: int = 0,
                what: str = "列表", delay: Callable[[], float] = lambda: 0.0,
                cancelled: Callable[[], bool] = lambda: False) -> AsyncIterator[Page]:
    """Walk a cursor-paginated listing, refusing untrustworthy pages."""
    cursor = start
    guard = CursorGuard(start)
    first = True
    while True:
        if cancelled():
            raise asyncio.CancelledError()
        if not first:
            pause = delay()
            if pause > 0:
                await asyncio.sleep(pause)
        first = False
        page = (await fetch(cursor)).ensure_valid(what)
        yield page
        if not page.has_more:
            return
        cursor = guard.advance(page.cursor, what)
