"""Keep production FFmpeg 4.4 compatible without scanning frames during probes."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.clips import media_child
from app.modules.clips.media import FFmpegClipAdapter
from app.modules.video import preflight


def test_probe_uses_stream_side_data_wrapper_and_preserves_rotation(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixed media")
    monkeypatch.setattr(media_child.shutil, "which", lambda _: "/usr/bin/ffprobe")

    def probe(command, **kwargs):
        entries = command[command.index("-show_entries") + 1].split(":")
        assert "stream_side_data_list" in entries
        assert "stream_side_data=rotation" not in entries
        assert "side_data=rotation" not in entries
        assert kwargs["timeout"] == 15
        return SimpleNamespace(stdout=json.dumps({
            "format": {"start_time": "0", "duration": "5"},
            "streams": [
                {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}},
                {"index": 1, "codec_type": "video", "width": 852, "height": 480,
                 "time_base": "1/16000", "side_data_list": [{"rotation": 90}]},
                {"index": 2, "codec_type": "audio", "time_base": "1/44100"},
            ],
        }).encode())

    monkeypatch.setattr(media_child.subprocess, "run", probe)
    result = media_child.inspect(source)
    assert result["video_index"] == 1
    assert result["audio_index"] == 2
    assert result["rotation"] == 90
    assert result["duration"] == 5


@pytest.mark.parametrize("flag", ["vsync", "fps_mode"])
async def test_renderer_uses_supported_frame_sync_and_reuses_its_probe(tmp_path, monkeypatch, flag):
    adapter = FFmpegClipAdapter(15, 1024 * 1024)
    monkeypatch.setattr(adapter, "executable", lambda: "/usr/bin/ffmpeg")
    adapter.run = AsyncMock(return_value={"features": {flag: True}})
    adapter.inspect = AsyncMock(return_value={
        "duration": 2, "audio_start": 0, "video_start": 0, "sha256": "fixed-digest",
    })
    info = {"origin": 0, "audio_start": 0, "video_start": 0, "audio_index": 1, "video_index": 0}
    result = await adapter.render(tmp_path / "source.mp4", tmp_path / "clip.mp4", info, 0, 2)
    command = adapter.run.await_args_list[1].args[0]
    assert command[command.index("-" + flag) + 1] == "vfr"
    other = "-fps_mode" if flag == "vsync" else "-vsync"
    assert other not in command
    assert result["decoded"] is True
    await adapter.render(tmp_path / "source.mp4", tmp_path / "second.mp4", info, 0, 2)
    assert len([call for call in adapter.run.await_args_list if call.args[0][-1] == "features"]) == 1


@pytest.mark.parametrize("help_text,expected", [
    ("-vsync              video sync method", "vsync"),
    ("-fps_mode           set framerate mode", "fps_mode"),
    ("-fps_mode[:<stream_spec>]  set framerate mode", "fps_mode"),
])
def test_feature_detection_accepts_old_and_new_ffmpeg_help(tmp_path, monkeypatch, help_text, expected):
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=help_text))
    features = preflight.ffmpeg_features("/usr/bin/ffmpeg")
    assert features[expected] is True
    assert features["render_verified"] is False
