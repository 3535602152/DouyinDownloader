"""Where downloaded files go and what each file is.

A download's *context* (:class:`DownloadContext`) says why a work is being
saved — which followed creator, which account's likes, a single link … —
and therefore which folder it belongs in.  The layout is the one users
already have on disk:

``关注主播/<creator>/主页作品/<work>/``           monitored creators
``喜欢|收藏/<account>/<author>/<work>/``           like / collect subscriptions
``用户主页/<user>/<mode>/…``  ``我的内容/<mode>/…``  account downloads
``单独下载/单个作品|合集|音乐/…``                  single links
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from common.fs import safe_dir_name, sanitize_filename
from storage import folders

MODE_DIRS = {
    "post": "主页作品", "like": "喜欢作品", "mix": "合集作品", "music": "音乐原声作品",
    "collect": "收藏作品", "collectmix": "收藏合集",
}
SUBSCRIPTION_ROOTS = {"like": "喜欢", "collect": "收藏"}
AUTHOR_DIR_STYLES = ("nickname", "sec_uid", "nickname_uid", "user_sec_uid")
TEMPLATE_VARIABLES = ("id", "title", "author", "author_id", "date", "year", "month", "day",
                      "time", "hour", "minute", "second", "timestamp", "type", "mode")
DEFAULT_TEMPLATE = "{date}_{title}_{id}"
TEMPLATE_MAX_LENGTH = 200
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_NAME_LIMIT = 80

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v", ".avi"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


@dataclass(frozen=True)
class DownloadContext:
    """Why a work is being downloaded; decides its folder."""

    source_type: str            # following|subscription|user|self|single_aweme|single_mix|single_music
    source_name: str = ""
    source_sec_uid: Optional[str] = None
    mode: str = "post"
    collection_id: Optional[str] = None
    collection_name: Optional[str] = None
    source_uid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "DownloadContext":
        known = {key: values.get(key) for key in cls.__dataclass_fields__
                 if values.get(key) is not None}
        known.setdefault("source_type", "single_aweme")
        return cls(**known)


# ── naming templates ────────────────────────────────────────────────


def validate_template(template: str, label: str = "模板") -> None:
    text = str(template or "")
    if not text.strip():
        raise ValueError(f"{label}不能为空")
    if len(text) > TEMPLATE_MAX_LENGTH:
        raise ValueError(f"{label}不能超过 {TEMPLATE_MAX_LENGTH} 个字符")
    if "/" in text or "\\" in text:
        raise ValueError(f"{label}不能包含路径分隔符")
    names = _PLACEHOLDER.findall(text)
    unknown = sorted({name for name in names if name not in TEMPLATE_VARIABLES})
    if unknown:
        raise ValueError(f"{label}使用了未知变量：{', '.join(unknown)}")
    if "id" not in names:
        raise ValueError(f"{label}必须包含 {{id}}，以免作品互相覆盖")


def template_context(*, aweme_id: str, title: str, author: str, author_sec_uid: str,
                     create_time: int, media_type: str, mode: str) -> Dict[str, str]:
    stamp = int(create_time or 0)
    try:
        moment = datetime.fromtimestamp(stamp) if stamp > 0 else datetime.now()
    except (OSError, OverflowError, ValueError):
        moment = datetime.now()
    return {
        "id": str(aweme_id), "title": title or "no_title", "author": author or "",
        "author_id": author_sec_uid or "", "date": moment.strftime("%Y-%m-%d"),
        "year": moment.strftime("%Y"), "month": moment.strftime("%m"),
        "day": moment.strftime("%d"), "time": moment.strftime("%H%M") if stamp else "",
        "hour": moment.strftime("%H") if stamp else "",
        "minute": moment.strftime("%M") if stamp else "",
        "second": moment.strftime("%S") if stamp else "",
        "timestamp": str(stamp) if stamp else "", "type": media_type or "", "mode": mode or "",
    }


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Render and sanitize; the ``{id}`` part is never truncated away."""
    def value(match) -> str:
        return str(values.get(match.group(1)) or "")

    identity = sanitize_filename(str(values.get("id") or ""), _NAME_LIMIT) \
        if values.get("id") else ""
    if identity and identity != "untitled" and "{id}" in template:
        rest = _PLACEHOLDER.sub(lambda m: "" if m.group(1) == "id" else value(m), template)
        budget = max(0, _NAME_LIMIT - len(identity) - 1)
        prefix = sanitize_filename(rest, budget) if budget else ""
        return f"{prefix}_{identity}" if prefix and prefix != "untitled" else identity
    rendered = sanitize_filename(_PLACEHOLDER.sub(value, template), _NAME_LIMIT)
    if rendered == "untitled":
        return sanitize_filename(f"{values.get('date', '')}_{values.get('id', '')}")
    return rendered


# ── folders ─────────────────────────────────────────────────────────


def author_dir_name(name: str, sec_uid: str, style: str) -> str:
    nickname = safe_dir_name(name or "未知作者")
    sec_uid = str(sec_uid or "").strip()
    if style == "sec_uid" and sec_uid:
        return safe_dir_name(sec_uid)
    if style == "user_sec_uid" and sec_uid:
        return "user_" + re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", sec_uid).strip("._- ")
    if style == "nickname_uid" and sec_uid:
        return safe_dir_name(f"{name}_{sec_uid}")
    return nickname


