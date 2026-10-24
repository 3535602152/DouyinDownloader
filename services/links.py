"""The link page: parse pasted links, list their works, download a selection."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence, Tuple

from douyin import aweme as aw
from douyin.urls import TYPE_LABELS
from services.common import explain, pages, resolve_link
from services.context import Context
from services.foreground import DOWNLOADED, EXISTS, download_one
from storage.files import DownloadContext

_STATUS_TEXT = {DOWNLOADED: "已下载", EXISTS: "已存在"}


class LinkService:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.items: Dict[str, Tuple[Dict[str, Any], DownloadContext]] = {}

    # ── preview ─────────────────────────────────────────────────────

    def preview(self, links: Sequence[str]) -> bool:
        links = [str(link).strip() for link in links if str(link or "").strip()]
        if not links:
            raise ValueError("请至少输入一个抖音链接。")
        self.ctx.settings.update(link=list(links))
        self.items = {}

        async def run(_task) -> None:
            events = self.ctx.events
            count = failed = 0
            try:
                for index, link in enumerate(links, start=1):
                    events.activity("link-parse", f"解析链接 {index}/{len(links)}"
                                    + (f" 失败{failed}" if failed else ""))
                    try:
                        count += await self._preview_one(link)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        events.log(f"预览失败：{link}（{explain(exc)}）")
                events.activity("link-parse", f"解析完成 {count}", running=False)
                events.emit("preview_done", scope="link", count=count, failed=failed)
            except asyncio.CancelledError:
                events.activity("link-parse", "")
                events.emit("preview_cancelled", scope="link")
                raise

        self.ctx.tasks.submit("link-preview", run, scope="link", lane="link",
                              key="link-preview:" + "\n".join(links))
        return True

    def _add(self, payload: Dict[str, Any], context: DownloadContext, source_type: str,
             author_name: str = "") -> bool:
        aweme_id = aw.aweme_id(payload) or str(payload.get("aweme_id") or "")
        if not aweme_id or aweme_id in self.items:
            return False
        self.items[aweme_id] = (payload, context)
        self.ctx.events.emit("preview_item", scope="link",
                             item=aw.preview_row(payload, source_type, author_name))
        return True

    async def _preview_one(self, text: str) -> int:
        client, events = self.ctx.client, self.ctx.events
        _url, link = await resolve_link(client, text)
        if link is None:
            events.emit("url_type", text="无法识别")
            raise RuntimeError(f"无法识别链接：{text}")
        events.emit("url_type", text=TYPE_LABELS.get(link.kind, "无法识别"))
        if link.kind in ("live", "live_replay"):
            events.log(f"{TYPE_LABELS[link.kind]}：{text}")
            return 0
        if link.kind == "user":
            events.emit("user_url", sec_uid=link.sec_uid)
            return 0
        if link.kind in ("video", "gallery"):
            detail, reason, _kind = await client.aweme_detail(link.aweme_id)
            if detail is None:
                raise RuntimeError(f"获取作品详情失败：{reason}")
            added = self._add(detail, DownloadContext("single_aweme"), "单个作品")
            events.log(f"解析完成：{aw.title(detail) or link.aweme_id}")
            return int(added)
        if link.kind == "collection":
            return await self._preview_mix(link.mix_id)
        if link.kind == "music":
            return await self._preview_music(link.music_id)
        raise RuntimeError(f"暂不支持此链接类型：{link.kind}")

    async def _preview_mix(self, mix_id: str) -> int:
        client = self.ctx.client
        detail = await client.mix_detail(mix_id) or {}
        name = str(detail.get("mix_name") or detail.get("name") or mix_id)
        author = str((detail.get("author") or {}).get("nickname") or "")
        context = DownloadContext("single_mix", mode="mix", collection_id=mix_id,
                                  collection_name=name)
        count = 0
        async for page in pages(lambda cursor: client.mix_works(mix_id, cursor), what="合集"):
            for item in page.dict_items:
                payload = aw.extract(item)
                if payload and self._add({**payload, "_collection_name": name}, context,
                                         "collection", author):
                    count += 1
        self.ctx.events.log(f"解析完成：合集「{name}」 {count} 个作品")
        return count

    async def _preview_music(self, music_id: str) -> int:
        client = self.ctx.client
        detail = await client.music_detail(music_id) or {}
        sources, title, author = aw.music_info({"music": detail})
        title = title or f"music_{music_id}"
        context = DownloadContext("single_music", title, mode="music", collection_id=music_id,
                                  collection_name=title)
        if sources:
            track = {**detail, "aweme_id": f"music_{music_id}", "music_id": music_id,
                     "media_type": "music", "music_url": sources[0], "desc": title,
                     "author": {"nickname": author}}
            self._add(track, context, "music", author)
            return 1
        count = 0
        async for page in pages(lambda cursor: client.music_works(music_id, cursor),
                                what="音乐作品"):
            for payload in page.dict_items:
                if self._add(payload, DownloadContext("single_aweme"), "music", author):
                    count += 1
        return count

    # ── download ────────────────────────────────────────────────────

    def download(self, selected: Sequence[str]) -> bool:
        ids = [str(value) for value in selected if str(value) in self.items] or list(self.items)
        if not ids:
            raise ValueError("没有可下载的作品，请先解析链接。")
        chosen = [(aweme_id, *self.items[aweme_id]) for aweme_id in ids]
        root = self.ctx.settings.download_root
        root.mkdir(parents=True, exist_ok=True)

        async def run(task) -> None:
            events = self.ctx.events
            total, success, failed, skipped = len(chosen), 0, 0, 0
            events.log("开始下载…")
            with self.ctx.tasks.hold_foreground():
                for index, (aweme_id, payload, context) in enumerate(chosen, start=1):
                    events.emit("row_status", scope="link", id=aweme_id, status="下载中")

                    def on_file(name: str, written: int, expected: Optional[int], _rate: int,
                                index=index) -> None:
                        events.emit_latest("link-file", 0.25, "file_progress", task=str(index),
                                           name=name, written=written, expected=expected)

                    try:
                        outcome, error = await download_one(
                            self.ctx, aweme_id, payload, context,
                            fresh=aw.is_complete(payload), on_file=on_file)
                    except asyncio.CancelledError:
                        events.emit("row_status", scope="link", id=aweme_id, status="已终止")
                        raise
                    if outcome == DOWNLOADED:
                        success += 1
                    elif outcome == EXISTS:
                        skipped += 1
                    else:
                        failed += 1
                        events.log(f"作品 {aweme_id} 下载失败：{error}")
                    events.emit("row_status", scope="link", id=aweme_id,
                                status=_STATUS_TEXT.get(outcome, "失败"))
                    events.emit("progress", completed=success + failed + skipped, total=total,
                                success=success, failed=failed, skipped=skipped)
            events.log(f"任务结束：成功 {success}，失败 {failed}，已存在 {skipped}")
            events.emit("done")

        self.ctx.tasks.submit("link-download", run, scope="link", lane="link",
                              key="link-download:" + ",".join(sorted(ids)))
        return True

    def payloads(self) -> List[Dict[str, Any]]:
        return [payload for payload, _context in self.items.values()]
