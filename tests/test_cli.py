from __future__ import annotations

import asyncio

import pytest

from cli.main import main
from services.app import App
from tests.conftest import video_payload


@pytest.mark.parametrize("outcome,links,expected", [
    ("downloaded", ["1"], 0),
    ("exists", ["1"], 0),
    ("failed", ["1"], 1),
    ("exception", ["1"], 1),
    ("cancelled", ["1"], 1),
    ("downloaded", ["missing"], 1),
    ("downloaded", ["missing", "1"], 1),
])
def test_cli_exit_status(tmp_path, monkeypatch, outcome, links, expected):
    create = App.create
    instances = []

    async def detail(aweme_id):
        return (video_payload(aweme_id), "", "") if aweme_id == "1" else (None, "missing", "")

    async def download(*args, **kwargs):
        if outcome == "exception":
            raise RuntimeError("download crashed")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return outcome, "transfer failed" if outcome == "failed" else ""

    def create_offline(data_dir, **kwargs):
        instance = create(data_dir, **kwargs)
        monkeypatch.setattr(instance.ctx.client, "aweme_detail", detail)
        close = instance.close

        def close_and_record():
            close()
            instances.append(instance)

        monkeypatch.setattr(instance, "close", close_and_record)
        return instance

    monkeypatch.setattr(App, "create", create_offline)
    monkeypatch.setattr("services.links.download_one", download)
    urls = [f"https://www.douyin.com/video/{value}" for value in links]
    assert main([*urls, "--data-dir", str(tmp_path), "--path", str(tmp_path / "media")]) == expected
    assert len(instances) == 1 and not instances[0].ctx.runtime.loop.is_running()