def collection_dir_name(name: str, collection_id: str) -> str:
    safe = safe_dir_name(name)
    stable = str(collection_id or "").strip()
    if not stable or safe == safe_dir_name(stable) or safe.endswith(f"_{safe_dir_name(stable)}"):
        return safe
    return safe_dir_name(f"{safe}_{stable[-8:]}")


class Layout:
    """Folder decisions for one download root and the user's naming settings."""

    def __init__(self, root: Path, *, author_style: str = "nickname",
                 file_template: str = DEFAULT_TEMPLATE, folder_template: str = DEFAULT_TEMPLATE,
                 folder_per_work: bool = True):
        self.root = Path(root)
        self.author_style = author_style if author_style in AUTHOR_DIR_STYLES else "nickname"
        self.file_template = file_template or DEFAULT_TEMPLATE
        self.folder_template = folder_template or DEFAULT_TEMPLATE
        self.folder_per_work = bool(folder_per_work)

    def _owner_dir(self, root_name: str, name: str, sec_uid: str, uid: str = "") -> Path:
        base = self.root / root_name
        if self.author_style == "nickname" and sec_uid:
            return folders.resolve_folder(base, name or sec_uid, sec_uid, uid)
        return base / author_dir_name(name or sec_uid or "未知用户", sec_uid, self.author_style)

    def source_home(self, context: DownloadContext) -> Optional[Path]:
        """The creator/account folder that also holds 头像.jpg / 主页截图.png."""
        root = {"following": "关注主播", "user": "用户主页"}.get(context.source_type)
        if not root:
            return None
        return self._owner_dir(root, context.source_name, context.source_sec_uid or "",
                               context.source_uid or "")

    def context_dir(self, context: DownloadContext, author: str, author_sec_uid: str) -> Path:
        mode = context.mode or "post"
        mode_dir = MODE_DIRS.get(mode, safe_dir_name(mode))
        name = (context.collection_name or "").strip()
        ident = (context.collection_id or "").strip()
        if mode == "collect":
            name = name or (f"收藏夹_{ident}" if ident else "默认收藏")
        elif mode in ("mix", "collectmix") or context.source_type == "single_mix":
            name = name or ident or "未命名合集"
        collection = collection_dir_name(name, ident) if name else ""

        def author_part() -> str:
            return author_dir_name(author or "未知作者", author_sec_uid, self.author_style)

        kind = context.source_type
        if kind in ("following", "user"):
            parts = [mode_dir]
            if mode == "like":
                parts.append(author_part())
            elif mode in ("mix", "collectmix") and collection:
                parts.append(collection)
            return self.source_home(context).joinpath(*parts)
        if kind == "subscription":
            home = self._owner_dir(SUBSCRIPTION_ROOTS.get(mode, "喜欢"), context.source_name,
                                   context.source_sec_uid or "")
            return home / author_part()
        if kind == "self":
            parts = ["我的内容", mode_dir]
            if mode in ("collect", "collectmix", "mix") and collection:
                parts.append(collection)
            if mode in ("collect", "like"):
                parts.append(author_part())
            return self.root.joinpath(*parts)
        if kind == "single_mix":
            return self.root / "单独下载" / "合集" / (collection or "未命名合集")
        if kind == "single_music":
            return self.root / "单独下载" / "音乐" / collection_dir_name(
                name or context.source_name or "未知音乐", ident)
        return self.root / "单独下载" / "单个作品" / author_part()

    def work_target(self, context: DownloadContext, *, aweme_id: str, title: str, author: str,
                    author_sec_uid: str, create_time: int, media_type: str) -> Tuple[Path, str]:
        """``(folder, file stem)`` for every file of one work."""
        values = template_context(aweme_id=aweme_id, title=title, author=author,
                                  author_sec_uid=author_sec_uid, create_time=create_time,
                                  media_type=media_type, mode=context.mode)
        folder = self.context_dir(context, author, author_sec_uid)
        if self.folder_per_work:
            folder = folder / render_template(self.folder_template, values)
        return folder, render_template(self.file_template, values)


# ── file kinds ──────────────────────────────────────────────────────

_LIVE = re.compile(r"_live_(\d+)$")
_INDEXED = re.compile(r"_(\d+)$")


def classify_file(name: str) -> Tuple[str, int]:
    """``(kind, gallery position)`` of a downloaded file from its name.

    Kinds: ``media`` (video, gallery still or music track), ``live`` (live
    photo clip), ``motion`` (merged motion photo), ``cover``, ``music``
    (soundtrack), ``json``, ``comments``, ``avatar``; ``""`` for unknown.
    """
    lowered = str(name or "").lower()
    path = Path(lowered)
    suffix, stem = path.suffix, path.stem
    if lowered.endswith("_comments.json"):
        return "comments", 0
    if lowered.endswith("_data.json"):
        return "json", 0
    if "_cover." in lowered:
        return "cover", 0
    if "_avatar." in lowered:
        return "avatar", 0
    if "_music." in lowered:
        return "music", 0
    live = _LIVE.search(stem)
    if live:
        if suffix in VIDEO_SUFFIXES:
            return "live", int(live.group(1))
        if suffix in IMAGE_SUFFIXES:
            return "motion", int(live.group(1))
    if suffix in IMAGE_SUFFIXES:
        indexed = _INDEXED.search(stem)
        return "media", int(indexed.group(1)) if indexed else 1
    if suffix in VIDEO_SUFFIXES or suffix in AUDIO_SUFFIXES:
        return "media", 0
    return "", 0
