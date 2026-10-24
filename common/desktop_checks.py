"""Native-window release checks using pywebview's page-loaded event."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


def check_native_window(folder: Path, log: Path, *, expect_persistence: bool) -> int:
    import webview

    from gui.web_app import run_web_app

    original = webview.create_window
    outcome = {"ok": False, "native_ui": True, "restart": expect_persistence}

    def create(*values, **options):
        window = original(*values, **options)

        def check():
            try:
                for _ in range(180):
                    state = window.evaluate_js("""JSON.stringify({
                        controls: document.querySelectorAll('.sidebar .nav button').length,
                        ready: typeof state !== 'undefined' && state.startup === '',
                        stored: localStorage.getItem('release-native-selftest')
                    })""")
                    state = json.loads(state)
                    if state["ready"] and state["controls"] == 6:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Native window did not reach its ready state")
                if expect_persistence and state["stored"] != "persistent":
                    raise RuntimeError("Native profile did not retain localStorage")
                window.evaluate_js("""localStorage.setItem('release-native-selftest', 'persistent');
                    document.querySelector('[data-page="settings"]').click()""")
                for _ in range(80):
                    if window.evaluate_js("document.querySelectorAll('.settings-group').length > 0"):
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Native settings page did not render")
                outcome.update(ok=True, controls=6, settings=True,
                               database=(folder / "DouyinDownloader.db").is_file())
            except Exception as exc:
                outcome["error"] = str(exc)
            finally:
                window.destroy()

        window.events.loaded += lambda: threading.Thread(target=check, daemon=True).start()
        return window

    webview.create_window = create
    try:
        run_web_app(folder, title="Release window self-test")
    except Exception as exc:
        outcome["error"] = str(exc)
    finally:
        webview.create_window = original
        log.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return 0 if outcome["ok"] and outcome.get("database") else 1
