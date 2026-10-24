"""Download one work — the single implementation every feature uses.

A work is *complete* when its media (the video, or every gallery picture and
live-photo clip) and its JSON are saved.  The cover and a gallery's
soundtrack are best-effort extras: when missing they are reported in
``extras_missing`` and retried later at low priority, never blocking the
work.  Videos carry their own soundtrack; no separate music file is saved.

Signed media URLs expire within hours, so every attempt starts from a fresh
detail response (unless the caller just fetched one).  When a media
download still fails, the detail is fetched again through the logged-in
browser page before the attempt gives up.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from common.fs import nonempty_file
from common.log import get_logger
from douyin import aweme as aw
from douyin.client import DouyinClient
from douyin.errors import DouyinError, LoginRequired
from engine import live_photo
from engine.audio import AudioError, transcode_to_mp3
from engine.fetcher import FetchError, MediaFetcher
from storage.files import DownloadContext, Layout

logger = get_logger(__name__)

SUCCESS, RETRY, PERMANENT = "success", "retry", "permanent"
MAX_ATTEMPTS = 6
_BROWSER_DETAIL_TIMEOUT = 45.0
_THROTTLED_COOLDOWN = 5.0


def retry_delay(attempt: int) -> int:
    """1 min, 4 min, 16 min, ~1 h, ~4 h, then at most 6 h between attempts."""
    return min(6 * 3600, 60 * 4 ** max(0, int(attempt) - 1))


@dataclass
class WorkRequest:
    aweme_id: str
    context: DownloadContext
    payload: Dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    fresh: bool = False                 # ``payload`` is a detail fetched moments ago
    extras_only: Tuple[str, ...] = ()   # the work is complete; only these are due
    quality: str = ""                   # overrides the configured quality
    replace_media: bool = False         # re-download even if media exists (quality upgrade)


@dataclass
class WorkResult:
    aweme_id: str
    outcome: str
    error: str = ""
    code: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    extras_missing: Tuple[str, ...] = ()
    payload: Dict[str, Any] = field(default_factory=dict)
    media_type: str = ""
    attempt: int = 1
    folder: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.outcome == SUCCESS


class _MediaFailed(Exception):
    pass


def _file(path: Path, kind: str, position: int = 0) -> Dict[str, Any]:
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    return {"path": str(path), "kind": kind, "position": position, "size": size}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _existing(folder: Path, prefix: str) -> Optional[Path]:
    """A non-empty file whose name starts with ``prefix`` (any extension)."""
    try:
        for path in sorted(folder.iterdir()):
            if path.name.startswith(prefix) and nonempty_file(path) \
                    and not path.name.endswith((".fetch", ".tmp")):
                return path
    except OSError:
        return None
    return None


def _drop_siblings(folder: Path, prefix: str, keep: Path) -> None:
    """Delete other files named ``prefix*`` (a re-download may change the extension)."""
    try:
        for path in folder.iterdir():
            if path.name.startswith(prefix) and path != keep                     and not path.name.endswith((".fetch", ".tmp")):
                path.unlink(missing_ok=True)
    except OSError:
        pass


class WorkDownloader:
    def __init__(self, client: DouyinClient, fetcher: MediaFetcher, *,
                 layout: Callable[[], Layout], quality: Callable[[], str],
                 merge_live_photos: Callable[[], bool], browser: Any = None):
        self.client = client
        self.fetcher = fetcher
        self.layout = layout
        self.quality = quality
        self.merge_live_photos = merge_live_photos
        self.browser = browser
        self._locks: Dict[str, asyncio.Lock] = {}
        # The browser opens one page at a time: queue for it before taking an
        # API slot, so waiting callers do not hold slots the scans need.
        self._browser_turn = asyncio.Lock()

    def lock(self, aweme_id: str) -> asyncio.Lock:
        """Serializes every download of one work across features."""
        return self._locks.setdefault(str(aweme_id), asyncio.Lock())

    # ── detail ──────────────────────────────────────────────────────

    async def _browser_detail(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        if self.browser is None or not getattr(self.browser, "enabled", False):
            return None
        try:
            async with self._browser_turn:
                if getattr(self.browser, "cooldown_remaining", lambda: 0.0)() > 0:
                    return None
                async with self.client.slot():
                    return await asyncio.wait_for(self.browser.work_detail(aweme_id),
                                                  _BROWSER_DETAIL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Browser detail failed for %s: %s", aweme_id, exc)
            return None

    async def fetch_detail(self, aweme_id: str, fallback: Optional[Dict[str, Any]] = None,
                           *, gentle: bool = False
                           ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """``(payload, reason, kind)``; kind ``inaccessible`` means Douyin said no."""
        fallback = fallback or {}
        reason, kind = "", ""
        throttled = self.client.detail_cooldown() > _THROTTLED_COOLDOWN
        if not throttled:
            try:
                detail, reason, kind = await (self.client.aweme_detail(aweme_id, gentle=True)
                                              if gentle else self.client.aweme_detail(aweme_id))
            except LoginRequired:
                raise
            except (DouyinError, OSError) as exc:
                detail, reason, kind = None, str(exc), "error"
            if detail is not None:
                return aw.merge_detail(detail, fallback), "", ""
        # The logged-in page may still see works the web API filters
        # (e.g. followers-only), and has its own signature budget.
        detail = await self._browser_detail(aweme_id)
        if detail is not None:
            return aw.merge_detail(detail, fallback), "", ""
        if throttled or (kind != "inaccessible"
                         and self.client.detail_cooldown() > _THROTTLED_COOLDOWN):
            return None, "详情接口风控冷却中，浏览器也未取到详情", "throttled"
        return None, reason or "作品详情为空", kind or "empty"

    async def _full_size_gallery(self, aweme_id: str, payload: Dict[str, Any], fresh: bool
                                 ) -> Dict[str, Any]:
        """Re-read a gallery whose clean pictures came scaled down.

        The scaled copies go to some device ids only: after renewing the id
        (or when the payload predates a renewal) a fresh detail usually
        lists the full-size clean pictures.
        """
        renewed = await self.client.renew_device_id()
        if not renewed and fresh:
            return payload
        detail, _reason, _kind = await self.fetch_detail(aweme_id, payload)
        if detail is None or aw.media_type(detail) != "gallery":
            return payload
        if aw.scaled_gallery(detail):
            logger.info("Gallery %s still lists scaled pictures", aweme_id)
        return detail

    def detail_failure(self, request: WorkRequest, reason: str, kind: str) -> WorkResult:
        """The result for a work whose detail could not be fetched."""
        aweme_id = request.aweme_id
        if request.extras_only:
            return WorkResult(aweme_id, SUCCESS, reason, extras_missing=request.extras_only,
                              attempt=request.attempt)
        if kind == "throttled":
            # Risk control, not the work: this attempt does not count.
            return WorkResult(aweme_id, RETRY, reason, code="THROTTLED",
                              attempt=max(0, request.attempt - 1))
        if kind == "inaccessible":
            # Confirm once more: risk control occasionally answers a
            # public work with a filter reason.
            final = request.attempt >= 2
            return WorkResult(aweme_id, PERMANENT if final else RETRY,
                              f"作品不可访问：{reason}", code="INACCESSIBLE",
                              attempt=request.attempt)
        return WorkResult(aweme_id, RETRY, f"获取作品详情失败：{reason}",
                          attempt=request.attempt)

    # ── one attempt ─────────────────────────────────────────────────

    async def download(self, request: WorkRequest) -> WorkResult:
        """Download ``request``; exceptions become retryable results."""
        async with self.lock(request.aweme_id):
            try:
                return await self._download(request)
            except (asyncio.CancelledError, LoginRequired):
                raise
            except Exception as exc:
                logger.exception("Work %s failed", request.aweme_id)
                return WorkResult(request.aweme_id, RETRY, f"{type(exc).__name__}: {exc}",
                                  attempt=request.attempt)

    async def _download(self, request: WorkRequest) -> WorkResult:
        aweme_id = request.aweme_id
        if str(request.payload.get("media_type") or "") == "music":
            return await self._music_track(request)
        if request.fresh and aw.is_complete(request.payload):
            payload, reason, kind = request.payload, "", ""
        else:
            payload, reason, kind = await self.fetch_detail(aweme_id, request.payload)
        if payload is None:
            return self.detail_failure(request, reason, kind)

        quality = request.quality or self.quality()
        media_type = aw.media_type(payload)
        if media_type == "gallery" and aw.scaled_gallery(payload):
            payload = await self._full_size_gallery(aweme_id, payload, request.fresh)
        owner = aw.author(payload)
        author_name = str(owner.get("nickname") or request.context.source_name or "未知作者")
        folder, stem = self.layout().work_target(
            request.context, aweme_id=aweme_id, title=aw.title(payload), author=author_name,
            author_sec_uid=aw.author_sec_uid(payload), create_time=aw.create_time(payload),
            media_type=media_type)
        folder.mkdir(parents=True, exist_ok=True)
        wanted = ("cover", "music") if media_type == "gallery" else ("cover",)
        result = WorkResult(aweme_id, SUCCESS, payload=payload, media_type=media_type,
                            attempt=request.attempt, folder=folder)

        if request.extras_only:
            saved = await self._extras(request.extras_only, payload, media_type, folder, stem,
                                       quality)
            result.files = [entry for value in saved.values()
                            for entry in (value if isinstance(value, list) else [value])]
            result.extras_missing = tuple(k for k in request.extras_only if k not in saved)
            return result

        media_job = asyncio.ensure_future(self._media_with_refresh(
            aweme_id, payload, media_type, folder, stem, quality, request.replace_media))
        extras = await self._extras(wanted, payload, media_type, folder, stem, quality)
        try:
            media_files, payload = await media_job
        except _MediaFailed as exc:
            result.outcome = PERMANENT if request.attempt >= MAX_ATTEMPTS else RETRY
            result.error = str(exc)
            result.files = list(extras.values())
            return result
        json_path = folder / f"{stem}_data.json"
        await asyncio.to_thread(_write_json, json_path,
                                self._backup_json(request, payload, media_type))
        result.payload = payload
        result.files = [*media_files, *extras.values(), _file(json_path, "json")]
        result.extras_missing = tuple(kind for kind in wanted if kind not in extras)
        return result

    @staticmethod
    def _backup_json(request: WorkRequest, payload: Dict[str, Any], media_type: str
                     ) -> Dict[str, Any]:
        data = dict(payload)
        data["_backup"] = {
            "aweme_id": request.aweme_id, "source": request.context.to_dict(),
            "desc": aw.title(payload), "media_type": media_type,
            "publish_time": aw.publish_date(aw.create_time(payload)),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        return data

    # ── media ───────────────────────────────────────────────────────

    async def _media_with_refresh(self, aweme_id, payload, media_type, folder, stem, quality,
                                  replace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        try:
            return await self._media(payload, media_type, folder, stem, quality, replace), payload
        except _MediaFailed as first:
            logger.info("Media failed for %s (%s); refreshing via browser", aweme_id, first)
            refreshed = await self._browser_detail(aweme_id)
            if refreshed is None:
                raise
            refreshed = aw.merge_detail(refreshed, payload)
            try:
                return (await self._media(refreshed, media_type, folder, stem, quality, replace),
                        refreshed)
            except _MediaFailed as second:
                raise _MediaFailed(f"{second}（浏览器刷新后仍失败）") from second

    async def _media(self, payload, media_type, folder, stem, quality, replace
                     ) -> List[Dict[str, Any]]:
        if media_type == "gallery":
            return await self._gallery(payload, folder, stem, quality, replace)
        return [await self._video(payload, folder, stem, quality, replace)]

    async def _video(self, payload, folder: Path, stem: str, quality: str, replace: bool
                     ) -> Dict[str, Any]:
        dest = folder / f"{stem}.mp4"
        if nonempty_file(dest) and not replace:
            return _file(dest, "media")
        candidates = aw.video_candidates(payload, self.client, quality)
        if quality == "original":
            video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
            original = aw.original_video_candidate(video, self.client, quality)
            if original:
                size = await self.client.probe_size(*original)
                compressed = aw.estimated_video_bytes(payload, "highest")
                if size > 0 and size >= compressed:
                    candidates.insert(0, original)
        if not candidates:
            raise _MediaFailed("作品没有可用的视频地址")
        target = dest.with_name(f"{dest.stem}.new{dest.suffix}") if replace else dest
        try:
            saved = await self.fetcher.fetch(candidates, target)
        except FetchError as exc:
            raise _MediaFailed(f"视频下载失败：{exc}") from exc
        if replace:
            os.replace(saved, dest)
            saved = dest
        return _file(saved, "media")

    async def _gallery(self, payload, folder: Path, stem: str, quality: str, replace: bool
                       ) -> List[Dict[str, Any]]:
        items = aw.gallery_items(payload)
        assets = aw.gallery_assets(payload, quality)
        if not items or len(assets) != len(items):
            raise _MediaFailed("图文作品的图片列表不完整")
        headers = self.client.media_headers()
        stills = [(index, urls) for index, urls, _clips in assets if urls]
        clips = [(index, urls) for index, _urls, urls in assets if urls]

        async def still(number: int, urls: Sequence[str]) -> Dict[str, Any]:
            existing = None if replace else _existing(folder, f"{stem}_{number}.")
            if existing is None:
                dest = folder / f"{stem}_{number}{aw.image_ext(urls[0])}"
                existing = await self.fetcher.fetch([(url, headers) for url in urls], dest,
                                                    segmented=False, image_suffix=True)
                if replace:
                    _drop_siblings(folder, f"{stem}_{number}.", existing)
            return _file(existing, "media", number)

        async def clip(number: int, urls: Sequence[str]) -> Dict[str, Any]:
            existing = None if replace else _existing(folder, f"{stem}_live_{number}.mp4")
            if existing is None:
                candidates = [self.client.sign_play_candidate(url)
                              if urlparse(url).netloc.endswith("douyin.com")
                              else (url, headers)
                              for url in urls]
                existing = await self.fetcher.fetch(candidates,
                                                    folder / f"{stem}_live_{number}.mp4")
            return _file(existing, "live", number)

        try:
            still_files = await asyncio.gather(*(
                still(number, urls) for number, (_index, urls) in enumerate(stills, start=1)))
            clip_files = await asyncio.gather(*(
                clip(number, urls) for number, (_index, urls) in enumerate(clips, start=1)))
        except FetchError as exc:
            raise _MediaFailed(f"图片下载失败：{exc}") from exc
        if not still_files and not clip_files:
            raise _MediaFailed("图文作品没有可下载的图片")
        motions = await self._motion_photos(stills, clips, still_files, clip_files, folder, stem,
                                            rebuild=replace)
        return [*still_files, *clip_files, *motions]

    async def _fresh_stills(self, payload, folder: Path, stem: str, quality: str
                            ) -> List[Dict[str, Any]]:
        """Fetch every gallery picture again (repair of watermarked copies).

        Live clips already saved are kept; their motion photos are rebuilt
        from the new pictures.
        """
        assets = aw.gallery_assets(payload, quality)
        stills = [(index, urls) for index, urls, _clips in assets if urls]
        clips = [(index, urls) for index, _urls, urls in assets if urls]
        if not stills:
            raise RuntimeError("图文作品没有图片地址")
        headers = self.client.media_headers()

        async def still(number: int, urls: Sequence[str]) -> Dict[str, Any]:
            dest = folder / f"{stem}_{number}{aw.image_ext(urls[0])}"
            saved = await self.fetcher.fetch([(url, headers) for url in urls], dest,
                                             segmented=False, image_suffix=True)
            _drop_siblings(folder, f"{stem}_{number}.", saved)
            return _file(saved, "media", number)

        still_files = list(await asyncio.gather(*(
            still(number, urls) for number, (_index, urls) in enumerate(stills, start=1))))
        clip_paths = [_existing(folder, f"{stem}_live_{number}.mp4")
                      for number in range(1, len(clips) + 1)]
        motions: List[Dict[str, Any]] = []
        if clip_paths and all(clip_paths):
            clip_files = [_file(path, "live", number)
                          for number, path in enumerate(clip_paths, start=1)]
            motions = await self._motion_photos(stills, clips, still_files, clip_files, folder,
                                                stem, rebuild=True)
        return [*still_files, *motions]

    async def _motion_photos(self, stills, clips, still_files, clip_files, folder, stem,
                             rebuild: bool = False) -> List[Dict[str, Any]]:
        if not clip_files or not self.merge_live_photos():
            return []
        still_at = {index: Path(entry["path"]) for (index, _), entry in zip(stills, still_files)}
        merged = []
        for number, ((index, _urls), entry) in enumerate(zip(clips, clip_files), start=1):
            still = still_at.get(index)
            if still is None:
                continue
            target = folder / f"{stem}_live_{number}.jpg"
            if rebuild or not nonempty_file(target):
                made = await asyncio.to_thread(live_photo.merge_live_photo_files, still,
                                               Path(entry["path"]), target)
                if not made:
                    continue
            merged.append(_file(target, "motion", number))
        return merged

    # ── extras ──────────────────────────────────────────────────────

    async def _extras(self, kinds, payload, media_type, folder, stem, quality
                      ) -> Dict[str, Any]:
        jobs = {}
        if "cover" in kinds:
            jobs["cover"] = self._cover(payload, folder, stem, quality)
        elif "cover_fix" in kinds:
            # Repair of a cover saved from a worse source: fetch it again.
            jobs["cover_fix"] = self._cover(payload, folder, stem, quality, replace=True)
        if "music" in kinds and media_type == "gallery":
            jobs["music"] = self._soundtrack(payload, folder, stem)
        if "watermark_fix" in kinds and media_type == "gallery":
            jobs["watermark_fix"] = self._fresh_stills(payload, folder, stem, quality)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        saved = {}
        for kind, outcome in zip(jobs, results):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, (dict, list)):
                saved[kind] = outcome
            elif isinstance(outcome, BaseException):
                logger.info("Optional %s skipped for %s: %s", kind, payload.get("aweme_id"),
                            outcome)
        if "cover_fix" in kinds and "cover" in saved:
            saved["cover_fix"] = saved["cover"]      # a fresh cover is the fix
        if "watermark_fix" in kinds and media_type != "gallery":
            saved["watermark_fix"] = []              # no pictures to repair any more
        return saved

    async def _cover(self, payload, folder: Path, stem: str, quality: str,
                     replace: bool = False) -> Dict[str, Any]:
        existing = _existing(folder, f"{stem}_cover.")
        if existing is None or replace:
            urls = aw.cover_urls(payload, quality)
            if not urls:
                raise RuntimeError("作品没有封面地址")
            # A replacement lands beside the old cover and swaps in only once
            # complete, so a failed repair keeps the old one.
            dest = folder / (f"{stem}_cover_new.jpg" if existing else f"{stem}_cover.jpg")
            # One URL per fetch, in order: the fetcher ranks candidates by host
            # speed, and a faster host must not swap the author's cover for
            # ``origin_cover`` (the video's first frame, often blank).
            headers = self.client.media_headers()
            last: Optional[FetchError] = None
            for url in urls:
                try:
                    fetched = await self.fetcher.fetch([(url, headers)], dest,
                                                       segmented=False, image_suffix=True)
                    break
                except FetchError as exc:
                    last = exc
            else:
                raise last or RuntimeError("作品没有封面地址")
            if existing is not None:
                final = folder / f"{stem}_cover{fetched.suffix}"
                os.replace(fetched, final)
                if existing != final:
                    existing.unlink(missing_ok=True)
                fetched = final
            existing = fetched
        return _file(existing, "cover")

    async def _soundtrack(self, payload, folder: Path, stem: str) -> Dict[str, Any]:
        dest = folder / f"{stem}_music.mp3"
        if nonempty_file(dest):
            return _file(dest, "music")
        urls = aw.music_urls(payload)
        if not urls:
            raise RuntimeError("作品没有配乐地址")
        return _file(await self.save_audio(urls, dest), "music")

    async def save_audio(self, urls: Sequence[str], dest: Path) -> Path:
        """Download the first working audio URL and store it as MP3."""
        headers = self.client.media_headers()
        last: Optional[BaseException] = None
        for index, url in enumerate(urls):
            suffix = Path(url.split("?", 1)[0]).suffix.lower()
            suffix = suffix if suffix[1:].isalnum() and len(suffix) <= 6 else ".bin"
            source = dest.with_name(f"{dest.name}.{index}.source{suffix}")
            try:
                saved = await self.fetcher.fetch([(url, headers)], source, segmented=False)
                if suffix == ".mp3":
                    os.replace(saved, dest)
                else:
                    await transcode_to_mp3(saved, dest)
                return dest
            except (FetchError, AudioError) as exc:
                last = exc
            finally:
                for leftover in dest.parent.glob(f"{dest.name}.{index}.source*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        raise RuntimeError(f"配乐下载失败：{last}")

    # ── a music track (from a music link) ───────────────────────────

    async def _music_track(self, request: WorkRequest) -> WorkResult:
        payload = request.payload
        music_id = str(payload.get("music_id") or request.aweme_id.replace("music_", ""))
        urls = [url for url in [str(payload.get("music_url") or "")] if url] \
            or aw.music_urls(payload)
        if not urls:
            detail = await self.client.music_detail(music_id)
            urls = aw.music_info({"music": detail or {}})[0]
        if not urls:
            return WorkResult(request.aweme_id, PERMANENT, "音乐没有可下载的地址",
                              attempt=request.attempt)
        title = str(payload.get("desc") or payload.get("title") or f"music_{music_id}")
        folder, stem = self.layout().work_target(
            request.context, aweme_id=f"music_{music_id}", title=title,
            author=str(aw.author(payload).get("nickname") or ""), author_sec_uid="",
            create_time=0, media_type="music")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{stem}.mp3"
        try:
            if not nonempty_file(dest):
                await self.save_audio(urls, dest)
        except Exception as exc:
            return WorkResult(request.aweme_id, RETRY, str(exc), attempt=request.attempt)
        json_path = folder / f"{stem}_data.json"
        await asyncio.to_thread(_write_json, json_path, dict(payload))
        return WorkResult(request.aweme_id, SUCCESS, files=[_file(dest, "media"),
                                                            _file(json_path, "json")],
                          payload=payload, media_type="music", attempt=request.attempt,
                          folder=folder)
