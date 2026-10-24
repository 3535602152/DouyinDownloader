"""The local library: files on disk, their validation, history and upgrades."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from services.context import Context
from storage import diagnostics
from storage import sources as source_store
from storage import works as work_store
from storage.db import chunks, marks


def _remove(paths: Sequence[str]) -> Tuple[int, int, Set[Path]]:
    deleted = failed = 0
    folders: Set[Path] = set()
    for text in paths:
        path = Path(text)
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            failed += 1
            continue
        folders.add(path.parent)
    return deleted, failed, folders


def _prune(folders: Set[Path], stop: Path) -> None:
    """Remove folders the deletion emptied (never ``stop`` or above)."""
    for folder in sorted(folders, key=lambda value: len(value.parts), reverse=True):
        current = folder
        while current != stop and stop in current.parents:
            try:
                leftovers = [entry for entry in current.iterdir()
                             if entry.name not in (".author.json",)]
                if leftovers:
                    break
                for entry in current.iterdir():
                    entry.unlink()
                current.rmdir()
            except OSError:
                break
            current = current.parent


class LibraryService:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    # ── deleting a source's files ───────────────────────────────────

    async def delete_source_files(self, source_ids: Sequence[int],
                                  aweme_ids: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """Delete files these sources own, unless another source still lists the work.

        A work's files belong to the source whose download placed them
        (``downloads.origin``).  Works saved by another source or by a manual
        download stay; so do works another followed source still lists.
        """
        ids = [int(value) for value in source_ids]

        def collect(db) -> Tuple[List[str], Dict[str, List[str]]]:
            candidates = list(aweme_ids) if aweme_ids is not None \
                else source_store.members(db, ids)
            shared = source_store.other_listings(db, candidates, ids)
            owned: List[str] = []
            for batch in chunks([value for value in candidates if value not in shared]):
                owned += [str(row[0]) for row in db.execute(
                    f"SELECT aweme_id FROM downloads WHERE aweme_id IN ({marks(batch)}) "
                    f"AND origin IN ({marks(ids)})", (*batch, *ids))]
            files = work_store.files_of(db, owned)
            return owned, {key: [entry["path"] for entry in value] for key, value in files.items()}

        owned, files = await self.ctx.db.read(collect)
        paths = [path for values in files.values() for path in values]
        deleted, failed, folders = await asyncio.to_thread(_remove, paths)
        await asyncio.to_thread(_prune, folders, self.ctx.settings.download_root)
        if owned:
            await self.ctx.db.write(work_store.forget_downloads, owned)
        return {"works_deleted": len(owned), "files_deleted": deleted, "files_failed": failed}

    # ── validation ──────────────────────────────────────────────────

    async def validate(self, aweme_ids: Optional[Sequence[str]] = None,
                       progress: Callable[[int, int], None] = lambda _d, _t: None) -> List[str]:
        """Queue again every finished work whose media left the disk."""
        rows = await self.ctx.db.read(work_store.done_media, aweme_ids)
        by_work: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_work.setdefault(str(row["aweme_id"]), []).append(row)
        total = len(by_work)
        lost: List[str] = []
        stale: List[str] = []

        def check(entries: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
            media = [entry for entry in entries if entry.get("kind") in ("media", "live")]
            missing = [entry["path"] for entry in entries
                       if entry.get("path") and not os.path.isfile(entry["path"])]
            broken = not media or any(entry["path"] in missing for entry in media)
            return broken, missing

        for index, (aweme_id, entries) in enumerate(by_work.items(), start=1):
            broken, missing = await asyncio.to_thread(check, entries)
            if broken:
                lost.append(aweme_id)
            stale.extend(missing)
            if index % 50 == 0 or index == total:
                progress(index, total)
        if stale:
            await self.ctx.db.write(work_store.remove_files, stale)
        if lost:
            await self.ctx.db.write(work_store.requeue, lost, include_done=True,
                                    error="本地文件缺失，重新下载")
            self.ctx.queue.wake()
        return lost

    # ── history & diagnostics ───────────────────────────────────────

    async def history(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        return await self.ctx.db.read(
            work_store.history, page=max(1, int(filters.get("page") or 1)),
            size=min(100, max(1, int(filters.get("size") or 50))),
            author=str(filters.get("author") or ""), title=str(filters.get("title") or ""),
            media_type=str(filters.get("type") or ""))

    async def work_details(self, aweme_id: str) -> Dict[str, Any]:
        return await self.ctx.db.read(work_store.get_work, str(aweme_id)) or {}

    async def record_risk(self, event: Dict[str, Any]) -> None:
        await self.ctx.db.write(diagnostics.record, event)

    async def risk_report(self) -> Dict[str, Any]:
        return await self.ctx.db.read(diagnostics.report)
