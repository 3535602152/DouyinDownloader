"""Where the desktop app keeps its mutable data.

Everything the user owns (login, settings, database, caches, browser
profile) lives in one per-user directory shared by every app version.  Old
portable builds kept the same files next to the executable; the first start
of a new build copies whatever is still missing from the newest of them.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Dict, Iterable, List, Optional

APP_DIR_NAME = "DouyinDownloader"
DATABASE_FILE_NAME = "DouyinDownloader.db"
DATA_DIR_ENV = "DOUYIN_DOWNLOADER_DATA_DIR"
LEGACY_DATA_DIR_ENV = "DY_DOWNLOADER_DATA_DIR"
LEGACY_APP_DIR_NAMES = ("DyDownloader",)
LEGACY_DATABASE_FILE_NAMES = ("dy_downloader.db",)
SUBSCRIPTIONS_FILE_NAME = "DouyinDownloader.subscriptions.db"
PERSISTED_FILES = (".accounts.json", ".cookies.json", "config.yml", "desktop_settings.json",
                   SUBSCRIPTIONS_FILE_NAME, DATABASE_FILE_NAME)


def data_dir(env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = str(env.get(DATA_DIR_ENV) or env.get(LEGACY_DATA_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("LOCALAPPDATA") or env.get("APPDATA") or "").strip()
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def legacy_data_dirs(env: Optional[Dict[str, str]] = None) -> List[Path]:
    env = os.environ if env is None else env
    if str(env.get(DATA_DIR_ENV) or env.get(LEGACY_DATA_DIR_ENV) or "").strip():
        return []
    base = str(env.get("LOCALAPPDATA") or env.get("APPDATA") or "").strip()
    base_path = Path(base) if base else Path.home() / "AppData" / "Local"
    current = data_dir(env).resolve()
    found = []
    for name in LEGACY_APP_DIR_NAMES:
        candidate = base_path / name
        try:
            if candidate.resolve() != current and candidate.is_dir():
                found.append(candidate)
        except OSError:
            continue
    return found


def _score(folder: Path) -> float:
    times = []
    for name in PERSISTED_FILES + LEGACY_DATABASE_FILE_NAMES:
        try:
            if (folder / name).exists():
                times.append((folder / name).stat().st_mtime)
        except OSError:
            continue
    return max(times, default=-1.0)


def portable_data_candidates(install_dir: Path) -> List[Path]:
    """Old portable/version folders next to the running executable."""
    install_dir = Path(install_dir).resolve()
    found: List[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved not in found and _score(resolved) >= 0:
            found.append(resolved)

    add(install_dir)
    try:
        for child in install_dir.parent.iterdir():
            name = child.name.lower()
            if child.is_dir() and child.resolve() != install_dir and name.startswith(
                ("dy_downloader-", "douyin-downloader-", "douyindownloader")
            ):
                add(child)
    except OSError:
        pass
    found.sort(key=_score, reverse=True)
    return found


def _copy_sqlite(source: Path, destination: Path) -> bool:
    """Copy a transactionally consistent snapshot (WAL included)."""
    temporary = destination.with_name(destination.name + ".migration.tmp")
    try:
        temporary.unlink(missing_ok=True)
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=30)) as src, \
                closing(sqlite3.connect(str(temporary), timeout=30)) as dst:
            src.backup(dst)
        os.replace(temporary, destination)
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def migrate_portable_data(target: Path, roots: Iterable[Path]) -> Dict[str, str]:
    """Copy files missing from ``target`` out of the first root that has them."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    roots = [Path(root) for root in roots]
    copy_databases = not (target / DATABASE_FILE_NAME).exists()
    database_root = next((root for root in roots
                          if any((root / name).is_file()
                                 for name in (DATABASE_FILE_NAME, *LEGACY_DATABASE_FILE_NAMES))), None)
    if database_root and copy_databases and (target / SUBSCRIPTIONS_FILE_NAME).exists():
        raise RuntimeError("Subscription database already exists without a main database; "
                           "refusing to pair it with another library")
    migrated: Dict[str, str] = {}
    for name in PERSISTED_FILES:
        destination = target / name
        if destination.exists():
            continue
        names = (name,)
        if name == DATABASE_FILE_NAME:
            names += LEGACY_DATABASE_FILE_NAMES
        elif name == SUBSCRIPTIONS_FILE_NAME:
            names += ("dy_downloader.subscriptions.db",)
        is_database = name in (DATABASE_FILE_NAME, SUBSCRIPTIONS_FILE_NAME)
        candidates = roots
        if is_database:
            candidates = [database_root] if database_root and copy_databases else []
        for root in candidates:
            source = next((root / n for n in names if (root / n).is_file()), None)
            if source is None:
                continue
            try:
                if is_database:
                    if not _copy_sqlite(source, destination):
                        raise RuntimeError(f"Could not migrate database: {source}")
                else:
                    shutil.copy2(source, destination)
            except OSError:
                continue
            migrated[name] = str(source)
            break
    cache = target / "cache"
    if not cache.exists():
        for root in roots:
            if (root / "cache").is_dir():
                try:
                    shutil.copytree(root / "cache", cache)
                    migrated["cache"] = str(root / "cache")
                    break
                except OSError:
                    continue
    return migrated
