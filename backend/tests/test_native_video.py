from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.download import child
from app.modules.download.presets import select_streams, source_selection
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.service import aggregate_download_options


@pytest.fixture
def native_video(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    return ParsedVideo("bilibili", "fixture", "fixture", duration=1, formats=[
        {"format_id": "video", "width": 854, "height": 356, "vcodec": "avc1", "acodec": "none",
         "ext": "mp4", "url": "https://example.com/video", "filesize": 60},
        {"format_id": "audio", "vcodec": "none", "acodec": "mp4a", "ext": "m4a",
         "url": "https://example.com/audio", "filesize": 50},
    ])


@pytest.mark.parametrize("platform,dimensions", [("bilibili", (854, 356)), ("youtube", (1080, 1482))])
def test_native_split_options_use_actual_dimensions(native_video, platform, dimensions):
    native_video = replace(native_video, platform=platform)
    native_video.formats[0].update(width=dimensions[0], height=dimensions[1])
    options = aggregate_download_options(native_video)
    assert [item.id for item in options] == ["video_source_mp4", "audio_mp3"]
    assert options[0].height == dimensions[1]
    assert options[0].estimated_size_bytes == 110
    assert f"{dimensions[0]}×{dimensions[1]}" in options[0].label
    assert select_streams(native_video, "video_source_mp4") == native_video.formats
    with pytest.raises(AppException) as error:
        select_streams(native_video, "video_720_mp4")
    assert error.value.code == "PRESET_UNAVAILABLE"


def test_native_selection_bound_and_standard_tier_compatibility(native_video, monkeypatch):
    small = native_video.formats[0]
    native_video.formats.append({**small, "format_id": "huge", "width": 3840, "height": 1600})
    assert select_streams(native_video, "video_source_mp4")[0] is small
    small.update(width=2560, height=1066)
    assert select_streams(native_video, "video_source_mp4")[0] is small
    small.update(width=1280, height=720)
    assert [item.id for item in aggregate_download_options(native_video)] == ["video_720_mp4", "audio_mp3"]
    assert select_streams(native_video, "video_720_mp4")[0] is small
    small.update(width=854, height=356)
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "legacy")
    assert [item.id for item in aggregate_download_options(native_video)] == ["audio_mp3"]


@pytest.mark.parametrize("invalid", [{"has_drm": True}, {"protocol": "file"}, {"url": None},
                                    {"ext": "exe"}, {"width": float("nan")}, {"height": True},
                                    {"width": 16385}])
def test_invalid_native_source_is_never_offered(native_video, invalid):
    native_video.formats[0].update(invalid)
    assert "video_source_mp4" not in [item.id for item in aggregate_download_options(native_video)]


@pytest.mark.parametrize("change", [{"format_id": "new"}, {"acodec": "opus"}, {"ext": "webm"}, {"protocol": "m3u8_native"}])
def test_changed_audio_snapshot_rejected_before_transfer(native_video, monkeypatch, tmp_path, change):
    snapshot = source_selection(native_video)
    assert snapshot["source_mode"] == "separate_audio_video"
    assert "url" not in snapshot
    native_video.formats[1].update(change)
    guard = Mock(violation=None)
    guard.installed.return_value = nullcontext()
    monkeypatch.setattr(child, "NetworkGuard", Mock(return_value=guard))
    monkeypatch.setattr(child, "guarded_downloader", lambda *a, **k: nullcontext(Mock()))
    monkeypatch.setattr(child, "extract_source", lambda *a: {})
    monkeypatch.setattr(child, "metadata_from_info", lambda _: native_video)
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    with pytest.raises(AppException) as error:
        child.download({"url": "https://www.bilibili.com/video/BV1RA4w6AE74", "platform": "bilibili",
                        "platform_video_id": "fixture", "preset_id": "video_source_mp4",
                        "source_selection": snapshot}, tmp_path, lambda _: pytest.fail("No transfer expected"))
    assert error.value.code == "SOURCE_SELECTION_CHANGED"


def test_existing_muxed_snapshot_shape_preserved(native_video):
    native_video = replace(native_video, platform="douyin")
    native_video.formats[0]["acodec"] = "unknown"
    assert source_selection(native_video) == {
        "format_id": "video", "width": 854, "height": 356, "vcodec": "avc1", "acodec": "unknown",
        "duration": 1, "source_mode": "muxed_video",
    }


@pytest.mark.parametrize("size_mode", ["known", "progress", "actual"])
def test_split_source_combined_cap(native_video, monkeypatch, tmp_path, size_mode):
    guard = Mock(violation=None)
    guard.installed.return_value = nullcontext()
    monkeypatch.setattr(child, "NetworkGuard", Mock(return_value=guard))
    monkeypatch.setattr(child, "extract_source", lambda *a: {})
    monkeypatch.setattr(child, "metadata_from_info", lambda _: native_video)
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    streams = deepcopy(native_video.formats)
    if size_mode != "known":
        # Malformed upstream size hints must never disable the actual byte cap.
        native_video.formats[0]["filesize"] = "60"
        native_video.formats[1]["filesize"] = float("nan")

    def factory(guard, options, *, metadata_only=True):
        downloader = Mock()
        if not metadata_only:
            def transfer(info, **kwargs):
                size = next(item["filesize"] for item in streams if item["format_id"] == info["format_id"])
                if size_mode == "progress":
                    for hook in options["progress_hooks"]:
                        hook({"downloaded_bytes": size, "status": "downloading"})
                from pathlib import Path
                Path(options["outtmpl"]).write_bytes(b"x" * size)
            downloader.process_ie_result.side_effect = transfer
        return nullcontext(downloader)

    monkeypatch.setattr(child, "guarded_downloader", factory)
    monkeypatch.setattr(child.subprocess, "run", lambda *a, **k: pytest.fail("No merge expected"))
    payload = {"url": "https://www.bilibili.com/video/BV1RA4w6AE74", "platform": "bilibili",
               "platform_video_id": "fixture", "preset_id": "video_source_mp4",
               "source_selection": source_selection(native_video),
               "allowance": {"limits": {"video_seconds": 1200, "file_bytes": 100}}}
    with pytest.raises(AppException) as error:
        child.download(payload, tmp_path, lambda _: None)
    assert error.value.code == "FILE_TOO_LARGE"
    if size_mode == "known":
        assert not list(tmp_path.iterdir())
