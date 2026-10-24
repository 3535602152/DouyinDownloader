"""Everything a service needs, shared by all of them.

One Douyin client (one session, one request budget, one risk gate), one
browser channel, one database, one download engine: features cooperate
through these instead of each opening their own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from douyin.browser import BrowserChannel
from douyin.client import DouyinClient
from engine.downloader import WorkDownloader
from engine.events import EventBus
from engine.queue import DownloadQueue
from engine.runtime import Runtime, TaskManager
from engine.scheduler import Scheduler
from storage.db import Database
from storage.files import Layout
from storage.settings import Settings


@dataclass
class Context:
    data_dir: Path
    settings: Settings
    events: EventBus
    runtime: Runtime
    tasks: TaskManager
    db: Database
    client: DouyinClient
    browser: BrowserChannel
    downloader: WorkDownloader
    queue: DownloadQueue
    scheduler: Scheduler
    activity: Any = None      # services.activity.ScanActivity, set by the App

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def layout(self) -> Layout:
        settings = self.settings
        return Layout(settings.download_root,
                      author_style=str(settings.get("author_dir") or "nickname"),
                      file_template=str(settings.get("filename_template") or ""),
                      folder_template=str(settings.get("folder_template") or ""),
                      folder_per_work=bool(settings.get("folderstyle", True)))

    def background(self, coro: Awaitable[Any], label: str = "") -> "asyncio.Future[Any]":
        """Run ``coro`` on the loop without awaiting it; failures are logged."""
        future = asyncio.ensure_future(coro)

        def done(task: "asyncio.Future[Any]") -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                self.events.log(f"{label or '后台任务'}失败：{error}")

        future.add_done_callback(done)
        return future

    def call(self, coro: Awaitable[Any], timeout: Optional[float] = 60) -> Any:
        return self.runtime.call(coro, timeout)


Callback = Callable[..., Any]
