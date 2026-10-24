"""Launch the real Windows WebView with isolated data, check it, then close."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--expect-persistence", action="store_true")
    args = parser.parse_args()
    os.environ["DOUYIN_DOWNLOADER_DATA_DIR"] = str(args.data_dir.resolve())
    import webview

    import desktop_app

    original = webview.create_window
    outcome = {"ok": False}

    def create(*values, **options):
        window = original(*values, **options)

        def check():
            try:
                for _ in range(180):
                    result = window.evaluate_js("""JSON.stringify({
                        controls: document.querySelectorAll('.sidebar .nav button').length,
                        heading: document.getElementById('page-title')?.textContent || document.title,
                        ready: typeof state !== 'undefined' && state.startup === '',
                        stored: localStorage.getItem('release-smoke')
                    })""")
                    result = json.loads(result)
                    if result["ready"] and result["controls"] == 6:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Native WebView did not reach its ready state")
                if args.expect_persistence and result["stored"] != "persistent":
                    raise RuntimeError("WebView profile did not retain localStorage")
                window.evaluate_js("localStorage.setItem('release-smoke', 'persistent')")
                outcome.update(ok=True, controls=result["controls"], persistence=args.expect_persistence)
            except Exception as exc:
                outcome["error"] = str(exc)
            finally:
                window.destroy()

        window.events.loaded += lambda: threading.Thread(target=check, daemon=True).start()
        return window

    webview.create_window = create
    desktop_app.main()
    print(json.dumps(outcome))
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
