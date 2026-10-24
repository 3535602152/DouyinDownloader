"""Command line: download Douyin links with the same engine as the desktop app.

    douyin-dl https://v.douyin.com/xxxx/ https://www.douyin.com/video/123

Works are saved under the desktop app's download folder and recorded in its
library, so the desktop app sees them as downloaded.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

from common.paths import data_dir


def _print_events(app, name: str) -> bool:
    failed = False
    for event in app.ctx.events.drain():
        kind = event.get("type")
        if kind == "log":
            sys.stdout.write(str(event.get("text") or ""))
        elif kind == "row_status":
            print(f"  {event['id']}: {event['status']}")
        elif kind == "error":
            print(f"错误：{event.get('text')}", file=sys.stderr)
            failed = True
        elif kind in ("progress", "preview_done") and event.get("failed", 0):
            failed = True
        elif (kind == "task_state" and event.get("task") == name
              and event.get("state") in ("failed", "cancelled")):
            print(f"任务 {name}：{event['state']}", file=sys.stderr)
            failed = True
    return failed


def _wait(app, name: str) -> bool:
    failed = False
    while True:
        failed = _print_events(app, name) or failed
        states = app.ctx.runtime.call(_states(app), timeout=10)
        if not any(state["name"] == name for state in states):
            return _print_events(app, name) or failed
        time.sleep(0.2)


async def _states(app):
    return app.ctx.tasks.states()


async def _run(fn):
    return fn()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="douyin-dl", description="下载抖音作品、合集或音乐链接")
    parser.add_argument("links", nargs="+", help="作品/合集/音乐链接或分享文案")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认与桌面版相同）")
    parser.add_argument("--path", type=Path, default=None, help="本次使用的下载目录")
    args = parser.parse_args(argv)

    from services.app import App

    app = App.create(args.data_dir or data_dir(), start_background=False)
    try:
        if args.path:
            app.ctx.settings.update(path=str(args.path))
        if not app.ctx.settings.logged_in:
            print("提示：尚未登录，部分作品可能无法下载（请先在桌面版扫码登录）。", file=sys.stderr)
        app.ctx.call(_run(lambda: app.links.preview(args.links)))
        preview_failed = _wait(app, "link-preview")
        if not app.links.items:
            print("没有解析到可下载的作品。", file=sys.stderr)
            return 1
        app.ctx.call(_run(lambda: app.links.download(list(app.links.items))))
        download_failed = _wait(app, "link-download")
        return int(preview_failed or download_failed)
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
