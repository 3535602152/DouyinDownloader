<!-- Generated: 2026-03-27 | Updated: 2026-09-29 (backend rewrite, 0.4.0) -->

# DouyinDownloader

## Purpose
A Douyin (Chinese short-video platform) downloader with a desktop GUI. It downloads videos,
image galleries (including live photos), mixes and music without watermarks, monitors followed
creators, subscribes to the account's likes / favorites, and collects comments.

Front ends:

- **Desktop GUI** — pywebview shell (`desktop_app.py` → `gui/web_app.py`) with the web frontend
  in `gui/web/`. The JS API exposed by `gui/bridge.WebBridge` is the compatibility contract.
- **CLI** — `douyin-dl <links…>` (`cli/main.py`), a thin wrapper over the same services.

## Architecture (layers, top depends on bottom only)

| Directory | Purpose |
|-----------|---------|
| `gui/` | `bridge.py` (every JS method; thin, delegates to services), `web_app.py`, `media_server.py`, `web/` frontend |
| `cli/` | Link download command line |
| `services/` | Feature logic: `app.py` wires everything; `links`, `user_content`, `creators`, `monitor`, `subscriptions`, `scanning` (one scanner for creator/like/collect via `Policy`), `library`, `comments`, `account`, `views` (UI payloads) |
| `engine/` | `runtime.py` (one asyncio loop thread + `TaskManager` lanes), `events.py` (EventBus → `poll()`), `downloader.py` (one work → files), `queue.py` (background download queue), `fetcher.py`, `live_photo.py`, `audio.py` |
| `storage/` | One SQLite file via `db.Database` (single writer thread, reader pool). `schema.py`, `works.py` (download ledger + files), `sources.py`, `scans.py`, `comments.py`, `profiles.py` (cached creator homepage headers), `settings.py`, `files.py` / `folders.py` (layout), `legacy.py` (one-time import of pre-0.4 tables) |
| `douyin/` | `client.py` (DouyinClient), `risk.py` (RiskGate), `pages.py`, `aweme.py` (payload parsing), `urls.py`, `browser.py` (Playwright page channel + QR login), `signing/` |
| `common/` | Logging, filesystem helpers, data-dir paths |
| `tests/` | Pytest suite; `conftest.py` has `FakeClient` / `FakeFetcher` — tests never touch the network |

## Core Rules
- **One ledger**: `downloads` has one row per aweme shared by every feature. A work already `done`
  is "已存在" everywhere. Completeness = media + JSON; cover and gallery music are best-effort
  extras retried at low priority.
- **Sources**: creators, likes and collects are rows in `sources`; membership lives in
  `source_works` (`listed` / `missing` / `removed`). Files are deleted only when the work's
  `downloads.origin` is being removed and no other source still lists it.
- **Background queue priority**: creators → like/collect subscriptions → extras; it yields while a
  user-started download runs.
- **Folder layout** is user-visible and must stay stable (`关注主播/…`, `喜欢|收藏/…`, `用户主页/…`,
  `我的内容/…`, `单独下载/…`).
- All database access goes through `Database.read/write` (async) or `read_sync/write_sync`;
  storage functions take a `sqlite3.Connection` as their first argument.
- Never block the runtime loop; long work runs as a `TaskManager` task in its lane.

## Working In This Directory
- Minimum Python is 3.12: start modules with `from __future__ import annotations`; no `match`.
- Keep the repository self-contained: no sibling checkouts, machine paths, cookies, media,
  databases or captured pages in Git.
- Tests: `python -m pytest -q`. Lint: `ruff check .` (py312, line length 100, rules `E,F,W,I`).
- Version lives in `VERSION`, `__init__.py` and `pyproject.toml` — keep them in sync.

## Dependencies
- Required: `aiohttp`, `pyyaml`, `gmssl`, `pillow`, `pywebview`.
- `browser` extra: `playwright` (page-channel fallback, QR login, homepage screenshots).
- `dev` extra: `pytest`, `pytest-asyncio`, `ruff`.

<!-- MANUAL: -->
