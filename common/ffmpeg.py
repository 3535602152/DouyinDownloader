"""Locate a user-selected, project-supplied or system FFmpeg without downloads."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def executable() -> str:
    override = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if override:
        selected = shutil.which(override)
        if selected:
            return selected
        raise FileNotFoundError("IMAGEIO_FFMPEG_EXE does not point to an executable")
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    for candidate in (root / "ffmpeg" / name, root / ".tools" / "ffmpeg" / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    selected = shutil.which("ffmpeg")
    if selected:
        return selected
    raise FileNotFoundError(
        "FFmpeg is unavailable; prepare the media component or set IMAGEIO_FFMPEG_EXE"
    )
