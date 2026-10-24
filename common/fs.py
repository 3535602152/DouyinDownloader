"""File-system helpers: safe names and atomic writes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_ILLEGAL = re.compile(r'[<>:"/\\|?*#\x00-\x1f]')
_RESERVED = re.compile(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])")


def sanitize_filename(value: str, max_length: int = 80) -> str:
    """A single path component that is valid on Windows and POSIX."""
    text = str(value or "").replace("\n", " ").replace("\r", " ")
    text = _ILLEGAL.sub("_", text)
    text = re.sub(r"_+", "_", text)
    text = re.sub(r" +", " ", text)
    text = text.strip("._- ")
    if len(text) > max_length:
        text = text[:max_length].rstrip("._- ")
    return text or "untitled"


def safe_dir_name(value: str, max_length: int = 80) -> str:
    name = sanitize_filename(str(value or "").strip(), max_length)
    if _RESERVED.fullmatch(name.split(".", 1)[0]):
        name = f"_{name}"
    return name


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` durably and replace ``path`` in one step."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    atomic_write_text(Path(path), json.dumps(data, ensure_ascii=False, indent=indent,
                                             default=str))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def file_size(path: Any) -> int:
    try:
        return Path(path).stat().st_size
    except (OSError, TypeError, ValueError):
        return 0


def nonempty_file(path: Any) -> bool:
    try:
        candidate = Path(path)
        return candidate.is_file() and candidate.stat().st_size > 0
    except (OSError, TypeError, ValueError):
        return False


def image_data_url(path: Any, limit: int = 4 * 1024 * 1024) -> str:
    """A ``data:`` URL for a small local image ("" when missing or too large)."""
    import base64
    import mimetypes

    try:
        candidate = Path(str(path or ""))
        if not str(path or "") or not candidate.is_file() or candidate.stat().st_size > limit:
            return ""
        mime = mimetypes.guess_type(candidate.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(candidate.read_bytes()).decode('ascii')}"
    except OSError:
        return ""
