from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

import desktop_app
from common.paths import DATABASE_FILE_NAME, migrate_portable_data
from storage.settings import DEFAULT_DOWNLOAD_DIR, Settings


@pytest.mark.parametrize("frozen,override", [(False, ""), (True, ""),
                                           (False, "DOUYIN_DOWNLOADER_DATA_DIR"),
                                           (True, "DY_DOWNLOADER_DATA_DIR")])
def test_startup_uses_user_data_without_importing_a_checkout(tmp_path, monkeypatch,
                                                           frozen, override):
    monkeypatch.delenv("DOUYIN_DOWNLOADER_DATA_DIR", raising=False)
    monkeypatch.delenv("DY_DOWNLOADER_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    install = tmp_path / "install"
    install.mkdir()
    (install / "config.yml").write_text("path: checkout-or-portable\n", encoding="utf-8")
    legacy = tmp_path / "local" / "DyDownloader"
    legacy.mkdir(parents=True)
    (legacy / "config.yml").write_text("path: legacy\n", encoding="utf-8")
    monkeypatch.setattr(desktop_app, "ROOT", install)
    monkeypatch.setattr(desktop_app.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(desktop_app.sys, "executable", str(install / "app.exe"))
    if override:
        monkeypatch.setenv(override, str(tmp_path / "isolated"))
    folder = desktop_app.prepare_data_dir()
    config = yaml.safe_load((folder / "config.yml").read_text(encoding="utf-8"))
    expected = DEFAULT_DOWNLOAD_DIR if override else "checkout-or-portable" if frozen else "legacy"
    assert config["path"] == expected
    assert (install / "config.yml").read_text(encoding="utf-8") == "path: checkout-or-portable\n"
    (folder / "config.yml").write_text("path: chosen\n", encoding="utf-8")
    assert desktop_app.prepare_data_dir() == folder
    assert Settings(folder).download_root == folder / "chosen"


@pytest.mark.parametrize("stem", ["DouyinDownloader", "dy_downloader"])
def test_migration_keeps_wal_and_subscription_database_from_the_same_root(tmp_path, stem):
    source, other, target = (tmp_path / name for name in ("source", "other", "target"))
    source.mkdir()
    other.mkdir()
    # Another candidate's subscription database must never be paired with this main database.
    (other / "DouyinDownloader.subscriptions.db").write_bytes(b"wrong database")
    connections = []
    try:
        for suffix in (".db", ".subscriptions.db"):
            connection = sqlite3.connect(source / (stem + suffix))
            connections.append(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE proof (value TEXT)")
            connection.execute("INSERT INTO proof VALUES ('preserved')")
            connection.commit()
        migrate_portable_data(target, [other, source])
        for name in (DATABASE_FILE_NAME, "DouyinDownloader.subscriptions.db"):
            path = target / name
            assert path.is_file()
            with sqlite3.connect(path) as copied:
                assert copied.execute("SELECT value FROM proof").fetchone() == ("preserved",)
    finally:
        for connection in connections:
            connection.close()


def test_default_download_path_is_portable_and_preserves_explicit_paths(tmp_path):
    settings = Settings(tmp_path)
    assert settings.download_root == tmp_path / "Downloaded"
    settings.update(path="")
    assert settings.download_root == tmp_path / "Downloaded"
    settings.update(path=str(tmp_path / "custom"))
    assert Settings(tmp_path).download_root == tmp_path / "custom"
    settings.update(path="D:/ExistingArchive")
    assert Settings(tmp_path).get("path") == "D:/ExistingArchive"
    example = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.example.yml")
                             .read_text(encoding="utf-8"))
    assert example["path"] == DEFAULT_DOWNLOAD_DIR


def test_failed_database_copy_stops_before_initializing_an_empty_library(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / DATABASE_FILE_NAME).write_bytes(b"corrupt snapshot")
    target = tmp_path / "target"
    with pytest.raises(RuntimeError, match="Could not migrate database"):
        migrate_portable_data(target, [source])
    assert not (target / DATABASE_FILE_NAME).exists()
    assert (source / DATABASE_FILE_NAME).read_bytes() == b"corrupt snapshot"


def test_existing_library_does_not_import_an_unrelated_subscription_database(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / DATABASE_FILE_NAME).write_bytes(b"existing library")
    (source / DATABASE_FILE_NAME).write_bytes(b"another library")
    (source / "DouyinDownloader.subscriptions.db").write_bytes(b"another account")
    migrate_portable_data(target, [source])
    assert (target / DATABASE_FILE_NAME).read_bytes() == b"existing library"
    assert not (target / "DouyinDownloader.subscriptions.db").exists()


def test_partial_target_is_not_paired_with_another_main_database(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / DATABASE_FILE_NAME).write_bytes(b"another library")
    subscription = target / "DouyinDownloader.subscriptions.db"
    subscription.write_bytes(b"existing subscription")
    with pytest.raises(RuntimeError, match="refusing to pair"):
        migrate_portable_data(target, [source])
    assert not (target / DATABASE_FILE_NAME).exists()
    assert subscription.read_bytes() == b"existing subscription"


def test_window_keeps_web_storage_and_waits_for_the_page(tmp_path, monkeypatch):
    import sys
    import types

    import gui.web_app as web_app

    started = {}
    created = {}
    shown = []
    closed = []

    class _Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def fire(self):
            for handler in self.handlers:
                handler()

    window = types.SimpleNamespace(events=types.SimpleNamespace(closed=_Event(), loaded=_Event()),
                                   show=lambda: shown.append(True))

    def create_window(*args, **kwargs):
        created.update(kwargs)
        return window

    def start(**kwargs):
        started.update(kwargs)
        assert created["hidden"] is True and shown == []
        window.events.loaded.fire()
        # Reloading the page must not restore a window the user has hidden.
        window.events.loaded.fire()

    fake = types.SimpleNamespace(create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(web_app, "WebBridge", lambda folder: types.SimpleNamespace(
        close=lambda: closed.append(True)))
    web_app.run_web_app(tmp_path)
    # Private mode would wipe localStorage (monitor columns, theme) on close.
    assert started["private_mode"] is False
    assert started["storage_path"] == str(tmp_path / "webview")
    assert shown == [True]
    assert closed == [True]

    def failed_start(**kwargs):
        raise RuntimeError("Native renderer failed")

    fake.start = failed_start
    with pytest.raises(RuntimeError, match="Native renderer failed"):
        web_app.run_web_app(tmp_path)
    assert closed == [True, True]


def test_local_ui_assets_are_revalidated_on_every_load(tmp_path, monkeypatch):
    import bottle

    from gui import web_app

    monkeypatch.setattr(bottle, "static_file", bottle.static_file)
    web_app._revalidate_local_assets()
    web_app._revalidate_local_assets()
    (tmp_path / "index.html").write_text("<p>new</p>", encoding="utf-8")
    response = bottle.static_file("index.html", root=str(tmp_path))
    assert response.headers["Cache-Control"] == "no-cache"
