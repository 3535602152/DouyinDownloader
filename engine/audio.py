"""Normalize downloaded soundtracks to MP3 with the bundled ffmpeg."""

from __future__ import annotations

import asyncio
import collections
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from common.log import get_logger

logger = get_logger(__name__)

_PROBE_TTL_SECONDS = 60.0
_PROBE_TIMEOUT_SECONDS = 10.0
_TRANSCODE_TIMEOUT_SECONDS = 600.0
_STDERR_TAIL_BYTES = 4096


class AudioError(RuntimeError):
    """ffmpeg could not produce the requested audio file."""


class FfmpegNotAvailable(AudioError):
    """No working ffmpeg binary on this machine."""


class _Ffmpeg:
    """Locate the bundled ffmpeg once and remember whether it works."""

    def __init__(self) -> None:
        self._path: Optional[str] = None
        self._error = ""
        self._checked_at = float("-inf")
        self._lock: Optional[asyncio.Lock] = None

    async def path(self) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if time.monotonic() - self._checked_at > _PROBE_TTL_SECONDS:
                await self._probe()
                self._checked_at = time.monotonic()
            if not self._path:
                raise FfmpegNotAvailable(self._error or "ffmpeg unavailable")
            return self._path

    async def _probe(self) -> None:
        self._path = None
        try:
            from common.ffmpeg import executable

            path = executable()
        except Exception as exc:  # the dependency is optional in source checkouts
            self._error = f"FFmpeg lookup failed: {exc!r}"
            return
        if not path or not os.path.exists(path):
            self._error = f"ffmpeg path missing: {path!r}"
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                path, "-version", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), _PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            self._error = f"ffmpeg -version failed: {exc!r}"
            return
        if proc.returncode == 0 and b"ffmpeg version" in stdout.lower():
            self._path = path
            self._error = ""
        else:
            self._error = f"ffmpeg -version exit={proc.returncode}"


_FFMPEG = _Ffmpeg()


async def transcode_to_mp3(source: Path, output: Path) -> Path:
    """Convert any audio/video file to a high-quality MP3 at ``output``."""
    ffmpeg = await _FFMPEG.path()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", str(source), "-vn", "-c:a", "libmp3lame", "-q:a", "2",
        "-map_metadata", "0", "-f", "mp3", str(temp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    tail: collections.deque = collections.deque(maxlen=_STDERR_TAIL_BYTES)

    async def drain() -> None:
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                return
            tail.extend(chunk)

    try:
        await asyncio.wait_for(asyncio.gather(drain(), proc.wait()), _TRANSCODE_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise AudioError(
                f"ffmpeg exit={proc.returncode}: {bytes(tail).decode('utf-8', 'replace')[-300:]}"
            )
        if temp.stat().st_size <= 0:
            raise AudioError("ffmpeg produced an empty file")
        os.replace(temp, output)
        return output
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                pass
        raise
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
