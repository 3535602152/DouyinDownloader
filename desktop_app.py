#!/usr/bin/env python3
"""Desktop entry point: prepare the data directory and open the window.

``--self-test`` checks a packaged build (imports, SQLite, Playwright, MP3) and
writes ``desktop_self_test.log`` next to the executable.
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


ROOT = bundled_root()
sys.path.insert(0, str(ROOT))

from common.paths import (  # noqa: E402
    DATA_DIR_ENV,
    LEGACY_DATA_DIR_ENV,
    data_dir,
    legacy_data_dirs,
    migrate_portable_data,
    portable_data_candidates,
)


def app_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.4.0"
    except OSError:
        return "0.4.0"


def enable_dpi_awareness() -> str:
    """Render natively on high-DPI Windows displays (Per-Monitor V2 first)."""
    if sys.platform != "win32":
        return "not-windows"
    for label, attempt in (
        ("PerMonitorV2", lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4))),
        ("PerMonitor", lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, -2147024891)),
        ("SystemAware", lambda: ctypes.windll.user32.SetProcessDPIAware()),
    ):
        try:
            if attempt():
                return label
        except Exception:
            continue
    return "manifest-or-existing"


def prepare_data_dir() -> Path:
    """The per-user data directory, filled from old portable builds if needed."""
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    roots = []
    if not str(os.environ.get(DATA_DIR_ENV) or os.environ.get(LEGACY_DATA_DIR_ENV) or "").strip():
        if getattr(sys, "frozen", False):
            roots = portable_data_candidates(Path(sys.executable).resolve().parent)
        roots.extend(legacy_data_dirs())
    migrate_portable_data(folder, roots)
    config = folder / "config.yml"
    if not config.exists():
        try:
            from storage.settings import DEFAULT_DOWNLOAD_DIR

            config.write_text(f'link: []\npath: "{DEFAULT_DOWNLOAD_DIR}"\n', encoding="utf-8")
        except OSError:
            pass
    return folder


def self_test(folder: Path) -> int:
    log = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT) \
        / "desktop_self_test.log"
    lines = [f"version={app_version()}", f"data_dir={folder}", f"dpi={enable_dpi_awareness()}"]
    try:
        import asyncio

        from gui.bridge import WebBridge  # noqa: F401
        from storage.db import Database

        database = Database(folder / "desktop_self_test.sqlite")
        database.close()
        (folder / "desktop_self_test.sqlite").unlink(missing_ok=True)
        lines.append("sqlite=OK")

        async def media() -> None:
            import wave

            from engine.audio import transcode_to_mp3

            source, output = folder / "selftest.wav", folder / "selftest.mp3"
            try:
                with wave.open(str(source), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(44100)
                    stream.writeframes(b"\x00\x00" * 4410)
                await transcode_to_mp3(source, output)
                if output.stat().st_size < 100:
                    raise RuntimeError("MP3 conversion produced an invalid result")
            finally:
                source.unlink(missing_ok=True)
                output.unlink(missing_ok=True)

        asyncio.run(media())
        lines.append("ffmpeg=OK")

        async def browser() -> None:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                launched = await playwright.chromium.launch(channel="msedge", headless=True)
                await launched.close()

        asyncio.run(browser())
        lines.append("playwright=OK")
        code = 0
    except Exception:
        lines.append("exception=\n" + traceback.format_exc())
        code = 1
    try:
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return code


def main() -> None:
    os.chdir(ROOT)
    if "--self-test-ui" in sys.argv and not os.environ.get(DATA_DIR_ENV):
        raise SystemExit("--self-test-ui requires an explicit isolated DOUYIN_DOWNLOADER_DATA_DIR")
    folder = prepare_data_dir()
    if "--self-test" in sys.argv:
        raise SystemExit(self_test(folder))
    enable_dpi_awareness()
    if "--self-test-ui" in sys.argv:
        from common.desktop_checks import check_native_window

        log_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
        raise SystemExit(check_native_window(
            folder, log_root / "desktop_ui_self_test.log",
            expect_persistence="--expect-persistence" in sys.argv,
        ))
    from gui.web_app import run_web_app

    run_web_app(folder, title=f"抖音下载器 {app_version()}")


if __name__ == "__main__":
    main()
