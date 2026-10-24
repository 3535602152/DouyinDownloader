"""Creator folders named after the creator but identified by ID.

``关注主播/<name>``, ``用户主页/<name>`` and the subscription roots use the
current nickname, which is what people browse for.  Ownership lives in a
hidden ``.author.json`` marker inside the folder, so a folder is still
recognized after the creator renames themselves.

* :func:`resolve_folder` — used while downloading; returns the folder owned
  by ``sec_uid`` (even with a stale name) or claims a new one.  Never renames.
* :func:`reconcile_folder` — run only when nothing of this creator is
  downloading; renames the folder to the current nickname and merges folders
  left behind by earlier renames.  The caller rewrites stored paths.

Two creators with the same nickname are told apart as ``小王 (1234567890)``.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from common.fs import safe_dir_name
from common.log import get_logger

logger = get_logger(__name__)

MARKER = ".author.json"
_lock = threading.RLock()
_index: Dict[str, Dict[str, str]] = {}   # root -> {sec_uid: folder name}


def read_owner(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and str(data.get("sec_uid") or "").strip() else None


def _owner(folder: Path) -> str:
    data = read_owner(folder)
    return str(data.get("sec_uid") or "").strip() if data else ""


def _set_hidden(path: Path, hidden: bool) -> None:
    if os.name != "nt" or not path.exists():
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02 if hidden else 0x80)
    except Exception:
        pass


def write_owner(folder: Path, sec_uid: str, *, uid: str = "", nickname: str = "") -> None:
    marker, temp = folder / MARKER, folder / (MARKER + ".tmp")
    try:
        _set_hidden(marker, False)
        temp.write_text(json.dumps({"sec_uid": sec_uid, "uid": uid, "nickname": nickname},
                                   ensure_ascii=False), encoding="utf-8")
        os.replace(temp, marker)
        _set_hidden(marker, True)
    except OSError as exc:
        logger.warning("Could not write author marker in %s: %s", folder, exc)


def _scan(root: Path) -> Dict[str, str]:
    found: Dict[str, str] = {}
    try:
        children = sorted((child for child in root.iterdir() if child.is_dir()),
                          key=lambda path: path.name)
    except OSError:
        return found
    for child in children:
        owner = _owner(child)
        if owner and owner not in found:
            found[owner] = child.name
    return found


def owned_folder(root: Path, sec_uid: str) -> Optional[Path]:
    sec_uid = str(sec_uid or "").strip()
    if not sec_uid:
        return None
    with _lock:
        name = _index.get(str(root), {}).get(sec_uid)
        if name and _owner(root / name) == sec_uid:
            return root / name
        _index[str(root)] = _scan(root)
        name = _index[str(root)].get(sec_uid)
        return root / name if name else None


def forget_index() -> None:
    with _lock:
        _index.clear()


def desired_name(root: Path, nickname: str, sec_uid: str, uid: str = "") -> str:
    candidates = [safe_dir_name(nickname or sec_uid or "未知用户")]
    if uid:
        candidates.append(safe_dir_name(f"{nickname} ({uid})"))
    candidates.append(safe_dir_name(f"{nickname} ({sec_uid})"))
    for name in candidates:
        owner = _owner(root / name) if (root / name).is_dir() else ""
        if not owner or owner == sec_uid:
            return name
    return candidates[-1]


def resolve_folder(root: Path, nickname: str, sec_uid: str, uid: str = "") -> Path:
    """This creator's folder (created if needed), without renaming anything."""
    sec_uid = str(sec_uid or "").strip()
    if not sec_uid:
        folder = root / safe_dir_name(nickname or "未知用户")
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    with _lock:
        existing = owned_folder(root, sec_uid)
        if existing is not None:
            return existing
        # An unmarked folder with this name predates markers: adopt it.
        folder = root / desired_name(root, nickname, sec_uid, uid)
        folder.mkdir(parents=True, exist_ok=True)
        if _owner(folder) != sec_uid:
            write_owner(folder, sec_uid, uid=uid, nickname=nickname)
        _index.setdefault(str(root), {})[sec_uid] = folder.name
        return folder


@dataclass
class ReconcileResult:
    folder: Optional[Path] = None
    renamed_from: str = ""
    merged: List[str] = field(default_factory=list)
    moves: List[Tuple[Path, Path]] = field(default_factory=list)
    deferred: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.moves)


def _same(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _merge_tree(source: Path, destination: Path) -> None:
    """Move ``source`` into ``destination``; existing names stay put."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in list(source.iterdir()):
        if entry.name in (MARKER, MARKER + ".tmp"):
            try:
                _set_hidden(entry, False)
                entry.unlink()
            except OSError:
                pass
            continue
        target = destination / entry.name
        if not target.exists():
            try:
                os.rename(entry, target)
            except OSError as exc:
                logger.info("Could not move %s into %s: %s", entry, destination, exc)
        elif entry.is_dir() and target.is_dir():
            _merge_tree(entry, target)
    try:
        source.rmdir()
    except OSError:
        pass


def reconcile_folder(root: Path, sec_uid: str, nickname: str, *, uid: str = "",
                     old_names: Iterable[str] = (),
                     holds_work: Callable[[Path], bool] = lambda _folder: False,
                     ) -> ReconcileResult:
    """Rename this creator's folder to ``nickname`` and merge stale folders.

    ``old_names`` are names this creator's works were saved under before
    markers existed; an unmarked folder with such a name only counts when
    ``holds_work`` confirms it contains one of their works.
    """
    result = ReconcileResult()
    sec_uid, nickname = str(sec_uid or "").strip(), str(nickname or "").strip()
    if not sec_uid or not nickname or not root.is_dir():
        return result
    with _lock:
        current = owned_folder(root, sec_uid)
        legacy = []
        for name in sorted({safe_dir_name(name) for name in old_names if str(name).strip()}):
            folder = root / name
            if not folder.is_dir() or (current is not None and _same(folder, current)):
                continue
            owner = _owner(folder)
            if owner and owner != sec_uid:
                continue
            if not owner and not holds_work(folder):
                continue
            legacy.append(folder)
        if current is None and not legacy:
            return result
        target_name = desired_name(root, nickname, sec_uid, uid)
        target = root / target_name
        primary = current or legacy.pop(0)
        for index, folder in enumerate(legacy):
            if _same(folder, target):
                legacy[index], primary = primary, folder
                break
        if primary.name != target_name:
            if target.exists() and not _same(primary, target):
                try:
                    target.rmdir()
                except OSError:
                    result.deferred = f"目标文件夹已存在：{target_name}"
                    return result
            try:
                os.rename(primary, target)
            except OSError as exc:  # usually a file open in a player
                result.deferred = str(exc)
                return result
            result.moves.append((primary, target))
            result.renamed_from = primary.name
        for folder in legacy:
            if folder.is_dir():
                _merge_tree(folder, target)
                result.moves.append((folder, target))
                result.merged.append(folder.name)
        data = read_owner(target) or {}
        if data.get("sec_uid") != sec_uid or (uid and not data.get("uid")):
            write_owner(target, sec_uid, uid=uid, nickname=nickname)
        _index.setdefault(str(root), {})[sec_uid] = target.name
        result.folder = target
        return result
