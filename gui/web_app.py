"""The desktop window: pywebview around ``gui/web`` with :class:`WebBridge`."""

from __future__ import annotations

from pathlib import Path

from gui import window_frame
from gui.bridge import WebBridge

__all__ = ["WebBridge", "run_web_app"]


def _revalidate_local_assets() -> None:
    """Make pywebview's local server send ``Cache-Control: no-cache``.

    Outside private mode pywebview serves ``gui/web`` on a fixed port and the
    WebView2 profile persists, so without a cache header Chromium keeps a stale
    ``index.html`` fresh by heuristic and the app shows an old UI after an
    update.  pywebview's own no-cache headers are dropped because
    ``static_file`` returns its own response, so the header goes on that.
    """
    import bottle

    if getattr(bottle.static_file, "_revalidates", False):
        return
    original = bottle.static_file

    def static_file(*args, **kwargs):
        response = original(*args, **kwargs)
        response.set_header("Cache-Control", "no-cache")
        return response

    static_file._revalidates = True
    bottle.static_file = static_file


def run_web_app(data_dir: Path, *, title: str = "抖音下载器") -> None:
    import webview

    _revalidate_local_assets()

    bridge = WebBridge(Path(data_dir))
    html = Path(__file__).with_name("web") / "index.html"
    window = webview.create_window(title, url=str(html), js_api=bridge, width=1440, height=900,
                                   min_size=(640, 480), resizable=True, hidden=True)
    bridge._window = window
    # A cold WebView2 profile can take seconds to initialize. Show the window
    # after the local page and bridge load, so its first frame contains the UI.
    first_page = True

    def show_first_page():
        nonlocal first_page
        if first_page:
            first_page = False
            window.show()

    window.events.loaded += show_first_page
    window_frame.install()
    # pywebview defaults to private mode, which wipes localStorage on every
    # close; the UI keeps per-user preferences (monitor columns, theme) there.
    try:
        webview.start(debug=False, private_mode=False,
                      storage_path=str(Path(data_dir) / "webview"))
    finally:
        # The closed event runs on another thread. Finish shutdown here before
        # Python's exit handlers shut down executors used by the queue/database.
        bridge.close()
