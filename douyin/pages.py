"""One shape for every paginated Douyin response."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from douyin.errors import BadResponse, PaginationError, RiskControl


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


@dataclass
class Page:
    items: List[Any] = field(default_factory=list)
    has_more: bool = False
    cursor: int = 0
    status_code: int = 0
    status_msg: str = ""
    items_missing: bool = True
    login_tip: bool = False
    verify_page: bool = False
    source: str = "api"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any, item_keys: Iterable[str] = (), *, source: str = "api",
              cursor_key: str = "") -> "Page":
        data = raw if isinstance(raw, dict) else {}
        items: List[Any] = []
        missing = True
        for key in (*item_keys, "items", "aweme_list"):
            value = data.get(key)
            if isinstance(value, list):
                items, missing = value, False
                break
        try:
            has_more = bool(int(data.get("has_more") or 0))
        except (TypeError, ValueError):
            has_more = bool(data.get("has_more"))
        if cursor_key:
            cursor = to_int(data.get(cursor_key))
        else:
            cursor = to_int(data.get("max_cursor") if data.get("max_cursor") is not None
                            else data.get("cursor"))
        login = data.get("not_login_module")
        return cls(
            items=items,
            has_more=has_more,
            cursor=cursor,
            status_code=to_int(data.get("status_code")),
            status_msg=str(data.get("status_msg") or ""),
            items_missing=missing,
            login_tip=bool(isinstance(login, dict) and login.get("guide_login_tip_exist")),
            verify_page=bool(data.get("verify_ticket")),
            source=source,
            raw=data,
        )

    @property
    def dict_items(self) -> List[Dict[str, Any]]:
        return [item for item in self.items if isinstance(item, dict)]

    @property
    def suspicious(self) -> bool:
        """A response that must not be read as "the list is empty"."""
        return (not self.raw or self.items_missing or self.status_code != 0
                or self.login_tip or self.verify_page)

    def ensure_valid(self, what: str = "列表接口") -> "Page":
        """Raise when this page cannot be trusted as a real list page."""
        if not self.raw:
            raise RiskControl(f"{what}返回空响应")
        if self.verify_page:
            raise RiskControl(f"{what}触发了验证码")
        if self.login_tip and not self.items:
            raise RiskControl(f"{what}要求登录")
        if self.status_code != 0:
            message = self.status_msg or "未知错误"
            raise BadResponse(f"{what}返回错误状态 {self.status_code}：{message}")
        if self.items_missing:
            raise RiskControl(f"{what}缺少列表字段")
        if not self.items and self.has_more:
            raise PaginationError(f"{what}返回空页但仍有下一页")
        return self


class CursorGuard:
    """Refuses a cursor that does not move forward (loop protection)."""

    def __init__(self, start: int = 0):
        self.seen: Set[int] = {int(start or 0)}

    def advance(self, cursor: int, what: str = "列表") -> int:
        value = int(cursor or 0)
        if value <= 0 or value in self.seen:
            raise PaginationError(f"{what}分页游标异常（{value}），未能读完")
        self.seen.add(value)
        return value


def first_url(value: Any) -> str:
    urls = url_list(value)
    return urls[0] if urls else ""


def url_list(value: Any) -> List[str]:
    """URLs from ``{url_list: [...]}``, a list, or a plain string."""
    if isinstance(value, dict):
        for key in ("url_list", "urlList"):
            listed = value.get(key)
            if isinstance(listed, list) and listed:
                return [str(item) for item in listed if isinstance(item, str) and item]
        for key in ("url", "uri"):
            single = value.get(key)
            if isinstance(single, str) and single.startswith(("http://", "https://")):
                return [single]
        return []
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(url_list(item) if not isinstance(item, str) else [item] if item else [])
        return result
    if isinstance(value, str) and value:
        return [value]
    return []


def user_sec_uid(user: Optional[Dict[str, Any]]) -> str:
    if not isinstance(user, dict):
        return ""
    return str(user.get("sec_uid") or user.get("sec_user_id")
               or user.get("sec_user_id_str") or "").strip()


def avatar_url(user: Optional[Dict[str, Any]]) -> str:
    if not isinstance(user, dict):
        return ""
    for key in ("avatar_larger", "avatar_medium", "avatar_thumb", "avatar_300x300", "avatar"):
        url = first_url(user.get(key))
        if url:
            return url
    return ""
