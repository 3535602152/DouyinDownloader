from __future__ import annotations

from pathlib import Path

import pytest

from scripts.export_settings_schema import javascript
from storage.preferences import config_defaults, preference_state, schema, validate_patch
from storage.settings import Settings


def test_offline_schema_matches_backend_and_has_one_field_per_form():
    definition = schema()
    assert {group["id"] for group in definition["groups"]} == {
        "account", "appearance", "download", "storage", "automation", "advanced"}
    forms = [field["form"] for field in definition["fields"]]
    assert len(forms) == len(set(forms))
    target = Path(__file__).resolve().parents[1] / "gui/web/settings-schema.js"
    assert target.read_text(encoding="utf-8") == javascript()


def test_partial_patch_preserves_other_preferences_and_zero_retry(tmp_path):
    settings = Settings(tmp_path)
    settings.update(video_quality="720p", retry_times=0, browser_fallback={"headless": False})
    config, ui = validate_patch({"scanStrategy": "fast", "theme": "system", "mediaAutoplay": False})
    settings.update(**config)
    settings.update_ui(**ui)
    result = preference_state(Settings(tmp_path))
    assert (result["quality"], result["retry"], result["theme"]) == ("720p", 0, "system")
    assert result["scan_strategy"] == "fast" and result["media_autoplay"] is False
    assert settings.snapshot()["browser_fallback"]["headless"] is False


@pytest.mark.parametrize("values", [
    {"retry": -1}, {"retry": 1.5}, {"retry": "nan"}, {"theme": "blue"},
    {"monitorWorks": 4}, {"fullScanInterval": 1}, {"mediaAutoplay": "false"},
    {"filenameTemplate": "{title}"}, {"folderTemplate": "{unknown}_{id}"},
    {"proxy": "http://localhost"}, {"proxy": "file://localhost:80"},
])
def test_invalid_patch_is_rejected(values):
    with pytest.raises(ValueError):
        validate_patch(values)


def test_aliases_and_quality_codec_match_cli_defaults():
    config, ui = validate_patch({"quality": "original", "livePhotoMerge": False,
                                "browser_fallback": False, "retry": "0"})
    assert config == {"video_quality": "original", "live_photo_merge": False,
                      "browser_fallback": {"enabled": False}, "retry_times": 0}
    assert ui == {}
    assert config_defaults()["video_quality"] == "highest"
