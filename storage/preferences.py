"""One definition for preference defaults, validation and the desktop editor.

The exported JS is generated from this module; it is an offline-preview cache,
not a second hand-maintained settings model. Login and per-account state stay
in their existing services and never become preferences here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from storage.files import (
    AUTHOR_DIR_STYLES,
    DEFAULT_TEMPLATE,
    TEMPLATE_MAX_LENGTH,
    TEMPLATE_VARIABLES,
    validate_template,
)

QUALITY_LABELS = {"original": "原片", "highest": "最高", "lowest": "最低",
                  **{q: q for q in ("1440p", "1080p", "720p", "540p", "480p", "360p")}}
QUALITY_VALUES = {label: value for value, label in QUALITY_LABELS.items()}
DEFAULT_DOWNLOAD_DIR = "Downloaded"

GROUPS = [
    {"id": "account", "title": "账号", "description": "管理主账号及订阅使用的登录身份"},
    {"id": "appearance", "title": "外观与交互", "description": "主题、动效和媒体播放偏好"},
    {"id": "download", "title": "下载与媒体", "description": "画质、附件和后台下载负载"},
    {"id": "storage", "title": "文件与存储", "description": "下载位置及作品命名规则"},
    {"id": "automation", "title": "自动化", "description": "关注主播的扫描策略和补齐周期"},
    {"id": "advanced", "title": "高级与诊断", "description": "网络、浏览器回退及运行诊断"},
]


def _field(key, form, group, title, description, default, kind="text", **extra):
    return {"key": key, "form": form, "group": group, "title": title, "description": description,
            "default": default, "type": kind, "scope": "config", **extra}


FIELDS = [
    _field("theme", "theme", "appearance", "主题", "浅色、深色或跟随系统；侧栏按钮使用同一偏好。", "light", "choice",
           scope="ui", options=[["light", "浅色"], ["dark", "深色"], ["system", "跟随系统"]]),
    _field("motion", "motion", "appearance", "界面动效", "跟随系统的减少动态效果设置，或始终减少动效。", "system", "choice",
           scope="ui", options=[["system", "跟随系统"], ["reduced", "减少动效"]]),
    _field("media_autoplay", "mediaAutoplay", "appearance", "打开作品时自动播放", "关闭后由播放按钮开始；实况片段也遵循此偏好。", True, "boolean", scope="ui"),
    _field("gallery_autoplay", "galleryAutoplay", "appearance", "图文自动轮播", "打开图文时自动翻页；切到后台时暂停计时。", True, "boolean", scope="ui"),
    _field("video_quality", "quality", "download", "画质", "原片是上传文件；压缩版自动选择最接近的可用清晰度。", "最高", "choice",
           state="quality", options=[[label, label] for label in QUALITY_VALUES]),
    _field("live_photo_merge", "livePhotoMerge", "download", "合并实况图", "把照片和视频合成一个动态照片文件。", True, "boolean"),
    _field("monitor_work_concurrency", "monitorWorks", "download", "后台同时下载", "1 个最接近正常浏览；只影响后台队列，前台任务仍优先。", 1, "choice",
           state="monitor_works", aliases=["monitor_works"], options=[[1, "1 个"], [2, "2 个"], [3, "3 个"]]),
    _field("retry_times", "retry", "download", "失败重试次数", "单次下载失败后的重试次数；0 表示不重试。", 3, "number", state="retry", minimum=0, maximum=20),
    _field("path", "path", "storage", "下载目录", "作品、封面、配乐和元数据的保存位置。", DEFAULT_DOWNLOAD_DIR, "path"),
    _field("folderstyle", "folderstyle", "storage", "每个作品单独建文件夹", "便于将媒体与附件保存在一起。", True, "boolean"),
    _field("author_dir", "authorDir", "storage", "作者目录", "固定功能目录下的作者文件夹命名方式。", "nickname", "choice",
           options=list(zip(AUTHOR_DIR_STYLES, ["昵称", "账号 ID", "昵称 + UID", "昵称 + 账号 ID"]))),
    _field("filename_template", "filenameTemplate", "storage", "文件名模板", "支持 {date}、{title}、{id}；必须含 {id}，避免同名覆盖。", DEFAULT_TEMPLATE, "template", advanced=True),
    _field("folder_template", "folderTemplate", "storage", "作品文件夹模板", "支持 {date}、{title}、{id}；必须含 {id}。", DEFAULT_TEMPLATE, "template", advanced=True),
    _field("scan_strategy", "scanStrategy", "automation", "扫描策略", "稳定模式在主播和分页之间等待；快速模式连续扫描，更容易触发风控。", "stable", "choice",
           options=[["stable", "稳定"], ["fast", "快速"]]),
    _field("full_scan_interval", "fullScanInterval", "automation", "自动全量扫描", "定期重新扫描每个主播的完整主页，补齐漏掉的作品。", 24, "choice",
           options=[[0, "关闭"], [6, "6 小时"], [24, "24 小时"], [72, "72 小时"]]),
    _field("browser_fallback", "browserFallback", "advanced", "浏览器回退", "接口不可用时尝试浏览器；运行环境需要安装浏览器支持。", True, "boolean"),
    _field("proxy", "proxy", "advanced", "网络代理", "留空使用直连；支持 http、https、socks5 或 socks5h URL。", "", "proxy"),
]
FULL_SCAN_INTERVALS = tuple(option[0] for field in FIELDS if field["key"] == "full_scan_interval"
                            for option in field["options"])


def schema() -> Dict[str, Any]:
    return deepcopy({"version": 1, "groups": GROUPS, "fields": FIELDS,
                     "template_variables": list(TEMPLATE_VARIABLES), "template_max_length": TEMPLATE_MAX_LENGTH})


def config_defaults() -> Dict[str, Any]:
    result = {f["key"]: deepcopy(f["default"]) for f in FIELDS if f["scope"] == "config"}
    result["video_quality"] = QUALITY_VALUES[result["video_quality"]]
    result["browser_fallback"] = {"enabled": result["browser_fallback"], "headless": True}
    return result


def preference_state(settings) -> Dict[str, Any]:
    config, ui = settings.snapshot(), settings.ui()
    result = {}
    for field in FIELDS:
        key = field["key"]
        value = (ui if field["scope"] == "ui" else config).get(key, field["default"])
        if key == "video_quality":
            value = QUALITY_LABELS.get(value, field["default"])
        elif key == "browser_fallback":
            value = value.get("enabled", True) if isinstance(value, dict) else bool(value)
        result[field.get("state", key)] = value
    return result


def validate_patch(values: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate the complete patch before writing either settings file.

    Unsupplied fields are preserved, including legacy camelCase bridge calls.
    """
    config, ui = {}, {}
    for field in FIELDS:
        keys = [field["key"], field.get("state", field["key"]), field["form"], *field.get("aliases", [])]
        supplied = next((key for key in keys if key in values and values[key] is not None), None)
        if supplied is None:
            continue
        value = values[supplied]
        kind, key, title = field["type"], field["key"], field["title"]
        if key == "video_quality":
            value = QUALITY_LABELS.get(value, value)
        if kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{title}必须是开关值。")
        elif kind in ("number", "choice"):
            if kind == "number" or isinstance(field["default"], int):
                try:
                    if isinstance(value, bool) or float(value) != int(value):
                        raise ValueError
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(f"{title}必须是整数。") from None
            if kind == "choice" and value not in [option[0] for option in field["options"]]:
                raise ValueError(f"{title}选项无效。")
            if kind == "number" and not field["minimum"] <= value <= field["maximum"]:
                raise ValueError(f"{title}须在 {field['minimum']}–{field['maximum']} 之间。")
        else:
            value = str(value).strip()
            if kind == "template":
                validate_template(value, title)
            elif kind == "proxy" and value:
                try:
                    parsed = urlsplit(value)
                    valid = parsed.scheme in ("http", "https", "socks5", "socks5h") and parsed.hostname and parsed.port
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError("代理须为带端口的 http、https、socks5 或 socks5h URL。")
            elif kind == "path" and not value:
                continue
        if key == "video_quality":
            # Accept a canonical config value as well as the legacy display label.
            value = QUALITY_VALUES[value]
        elif key == "browser_fallback":
            value = {"enabled": value}
        (ui if field["scope"] == "ui" else config)[key] = value
    return config, ui
