"""One logging setup for the whole backend.

Every module asks for ``get_logger(__name__)``; handlers are installed once on
the ``DouyinDownloader`` root logger.  The desktop shell additionally points
a rotating file handler at the per-user data directory.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT_NAME = "DouyinDownloader"
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _root() -> logging.Logger:
    root = logging.getLogger(ROOT_NAME)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.ERROR)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    _root()
    short = str(name or "app").rsplit(".", 1)[-1]
    return logging.getLogger(f"{ROOT_NAME}.{short}")


def log_to_file(path: Path, *, level: int = logging.INFO) -> None:
    """Also write backend logs to ``path`` (rotated at 5 MB, 3 backups)."""
    root = _root()
    target = str(Path(path))
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == target:
            return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=5 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)


def safe_url(value: object) -> str:
    """Keep scheme/host/path of a URL; drop credentials and query values."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        clean = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    except (TypeError, ValueError):
        return text.split("?", 1)[0].split("#", 1)[0]
    return f"{clean}?[query_keys={','.join(keys)}]" if keys else clean
