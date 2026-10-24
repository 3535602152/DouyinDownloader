from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.ffmpeg import executable
from engine.audio import FfmpegNotAvailable, _Ffmpeg


def test_explicit_override_precedes_bundle_and_path(monkeypatch):
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", sys.executable)
    assert Path(executable()).resolve() == Path(sys.executable).resolve()


def test_invalid_explicit_override_does_not_silently_use_another_binary(monkeypatch):
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", "missing-ffmpeg-test-binary")
    with pytest.raises(FileNotFoundError, match="IMAGEIO_FFMPEG_EXE"):
        executable()


def test_bundle_precedes_system_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    target = tmp_path / "ffmpeg" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    target.parent.mkdir()
    target.write_bytes(b"fixture")
    target.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: "/system/ffmpeg")
    assert executable() == str(target)


@pytest.mark.parametrize("clock", [0.0, 1.0, 1000.0])
async def test_missing_component_reports_error_without_network(monkeypatch, clock):
    # A freshly started runner may have less than the probe TTL of uptime.
    monkeypatch.setattr("engine.audio.time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", "missing-ffmpeg-test-binary")
    with pytest.raises(FfmpegNotAvailable, match="lookup failed"):
        await _Ffmpeg().path()
