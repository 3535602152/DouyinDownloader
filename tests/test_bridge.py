from __future__ import annotations

import inspect
import re
import time
from pathlib import Path

import pytest

from gui.bridge import WebBridge
from services.app import App
from storage import sources, works
from tests.conftest import gallery_payload, video_payload

WEB = Path(__file__).resolve().parents[1] / "gui" / "web"
STATE_KEYS = {
    "theme", "path", "links", "mode", "limit", "increase", "increase_by_mode",
    "last_user_account", "pending_user_download", "retry", "quality", "monitor_works",
    "full_scan_interval", "browser_fallback", "folderstyle", "author_dir",
    "filename_template", "folder_template", "start_time", "end_time", "download_pinned",
    "homepage_screenshot", "live_photo_merge", "cookie_configured", "cookie_invalid",
    "cookie_count", "account_profile", "tasks",
}


def frontend_calls():
    names = set()
    for script in WEB.rglob("*.js"):
        text = script.read_text(encoding="utf-8")
        names.update(re.findall(r"\bcall\('([a-z_]+)'", text))
        names.update(re.findall(r"(?:bridge\(\)|api)\.([a-z_]+)\(", text))
    return names


def test_every_frontend_call_exists_on_the_bridge():
    names = frontend_calls()
    assert {"poll", "state", "monitors", "creator_home", "add_subscription"} <= names
    missing = sorted(name for name in names if not callable(getattr(WebBridge, name, None)))
    assert missing == []


def test_bridge_parameters_do_not_shadow_pywebview_globals():
    # pywebview generates new Function(parameter_names, body); the body uses
    # window.pywebview and pywebview.stringify to reach the native bridge.
    for name, method in inspect.getmembers(WebBridge, inspect.isfunction):
        if not name.startswith("_"):
            assert not {"window", "pywebview"} & set(inspect.signature(method).parameters), name


def test_window_caption_falls_back_without_a_native_window(bridge):
    # The page hides its own title bar when the backend reports no custom frame.
    assert bridge.get_window_frame() == {"custom": False, "maximized": False}
    assert bridge.window_command("close") is False
    assert bridge.window_start_resize("top") is False


@pytest.mark.parametrize("in_window", [False, True])
def test_qr_login_bridge_dispatches_both_modes(bridge, monkeypatch, in_window):
    called = []
    monkeypatch.setattr(bridge._app.account, "start_qr_login",
                        lambda window=False: called.append(window) or True)
    assert bridge.start_qr_login(in_window)
    assert called == [in_window]


@pytest.fixture
def bridge(tmp_path):
    app = App.create(tmp_path / "data", start_background=False)

    class Media:
        def url(self, path):
            return f"http://media/{Path(path).name}"

        def close(self):
            pass

    instance = WebBridge(tmp_path / "data", app=app, media_server=Media())
    yield instance
    instance.close()


def test_saved_comment_pictures_use_their_local_files(bridge, tmp_path):
    from storage import comments

    picture = tmp_path / "b.jpeg"
    picture.write_bytes(b"jpeg")
    bridge._app.ctx.db.write_sync(comments.save, "9", [{"cid": "b", "text": "二"}], [
        {"comment_id": "b", "index": 0, "urls": ["https://p.example/b.jpeg"],
         "preview_url": "https://p.example/b.webp", "local_path": str(picture)}])
    page = bridge.work_comments("9", 0, "", "", 0, True)
    image = page["comments"][0]["images"][0]
    assert page["source"] == "local" and "local_paths" not in image
    assert image["full"] == image["preview"] == image["mirrors"][0] == "http://media/b.jpeg"
    assert image["mirrors"][1:] == ["https://p.example/b.webp", "https://p.example/b.jpeg"]


def test_saved_comments_use_the_downloaded_sticker_and_pictures(bridge, tmp_path):
    from storage import comments

    folder = tmp_path / "w_comments"
    folder.mkdir()
    (folder / "c_sticker.gif").write_bytes(b"gif")
    (folder / "c_1.jpeg").write_bytes(b"jpeg")
    expired = "https://p.example/s.gif?x-expires=1"
    db = bridge._app.ctx.db
    db.write_sync(comments.save, "9", [{"cid": "c", "text": "表情", "raw": {
        "sticker": {"animate_url": {"url_list": [expired]}}}}], [
        {"comment_id": "c", "index": 0, "urls": ["https://p.example/c.jpeg"],
         "preview_url": "https://p.example/c.webp"}])
    db.write_sync(works.save_files, "9", [
        {"path": str(path), "kind": "comments", "size": 3} for path in folder.iterdir()])
    comment = bridge.work_comments("9", 0, "", "", 0, True)["comments"][0]
    assert comment["sticker"] == "http://media/c_sticker.gif" and "sticker_path" not in comment
    assert comment["sticker_mirrors"] == ["http://media/c_sticker.gif", expired]
    assert comment["images"][0]["full"] == "http://media/c_1.jpeg"


def test_state_contract(bridge):
    state = bridge.state()
    assert STATE_KEYS <= set(state)
    assert state["quality"] == "最高" and state["cookie_configured"] is False
    assert bridge.monitors() == [] and bridge.subscriptions() == []
    assert bridge.full_scan_progress() is None
    assert bridge.history({"page": 1})["items"] == []
    assert set(bridge.get_risk_report()) >= {"risk_24h", "by_type", "recent_events"}


