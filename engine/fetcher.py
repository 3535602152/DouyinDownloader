"""Fast CDN downloads for creator monitoring.

Douyin's media CDN throttles every HTTP connection (about 1.5-2 MB/s in
practice) and a ``url_list`` often mixes a healthy mirror with one that is an
order of magnitude slower.  Downloading one file over one connection from the
first listed mirror therefore wastes almost all of the available bandwidth.

``MediaFetcher`` keeps the fast path simple and bounded:

* mirrors are ranked by the throughput observed per host;
* a large file is split into byte ranges fetched over several connections;
* a range that crawls on a slow mirror continues on a faster one;
* every request holds one slot of a global connection budget shared by all
  works in flight, so concurrency never grows with the queue length.

Bytes land in a ``.fetch`` staging file and only an exactly-sized, non-HTML
payload is renamed onto the destination.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import aiohttp
from yarl import URL

from douyin.aweme import is_watermarked

Candidate = Tuple[str, Dict[str, str]]

_CHUNK_BYTES = 256 * 1024
_WRITE_BUFFER_BYTES = 1024 * 1024
_IMAGE_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_ERROR_CONTENT_TYPES = frozenset(
    {"text/html", "text/plain", "text/xml", "application/json", "application/xml"}
)


class TransferProgress:
    """Bytes of one work across all of its files, for the download panel.

    ``MediaFetcher`` reports into the progress bound to ``current_progress``
    in the calling task; tasks spawned inside it inherit the binding.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: Dict[str, List[int]] = {}  # staging path -> [done, total]
        self._samples: deque = deque()

    def start_file(self, key: str, total: int) -> None:
        with self._lock:
            self._files[key] = [0, max(0, int(total or 0))]

    def add(self, key: str, nbytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            entry = self._files.setdefault(key, [0, 0])
            entry[0] += nbytes
            self._samples.append((now, nbytes))
            while self._samples and self._samples[0][0] < now - 5:
                self._samples.popleft()

    def snapshot(self) -> Dict[str, float]:
        now = time.monotonic()
        with self._lock:
            done = sum(min(d, t) if t else d for d, t in self._files.values())
            total = sum(max(d, t) for d, t in self._files.values())
            recent = [(at, size) for at, size in self._samples if at >= now - 3]
        rate = sum(size for _, size in recent) / 3.0 if recent else 0.0
        return {"done": done, "total": total, "rate": rate}


current_progress: contextvars.ContextVar = contextvars.ContextVar(
    "media_fetch_progress", default=None
)


class FetchError(RuntimeError):
    """Every candidate failed; ``statuses`` holds the HTTP codes seen."""

    def __init__(self, message: str, statuses: Iterable[int] = ()):
        super().__init__(message)
        self.statuses = tuple(int(status) for status in statuses if status)

    @property
    def link_expired(self) -> bool:
        """403/404/410 usually mean a stale signed URL, not a dead work."""
        return any(status in (403, 404, 410) for status in self.statuses)


class _CandidateFailed(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class _RangeFailed(Exception):
    def __init__(self, resume_at: int, message: str, status: int = 0, *, bad_mirror=False):
        super().__init__(message)
        self.resume_at = resume_at
        self.status = status
        self.bad_mirror = bad_mirror


class _SlowMirror(Exception):
    def __init__(self, resume_at: int):
        super().__init__("mirror too slow")
        self.resume_at = resume_at


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def _content_type(response) -> str:
    return str(response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()


def _range_total(response) -> Optional[Tuple[int, int, int]]:
    value = str(response.headers.get("Content-Range", "") or "").strip()
    if not value.startswith("bytes ") or "/" not in value:
        return None
    span, _, total = value[6:].partition("/")
    first, _, last = span.partition("-")
    try:
        return int(first), int(last), int(total)
    except ValueError:
        return None


def exact_url(url: str) -> URL:
    """``url`` sent byte for byte.

    yarl normalises a URL by default and decodes ``%2F`` in the query, which
    breaks the base64 ``x-signature`` of CDN links: the clean copy answers
    403 and the download falls back to the watermarked one.
    A URL that is not already encoded (spaces, non-ASCII) is encoded as usual.
    """
    if not url.isascii() or " " in url:
        return URL(url)
    return URL(url, encoded=True)


class HostStats:
    """Exponentially weighted throughput and failure count per CDN host."""

    DEFAULT_RATE = 1_000_000.0
    MIN_SAMPLE_BYTES = 256 * 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rates: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}

    def record(self, host: str, nbytes: int, seconds: float) -> None:
        if not host or nbytes < self.MIN_SAMPLE_BYTES or seconds <= 0.05:
            return
        rate = nbytes / seconds
        with self._lock:
            previous = self._rates.get(host)
            self._rates[host] = rate if previous is None else previous * 0.6 + rate * 0.4
            if self._failures.get(host):
                self._failures[host] -= 1

    def fail(self, host: str) -> None:
        if host:
            with self._lock:
                self._failures[host] = min(10, self._failures.get(host, 0) + 1)

    def score(self, host: str) -> float:
        with self._lock:
            rate = self._rates.get(host, self.DEFAULT_RATE)
            failures = self._failures.get(host, 0)
        return rate / (1 + 2 * failures)

    def rank(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        """Fastest mirror first, but never a watermarked copy before a clean one.

        Gallery pictures and videos list clean URLs first and the "抖音号"
        watermarked copies (``tplv-dy-water`` …) last, often on other hosts;
        speed alone must not promote a watermarked copy.
        """
        order = sorted(
            enumerate(candidates),
            key=lambda pair: (is_watermarked(pair[1][0]), -self.score(_host(pair[1][0])),
                              pair[0]),
        )
        return [candidate for _, candidate in order]


class _PartFile:
    """Positional writes to one staging file from several range workers."""

    def __init__(self, path: Path, size: int, executor: ThreadPoolExecutor):
        self.path = path
        self._executor = executor
        self._lock = threading.Lock()
        flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        self._fd = os.open(str(path), flags, 0o644)
        if size > 0:
            os.ftruncate(self._fd, size)

    def _write(self, offset: int, data: bytes) -> None:
        with self._lock:
            os.lseek(self._fd, offset, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]

    async def write(self, offset: int, data: bytes) -> None:
        if data:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._write, offset, data
            )

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class _Mirror:
    __slots__ = ("url", "headers", "host", "bad")

    def __init__(self, url: str, headers: Dict[str, str]):
        self.url = url
        self.headers = headers
        self.host = _host(url)
        self.bad = False


class MediaFetcher:
    """Shared, bounded downloader used by every monitor work in flight."""

    SLOW_GRACE_SECONDS = 4.0
    SLOW_BYTES_PER_SECOND = 350 * 1024
    MAX_RANGE_FAILURES = 8

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        connections: int = 24,
        max_segments: int = 4,
        segment_bytes: int = 8 * 1024 * 1024,
        proxy: Optional[str] = None,
        stats: Optional[HostStats] = None,
        io_workers: int = 4,
    ) -> None:
        self.session = session
        self.proxy = proxy or None
        self.max_segments = max(1, int(max_segments))
        self.segment_bytes = max(512 * 1024, int(segment_bytes))
        self.connections = max(1, int(connections))
        self.stats = stats or HostStats()
        self._slots = asyncio.Semaphore(self.connections)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(io_workers)), thread_name_prefix="media-fetch-io"
        )
        self._timeout = aiohttp.ClientTimeout(
            total=None, connect=10, sock_connect=10, sock_read=20
        )
        self.active_connections = 0
        self.bytes_total = 0
        self._meter: deque = deque()

    # ── public surface ───────────────────────────────────────────────

    def rate(self, window: float = 5.0) -> float:
        """Bytes per second written during the last ``window`` seconds."""
        now = time.monotonic()
        while self._meter and self._meter[0][0] < now - window:
            self._meter.popleft()
        return sum(size for _, size in self._meter) / window

    def close(self) -> None:
        self._executor.shutdown(wait=False)

    async def fetch(
        self,
        candidates: Sequence[Candidate],
        dest: Path,
        *,
        segmented: bool = True,
        image_suffix: bool = False,
    ) -> Path:
        """Download the first working candidate to ``dest`` and return the path.

        ``image_suffix`` renames the file after the served image type, since
        Douyin serves webp/jpeg variants behind one ``.jpeg`` URL.
        """
        unique: List[Candidate] = []
        seen = set()
        for url, headers in candidates:
            url = str(url or "").strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                unique.append((url, dict(headers or {})))
        if not unique:
            raise FetchError(f"no download source for {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        statuses: List[int] = []
        last_error = ""
        for url, headers in self.stats.rank(unique):
            try:
                return await self._fetch_candidate(
                    url, headers, unique, dest,
                    segmented=segmented, image_suffix=image_suffix,
                )
            except _CandidateFailed as exc:
                self.stats.fail(_host(url))
                statuses.append(exc.status)
                last_error = str(exc)
        raise FetchError(
            f"{dest.name}: all {len(unique)} source(s) failed ({last_error})", statuses
        )

    # ── internals ────────────────────────────────────────────────────

    async def _slot(self):
        await self._slots.acquire()
        self.active_connections += 1

    def _release(self) -> None:
        self.active_connections -= 1
        self._slots.release()

    def _count(self, nbytes: int, key: str = "") -> None:
        self.bytes_total += nbytes
        self._meter.append((time.monotonic(), nbytes))
        progress = current_progress.get()
        if progress is not None and key:
            progress.add(key, nbytes)

    def _has_faster(self, mirrors: Sequence[_Mirror], current: _Mirror, rate: float) -> bool:
        return any(
            mirror is not current and not mirror.bad
            and self.stats.score(mirror.host) > max(rate * 3, self.SLOW_BYTES_PER_SECOND)
            for mirror in mirrors
        )

    async def _fetch_candidate(
        self,
        url: str,
        headers: Dict[str, str],
        all_candidates: Sequence[Candidate],
        dest: Path,
        *,
        segmented: bool,
        image_suffix: bool,
    ) -> Path:
        part = dest.with_name(dest.name + ".fetch")
        part_file: Optional[_PartFile] = None
        committed = False
        request_headers = dict(headers)
        if segmented:
            request_headers["Range"] = f"bytes=0-{self.segment_bytes - 1}"
        try:
            await self._slot()
            try:
                started = time.monotonic()
                async with self.session.get(
                    exact_url(url), headers=request_headers, proxy=self.proxy, timeout=self._timeout
                ) as response:
                    status = response.status
                    if status not in (200, 206):
                        raise _CandidateFailed(f"HTTP {status}", status)
                    kind = _content_type(response)
                    if kind in _ERROR_CONTENT_TYPES:
                        raise _CandidateFailed(f"served {kind} instead of media", status)
                    final = dest
                    if image_suffix and kind in _IMAGE_SUFFIXES:
                        final = dest.with_suffix(_IMAGE_SUFFIXES[kind])
                    if status == 206:
                        span = _range_total(response)
                        if span is None or span[0] != 0:
                            raise _CandidateFailed("unusable Content-Range", status)
                        total = span[2]
                    else:
                        total = int(response.content_length or 0)
                    part_file = _PartFile(part, total, self._executor)
                    progress = current_progress.get()
                    if progress is not None:
                        # A new candidate restarts this file's count.
                        progress.start_file(str(part), total)
                    first = _Mirror(str(response.url), headers)
                    mirrors = [first] + [
                        _Mirror(other, dict(other_headers))
                        for other, other_headers in all_candidates
                        if other != url
                    ]
                    try:
                        resume_at = await self._stream(
                            response, part_file, 0, first, mirrors,
                            allow_switch=status == 206, check_head=True,
                        )
                    except _SlowMirror as slow:
                        resume_at = slow.resume_at
                        self.stats.fail(first.host)
                    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                        if status != 206:
                            raise _CandidateFailed(f"stream broke: {type(exc).__name__}")
                        # A ranged probe keeps its bytes; the range phase
                        # resumes the rest, preferably on another mirror.
                        resume_at = int(getattr(exc, "resume_at", 0))
                        self.stats.fail(first.host)
                    elapsed = time.monotonic() - started
                    self.stats.record(first.host, resume_at, elapsed)
                    self.stats.record(_host(url), resume_at, elapsed)
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                raise _CandidateFailed(f"{type(exc).__name__}: {exc}")
            finally:
                self._release()
            if status == 200:
                if total and resume_at != total:
                    raise _CandidateFailed(f"size mismatch {resume_at}/{total}")
                if resume_at <= 0:
                    raise _CandidateFailed("empty response")
            elif resume_at < total:
                await self._fetch_ranges(part_file, total, resume_at, mirrors)
            part_file.close()
            os.replace(str(part), str(final))
            committed = True
            return final
        finally:
            if part_file is not None:
                part_file.close()
            if not committed:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _stream(
        self,
        response,
        part_file: _PartFile,
        offset: int,
        mirror: _Mirror,
        mirrors: Sequence[_Mirror],
        *,
        allow_switch: bool,
        check_head: bool = False,
    ) -> int:
        """Copy a response body to ``offset``; return the next offset."""
        start = offset
        buffer = bytearray()
        buffer_at = offset
        began = time.monotonic()
        try:
            async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                if check_head and offset == start and not buffer:
                    if chunk.lstrip()[:1] in (b"<", b"{"):
                        raise _CandidateFailed("served a text page instead of media")
                buffer += chunk
                offset += len(chunk)
                self._count(len(chunk), str(part_file.path))
                if len(buffer) >= _WRITE_BUFFER_BYTES:
                    await part_file.write(buffer_at, bytes(buffer))
                    buffer_at, buffer = offset, bytearray()
                elapsed = time.monotonic() - began
                if (
                    allow_switch
                    and elapsed >= self.SLOW_GRACE_SECONDS
                    and (offset - start) / elapsed < self.SLOW_BYTES_PER_SECOND
                    and self._has_faster(mirrors, mirror, (offset - start) / elapsed)
                ):
                    await part_file.write(buffer_at, bytes(buffer))
                    raise _SlowMirror(offset)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
            await part_file.write(buffer_at, bytes(buffer))
            exc.resume_at = offset  # type: ignore[attr-defined]
            raise
        await part_file.write(buffer_at, bytes(buffer))
        return offset

    async def _fetch_ranges(
        self, part_file: _PartFile, total: int, start: int, mirrors: List[_Mirror]
    ) -> None:
        pending = deque()
        for first in range(start, total, self.segment_bytes):
            pending.append((first, min(total, first + self.segment_bytes) - 1))
        failures = 0
        error: Optional[BaseException] = None

        def best_mirror() -> Optional[_Mirror]:
            alive = [mirror for mirror in mirrors if not mirror.bad]
            if not alive:
                return None
            return max(alive, key=lambda mirror: self.stats.score(mirror.host))

        async def worker() -> None:
            nonlocal failures, error
            while pending and error is None:
                first, last = pending.popleft()
                mirror = best_mirror()
                if mirror is None:
                    error = _CandidateFailed("no usable mirror left")
                    return
                try:
                    await self._fetch_range(part_file, total, first, last, mirror, mirrors)
                except _SlowMirror as slow:
                    self.stats.fail(mirror.host)
                    if slow.resume_at <= last:
                        pending.appendleft((slow.resume_at, last))
                except _RangeFailed as failed:
                    failures += 1
                    self.stats.fail(mirror.host)
                    if failed.bad_mirror:
                        mirror.bad = True
                    if failed.resume_at <= last:
                        pending.append((failed.resume_at, last))
                    if failures > self.MAX_RANGE_FAILURES:
                        error = _CandidateFailed(f"range retries exhausted: {failed}", failed.status)
                        return

        workers = min(self.max_segments, max(1, len(pending)))
        await asyncio.gather(*(worker() for _ in range(workers)))
        if error is not None:
            raise error
        if pending:
            raise _CandidateFailed("ranges left unfinished")

    async def _fetch_range(
        self,
        part_file: _PartFile,
        total: int,
        first: int,
        last: int,
        mirror: _Mirror,
        mirrors: Sequence[_Mirror],
    ) -> None:
        headers = dict(mirror.headers)
        headers["Range"] = f"bytes={first}-{last}"
        await self._slot()
        started = time.monotonic()
        offset = first
        try:
            async with self.session.get(
                exact_url(mirror.url), headers=headers, proxy=self.proxy, timeout=self._timeout
            ) as response:
                if response.status != 206:
                    raise _RangeFailed(
                        first, f"HTTP {response.status}", response.status,
                        bad_mirror=response.status in (200, 403, 404, 410, 416),
                    )
                span = _range_total(response)
                if span is None or span[0] != first or span[2] != total:
                    raise _RangeFailed(first, "mirror serves a different file", 206,
                                       bad_mirror=True)
                try:
                    offset = await self._stream(
                        response, part_file, first, mirror, mirrors, allow_switch=True
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                    raise _RangeFailed(getattr(exc, "resume_at", first), type(exc).__name__)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
            raise _RangeFailed(offset, type(exc).__name__)
        finally:
            self._release()
        self.stats.record(mirror.host, offset - first, time.monotonic() - started)
        if offset != last + 1:
            raise _RangeFailed(offset, f"short range {offset - first}/{last - first + 1}")
