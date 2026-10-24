"""Custom title bar for the Windows desktop window.

The page draws its own title bar (``.titlebar`` in ``gui/web``); this module
removes the native caption underneath it while keeping everything else the
system frame provides:

* ``WM_NCCALCSIZE`` drops only the caption, so the side and bottom resize
  borders, the drop shadow, snapping and the min/max animations stay native.
* WebView2's non-client region support turns ``app-region: drag`` areas of
  the page into a real caption: dragging, double-click to maximize, Aero Snap
  and the system menu on right click.
* The WebView covers the whole client area up to the top edge, so the page
  has a thin top-edge strip whose mouse-down hands the press to Windows as a
  ``WM_NCLBUTTONDOWN`` on the top border (:func:`start_resize`).

The caption buttons post ``WM_SYSCOMMAND`` like the native ones, so closing
still runs pywebview's normal closing path.  Anything that fails leaves the
native caption in place and :func:`frame_state` reports ``custom: False``,
which tells the page to hide its own title bar.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any, Dict

from common.log import get_logger

logger = get_logger(__name__)

WM_NCDESTROY = 0x0082
WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
WM_SYSCOMMAND = 0x0112
RESIZE_EDGES = {"top": 12, "topleft": 13, "topright": 14}  # HTTOP, HTTOPLEFT, HTTOPRIGHT
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE, SC_CLOSE = 0xF020, 0xF030, 0xF120, 0xF060
SM_CYFRAME, SM_CXPADDEDBORDER = 33, 92
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # no size/move/zorder/activate; frame changed
_SUBCLASS_ID = 0xD0D1

_installed: Dict[int, Any] = {}  # hwnd -> subclass callback, kept alive
_patched = False


def _hwnd(form) -> int:
    return int(form.Handle.ToInt64())


def _form(window):
    if os.name != "nt" or window is None:
        return None
    return getattr(window, "native", None)


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    return user32


def _subclass(hwnd: int) -> None:
    user32 = _user32()
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    proc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
    comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    comctl32.DefSubclassProc.restype = lresult
    comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, proc_type, ctypes.c_size_t, ctypes.c_size_t]
    comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, proc_type, ctypes.c_size_t]
    default = comctl32.DefSubclassProc

    def proc(handle, msg, wparam, lparam, _id, _data):
        if msg == WM_NCCALCSIZE and wparam:
            client = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            top = client.top
            default(handle, msg, wparam, lparam)
            if user32.IsZoomed(handle):
                # A maximized window hangs its frame off-screen; keep the content on it.
                dpi = user32.GetDpiForWindow(handle) or 96
                top += (user32.GetSystemMetricsForDpi(SM_CYFRAME, dpi)
                        + user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi))
            client.top = top
            return 0
        if msg == WM_NCDESTROY:
            comctl32.RemoveWindowSubclass(handle, callback, _SUBCLASS_ID)
            _installed.pop(int(handle or 0), None)
        return default(handle, msg, wparam, lparam)

    callback = proc_type(proc)
    if not comctl32.SetWindowSubclass(hwnd, callback, _SUBCLASS_ID, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    _installed[hwnd] = callback
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)


def _customize(form, core) -> None:
    # Older WebView2 runtimes lack this; then the native caption stays.
    core.Settings.IsNonClientRegionSupportEnabled = True
    _subclass(_hwnd(form))


def install() -> bool:
    """Hook pywebview's WebView2 start-up; call before ``webview.start``."""
    global _patched
    if os.name != "nt":
        return False
    if _patched:
        return True
    try:
        from webview.platforms import edgechromium
    except Exception as exc:
        logger.info("自定义标题栏不可用：%s", exc)
        return False
    original = edgechromium.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        if args.IsSuccess:
            try:
                _customize(self.form, sender.CoreWebView2)
            except Exception as exc:
                logger.warning("自定义标题栏启用失败，保留系统标题栏：%s", exc)
        return original(self, sender, args)

    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    _patched = True
    return True


def frame_state(window) -> Dict[str, bool]:
    form = _form(window)
    try:
        hwnd = _hwnd(form) if form is not None else 0
    except Exception:
        hwnd = 0
    if not hwnd:
        return {"custom": False, "maximized": False}
    return {"custom": hwnd in _installed, "maximized": bool(_user32().IsZoomed(hwnd))}


def system_command(window, action: str) -> bool:
    """Run a caption button the way the native one does."""
    form = _form(window)
    if form is None:
        return False
    try:
        hwnd = _hwnd(form)
        user32 = _user32()
        if action == "maximize":
            command = SC_RESTORE if user32.IsZoomed(hwnd) else SC_MAXIMIZE
        else:
            command = {"minimize": SC_MINIMIZE, "close": SC_CLOSE}.get(action)
            if command is None:
                return False
        return bool(user32.PostMessageW(hwnd, WM_SYSCOMMAND, command, 0))
    except Exception:
        return False


def start_resize(window, edge: str) -> bool:
    """Turn a mouse-down on the page's top-edge strip into a native resize."""
    form = _form(window)
    hit = RESIZE_EDGES.get(edge)
    if form is None or hit is None:
        return False
    try:
        from System import Action

        hwnd = _hwnd(form)
        user32 = _user32()
        if hwnd not in _installed or user32.IsZoomed(hwnd):
            return False

        def begin():
            # The WebView holds the mouse capture from the press; the sizing
            # loop only tracks the drag once the window's thread releases it.
            if not user32.GetAsyncKeyState(0x01) & 0x8000:  # VK_LBUTTON already up
                return
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.ReleaseCapture()
            position = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, hit, position)

        form.BeginInvoke(Action(begin))
        return True
    except Exception:
        return False