def test_settings_round_trip(bridge, tmp_path):
    saved = bridge.save_settings({
        "path": str(tmp_path / "dl"), "quality": "720p", "retry": 2, "authorDir": "sec_uid",
        "filenameTemplate": "{id}", "folderTemplate": "{date}_{id}", "monitor_works": 3,
        "full_scan_interval": 72, "livePhotoMerge": False,
    })
    assert (saved["quality"], saved["author_dir"], saved["monitor_works"]) == ("720p", "sec_uid", 3)
    assert saved["full_scan_interval"] == 72
    assert saved["scan_strategy"] == "stable"                     # the default
    fast = bridge.save_settings({"path": str(tmp_path / "dl"), "scanStrategy": "fast"})
    assert fast["scan_strategy"] == "fast"
    with pytest.raises(ValueError):
        bridge.save_settings({"filenameTemplate": "{title}"})
    with pytest.raises(ValueError):
        bridge.save_settings({"cookie": "ttwid=1"})


def test_settings_validation_does_not_partially_save(bridge):
    before = bridge.state()
    with pytest.raises(ValueError):
        bridge.save_settings({"quality": "720p", "theme": "dark", "filenameTemplate": "{title}"})
    after = bridge.state()
    assert (after["quality"], after["theme"]) == (before["quality"], before["theme"])
    with pytest.raises(ValueError):
        bridge.save_settings({"retry": 0, "cookie": "ttwid=1"})
    assert bridge.state()["retry"] == before["retry"]


def test_bridge_partial_preferences_and_ui_policy_round_trip(bridge):
    bridge.save_settings({"quality": "720p", "retry": 0})
    result = bridge.save_settings({"theme": "system", "motion": "reduced", "mediaAutoplay": False})
    assert (result["quality"], result["retry"]) == ("720p", 0)
    assert (result["theme"], result["motion"], result["media_autoplay"]) == ("system", "reduced", False)
    assert bridge.set_theme("system") is True
    assert bridge.set_theme("blue") is False


def test_work_details_use_the_shared_ledger(bridge, tmp_path, monkeypatch):
    file = tmp_path / "work.mp4"
    file.write_bytes(b"media")
    bridge._ctx.db.write_sync(works.save_works, [video_payload("123")])
    bridge._ctx.db.write_sync(works.record_success, "123", origin=None,
                             files=[{"path": str(file), "kind": "media", "size": 5}])
    detail = bridge.work_details("123")
    assert detail["status"] == "done" and detail["files"][0]["path"] == str(file)
    assert "payload" not in detail and "context" not in detail
    opened = []
    monkeypatch.setattr(bridge, "open_directory", lambda path: opened.append(path) or True)
    assert bridge.open_work_directory("123") is True
    assert opened == [str(tmp_path)]
    file.unlink()
    assert bridge.open_work_directory("123") is False


def test_local_gallery_plays_from_disk(bridge, tmp_path):
    ctx = bridge._ctx
    folder = tmp_path / "files"
    folder.mkdir()
    paths = {}
    for name in ("g_1.jpeg", "g_2.jpeg", "g_live_1.mp4", "g_cover.jpeg", "g_music.mp3"):
        (folder / name).write_bytes(b"\xff\xd8\xff" + name.encode())
        paths[name] = str(folder / name)
    payload = gallery_payload("77", sec_uid="C", images=2, live_at=(0,))
    source_id, _ = ctx.db.write_sync(sources.add, "creator", "C", nickname="主播")
    ctx.db.write_sync(works.save_works, [payload])
    ctx.db.write_sync(sources.record_listing, source_id, ["77"], seen_at=int(time.time()))
    ctx.db.write_sync(works.record_success, "77", origin=source_id, files=[
        {"path": paths["g_1.jpeg"], "kind": "media", "position": 1},
        {"path": paths["g_2.jpeg"], "kind": "media", "position": 2},
        {"path": paths["g_live_1.mp4"], "kind": "live", "position": 1},
        {"path": paths["g_cover.jpeg"], "kind": "cover"},
        {"path": paths["g_music.mp3"], "kind": "music"},
    ])
    home = bridge.creator_home("C")
    card = home["works"][0]
    assert card["media_type"] == "image" and card["cover_source"].startswith("http://media/")
    detail = bridge.creator_work_detail("C", "77")
    assert detail["media_state"] == "present"
    assert detail["media_sources"] == ["http://media/g_1.jpeg", "http://media/g_2.jpeg"]
    assert detail["live_sources"] == ["http://media/g_live_1.mp4", ""]
    assert detail["music_source"] == "http://media/g_music.mp3"
    assert detail["cover_source"].startswith("data:image/jpeg;base64,")
    rows = bridge.monitors()
    assert rows[0]["nickname"] == "主播" and rows[0]["last_24h_downloaded"] == 1


def test_poll_coalesces_monitor_refresh(bridge):
    for _ in range(3):
        bridge._ctx.events.emit("monitor_refresh")
    bridge._ctx.events.log("hello")
    events = bridge.poll()
    assert [event["type"] for event in events].count("monitor_refresh") == 1
    assert {"type": "log", "text": "hello\n"} in events


def test_creator_home_hidden_only_pages_the_archive(bridge):
    ctx = bridge._ctx
    source_id, _ = ctx.db.write_sync(sources.add, "creator", "H", nickname="主播")
    ids = [str(7100000000000000000 + n) for n in range(40)]
    ctx.db.write_sync(works.save_works, [video_payload(aweme_id, sec_uid="H") for aweme_id in ids])
    ctx.db.write_sync(sources.record_listing, source_id, ids, seen_at=int(time.time()))
    # The oldest work sits past the first page of the normal list.
    ctx.db.write_sync(works.set_visibility, ids[0], "hidden", "主页已看不到")
    assert all(not work["offline_label"] for work in bridge.creator_home("H")["works"])
    home = bridge.creator_home("H", True, 0, 0, True)
    assert [work["aweme_id"] for work in home["works"]] == [ids[0]]
    assert home["works"][0]["offline_label"] == "作品已隐藏" and home["has_more"] is False
