# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# PyInstaller executes a spec with ``exec`` and does not provide ``__file__``.
# ``SPECPATH`` is available in normal invocations; the cwd fallback keeps the
# spec usable with older PyInstaller versions when the caller is the repo root.
_SPEC_ROOT = globals().get("SPECPATH")
ROOT = Path(_SPEC_ROOT if _SPEC_ROOT else Path.cwd()).resolve()
PY_PREFIX = Path(sys.prefix)
CONDA_BIN = PY_PREFIX / "Library" / "bin"


datas = [
    (str(ROOT / "img"), "img"),
    (str(ROOT / "config.example.yml"), "."),
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / "gui" / "web"), "gui/web"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "NOTICE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "licenses"), "licenses"),
    (str(ROOT / ".tools" / "ffmpeg" / "ffmpeg.exe"), "ffmpeg"),
    (str(ROOT / ".tools" / "ffmpeg" / "MEDIA_BUILD.json"), "ffmpeg"),
]
binaries = []

for dll_name in (
    "sqlite3.dll",
    "libbz2.dll",
    "liblzma.dll",
    "ffi-8.dll",
    "libexpat.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
):
    dll_path = CONDA_BIN / dll_name
    if dll_path.exists():
        binaries.append((str(dll_path), "."))

hiddenimports = [
    "cli.main",
    "common.paths",
    "douyin.client",
    "douyin.transport",
    "douyin.browser",
    "engine.downloader",
    "engine.queue",
    "services.app",
    "storage.legacy",
    "gui.bridge",
    "gui.web_app",
]

for package in (
    "aiohttp",
    # Chrome's TLS fingerprint for the Douyin API (native libcurl-impersonate).
    "curl_cffi",
    "yaml",
    "playwright",
    "PIL",
    "webview",
    "pythonnet",
    "clr_loader",
):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hidden


a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DouyinDownloader",
    debug=False,
    icon=str(ROOT / "img" / "app_icon.ico"),
    manifest=str(ROOT / "windows_app.manifest"),
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DouyinDownloader",
)
