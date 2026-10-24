"""User-started downloads (link page, account downloads).

They use the same :class:`~engine.downloader.WorkDownloader` and the same
ledger as background downloads, so a work saved by any feature is "已存在"
for every other one.  While a user download runs, the background queue
starts nothing new.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from common.fs import nonempty_file
from douyin.errors import LoginRequired
from engine.downloader import SUCCESS, WorkRequest, WorkResult
from engine.fetcher import TransferProgress, current_progress
from services.common import explain
from services.context import Context
from storage import works as work_store
from storage.files import DownloadContext

FileCallback = Callable[[str, int, Optional[int], int], None]

DOWNLOADED, EXISTS, FAILED = "downloaded", "exists", "failed"


class ReportingProgress(TransferProgress):
    """Byte progress that also reports each file to a callback (throttled)."""

    def __init__(self, callback: Optional[FileCallback]):
        super().__init__()
        self.callback = callback
        self._sizes: Dict[str, Tuple[int, int]] = {}
        self._last: Dict[str, float] = {}

    def start_file(self, key: str, total: int) -> None:
        super().start_file(key, total)
        self._sizes[key] = (0, int(total or 0))
        self._report(key, force=True)

    def add(self, key: str, nbytes: int) -> None:
        super().add(key, nbytes)
        done, total = self._sizes.get(key, (0, 0))
        self._sizes[key] = (done + nbytes, total)
        self._report(key, force=bool(total) and done + nbytes >= total)

    def _report(self, key: str, *, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if not force and now - self._last.get(key, 0.0) < 0.25:
            return
        self._last[key] = now
        done, total = self._sizes.get(key, (0, 0))
        name = Path(key).name
        if name.endswith(".fetch"):
            name = name[:-6]
        rate = int(self.snapshot()["rate"])
        self.callback(name, done, total or None, rate)


async def media_on_disk(ctx: Context, aweme_id: str) -> bool:
    files = (await ctx.db.read(work_store.files_of, [aweme_id])).get(aweme_id, [])
    media = [entry for entry in files if entry["kind"] in ("media", "live")]
    return bool(media) and all(nonempty_file(entry["path"]) for entry in media)


async def download_one(ctx: Context, aweme_id: str, payload: Dict[str, Any],
                       context: DownloadContext, *, fresh: bool = False,
                       on_file: Optional[FileCallback] = None,
                       skip_existing: bool = True) -> Tuple[str, str]:
    """``(outcome, error)``; outcome is downloaded / exists / failed."""
    if skip_existing:
        status = (await ctx.db.read(work_store.statuses, [aweme_id])).get(aweme_id)
        if status == "done" and await media_on_disk(ctx, aweme_id):
            return EXISTS, ""
    await ctx.db.write(work_store.claim, aweme_id, 0, context.to_dict())
    token = current_progress.set(ReportingProgress(on_file))
    result: Optional[WorkResult] = None
    try:
        await ctx.tasks.wait_unpaused()
        result = await ctx.downloader.download(
            WorkRequest(aweme_id, context, payload=payload, fresh=fresh))
    except LoginRequired as exc:
        await ctx.db.write(work_store.release, [aweme_id])
        return FAILED, explain(exc)
    except BaseException:
        await ctx.db.write(work_store.release, [aweme_id])
        raise
    finally:
        current_progress.reset(token)
    if result.outcome == SUCCESS:
        await ctx.db.write(work_store.record_success, aweme_id, files=result.files,
                           extras_missing=result.extras_missing, context=context.to_dict(),
                           origin=0, payload=result.payload)
        return DOWNLOADED, ""

    def record(db) -> None:
        work_store.record_failure(db, aweme_id, error=result.error,
                                  permanent=result.outcome != "retry", attempts=result.attempt,
                                  code=result.code)
        if result.code == "INACCESSIBLE":
            work_store.set_visibility(db, aweme_id, "hidden", result.error)

    await ctx.db.write(record)
    return FAILED, result.error
