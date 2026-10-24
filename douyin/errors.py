"""Typed failures raised by the Douyin client."""

from __future__ import annotations

from typing import Optional


class DouyinError(RuntimeError):
    """Base class; ``retryable`` tells callers whether trying again may help."""

    retryable = False

    def __init__(self, message: str, *, path: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.path = path
        self.status = status


class LoginRequired(DouyinError):
    """Douyin answered that the session is not (or no longer) logged in."""

    def __init__(self, message: str = "登录已失效，请重新登录", *, path: str = "",
                 status: Optional[int] = None):
        super().__init__(message, path=path, status=status)


class HttpError(DouyinError):
    def __init__(self, status: int, path: str, *, body: str = ""):
        super().__init__(f"Douyin HTTP {status} at {path}", path=path, status=status)
        self.body = str(body or "")
        self.retryable = status == 429 or status >= 500

    @property
    def signature_rejected(self) -> bool:
        text = self.body.lower()
        return self.status == 403 and any(
            marker in text for marker in ("sign invalid", "argussecurityplugin", "uifid not found")
        )


class RiskControl(DouyinError):
    """A verification page, login prompt or empty envelope instead of data."""


class EmptyResponse(DouyinError):
    retryable = True


class BadResponse(DouyinError):
    """The response was JSON but not the shape the endpoint promises."""

    retryable = True


class NetworkError(DouyinError):
    retryable = True


class PaginationError(DouyinError):
    """The listing advertised more pages but its cursor did not advance."""


def is_risk(exc: BaseException) -> bool:
    """Whether ``exc`` means "slow down / verify", not "this item is broken"."""
    if isinstance(exc, (RiskControl, EmptyResponse)):
        return True
    return isinstance(exc, HttpError) and exc.status in (403, 429)


def describe(exc: BaseException) -> str:
    """A short Chinese explanation of a failure for logs and the UI."""
    if isinstance(exc, LoginRequired):
        return "抖音登录已失效，请在设置中重新扫码登录。"
    if isinstance(exc, HttpError):
        if exc.signature_rejected:
            return "抖音拒绝了请求签名（HTTP 403），请稍后重试。"
        if exc.status == 403:
            return "抖音接口拒绝了请求（HTTP 403），请稍后重试。"
        if exc.status == 429:
            return "请求过于频繁，已触发抖音限流，请稍后重试。"
        return f"抖音接口返回 HTTP {exc.status}，请稍后重试。"
    if isinstance(exc, RiskControl):
        return f"抖音触发了风控验证：{exc}"
    if isinstance(exc, EmptyResponse):
        return "抖音返回了空响应（可能是反爬保护），请稍后重试。"
    if isinstance(exc, NetworkError):
        return "网络连接失败，请检查网络或代理设置。"
    text = str(exc).strip()
    return text or type(exc).__name__
