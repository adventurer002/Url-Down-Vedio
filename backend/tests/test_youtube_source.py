import asyncio
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pydantic import SecretStr
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.download.presets import select_streams
from app.modules.media.audio import select_audio_stream
from app.modules.video import preflight
from app.modules.video.parsers import dispatch, runtime, youtube_runtime, ytdlp, ytdlp_worker
from app.modules.video.parsers.base import ParsedVideo
from app.modules.video.service import aggregate_download_options

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
REAL_WHICH = shutil.which
INFO = {"id": "dQw4w9WgXcQ", "extractor_key": "Youtube", "title": "public fixture", "duration": 10,
        "formats": [
            {"format_id": "audio", "url": "https://media.googlevideo.com/audio", "ext": "webm",
             "acodec": "opus", "vcodec": "none", "abr": 64},
            {"format_id": "video", "url": "https://media.googlevideo.com/video", "ext": "mp4",
             "acodec": "none", "vcodec": "avc1", "height": 720},
        ]}


def test_operator_probe_resolves_project_imports_without_pythonpath(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/youtube_stage_c_probe.py"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([
        sys.executable, str(script), "--host", "unused", "--identity", "unused",
        "--python", "unused", "--cache", "unused", "--client-config", "unused",
        "--deno", "unused", "--report", str(tmp_path / "report.json"),
        "--url", "https://www.bilibili.com/video/BV1g2JX6nESP",
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "A YouTube video is required" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_proxy_template_resolves_source_domains_through_encrypted_proxy_dns():
    import yaml

    template = Path(__file__).resolve().parents[1] / "deploy/youtube/youtube-proxy.yaml.example"
    config = yaml.safe_load(template.read_text())
    dns = config["dns"]
    assert dns["enable"] and dns["use-system-hosts"] is False
    assert dns["listen"].startswith("127.0.0.1:")
    group_names = {group["name"] for group in config["proxy-groups"]}
    for endpoint in dns["nameserver"]:
        address, group = endpoint.split("#", 1)
        assert address.startswith("https://") and group in group_names
    assert dns["proxy-server-nameserver"]


@pytest.fixture(autouse=True)
def youtube_settings(isolated_product_settings, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YOUTUBE_PROXY", SecretStr("http://operator:secret@proxy.example:8080"))
    monkeypatch.setattr(settings, "YTDLP_PROXY", SecretStr("file:///invalid-global"))
    monkeypatch.setattr(settings, "YTDLP_JS_RUNTIME", "deno")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/opt/deno")


class FixtureDownloader:
    def __init__(self, info=None):
        self.extract_info = Mock(return_value=deepcopy(INFO if info is None else info))
        self.limits = []

    @contextmanager
    def source_budget(self, limit):
        self.limits.append(limit)
        yield


@pytest.mark.parametrize("url", [URL, "https://youtu.be/dQw4w9WgXcQ?t=3", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"])
def test_youtube_adapter_preserves_identity_and_audio_capability(url):
    downloader = FixtureDownloader()
    info = dispatch.extract_source(downloader, url)
    downloader.extract_info.assert_called_once_with(URL, download=False)
    assert downloader.limits == [32]
    video = ytdlp.metadata_from_info(info)
    assert {item.id for item in aggregate_download_options(video)} == {"audio_mp3", "video_720_mp4"}
    assert select_streams(video, "audio_mp3")[0]["format_id"] == "audio"
    assert select_audio_stream(video)["vcodec"] == "none"


@pytest.mark.parametrize("url", ["https://www.youtube.com/playlist?list=abc", "https://www.youtube.com/@channel",
    "https://www.youtube.com/watch?v=invalid", URL + "&v=", "https://youtu.be/invalid"])
def test_non_video_links_rejected_before_sdk(url):
    downloader = FixtureDownloader()
    with pytest.raises(AppException): dispatch.extract_source(downloader, url)
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("changes,code", [({"id": "aaaaaaaaaaa"}, "VIDEO_ID_CHANGED"),
    ({"extractor_key": "BiliBili"}, "VIDEO_ID_CHANGED"), ({"_type": "playlist"}, "UNSUPPORTED_URL"),
    ({"is_live": True}, "UNSUPPORTED_URL"), ({"live_status": "is_upcoming"}, "UNSUPPORTED_URL"),
    ({"availability": "needs_auth"}, "UNSUPPORTED_URL"), ({"availability": "private"}, "UNSUPPORTED_URL")])
def test_identity_and_public_scope_fail_closed(changes, code):
    downloader = FixtureDownloader({**INFO, **changes})
    with pytest.raises(AppException) as error: dispatch.extract_source(downloader, URL)
    assert error.value.code == code
    downloader.extract_info.assert_called_once()


@pytest.mark.parametrize("message,reason", [("Sign in to confirm you're not a bot SECRET", "restricted"),
    ("HTTP Error 429 SECRET", "rate_limited"), ("HTTP Error 403 expired signature SECRET", None)])
def test_sdk_failure_never_switches_proxy_or_repeats_extraction(message, reason):
    downloader = FixtureDownloader()
    downloader.extract_info.side_effect = RuntimeError(message)
    with pytest.raises(AppException) as error: dispatch.extract_source(downloader, URL)
    assert getattr(error.value, "source_failure", None) == reason
    assert "SECRET" not in error.value.message
    downloader.extract_info.assert_called_once()


@pytest.mark.parametrize("message", ["ERROR: [youtube] test: This video is unavailable", "This video was removed by the uploader"])
def test_unavailable_video_is_a_content_error_without_platform_cooldown(message):
    downloader = FixtureDownloader()
    downloader.extract_info.side_effect = RuntimeError(message)
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, URL)
    assert error.value.code == "UNSUPPORTED_URL"
    assert getattr(error.value, "source_failure", None) is None


def test_storyboards_and_drm_not_advertised():
    info = deepcopy(INFO)
    info["formats"] += [{**info["formats"][0], "has_drm": True}, {"ext": "mhtml", "protocol": "mhtml", "url": "https://example.com/"}]
    assert len(dispatch.extract_source(FixtureDownloader(info), URL)["formats"]) == 2


def test_muxed_only_source_cannot_be_silently_used_for_audio_export():
    muxed = ParsedVideo("youtube", "dQw4w9WgXcQ", "muxed", formats=[
        {"url": "https://media.googlevideo.com/video", "ext": "mp4", "height": 480, "acodec": "mp4a", "vcodec": "avc1"}])
    assert [item.id for item in aggregate_download_options(muxed)] == ["video_480_mp4"]
    with pytest.raises(AppException) as error: select_streams(muxed, "audio_mp3")
    assert error.value.code == "AUDIO_STREAM_UNAVAILABLE"
    assert len(select_streams(muxed, "video_480_mp4")) == 1


def test_youtube_retries_disabled_and_proxy_does_not_leak_to_domestic_sources():
    options = ytdlp.extraction_options(runtime.server_proxy(URL), url=URL)
    assert options["retries"] == options["extractor_retries"] == options["fragment_retries"] == 0
    assert options["js_runtimes"] == {"deno": {"path": "/opt/deno"}}
    assert options["remote_components"] == [] and options["cookiefile"] is None
    for url in ("https://www.bilibili.com/video/BV1g2JX6nESP", "https://v.douyin.com/abc"):
        assert runtime.server_proxy(url) is None
        assert ytdlp.extraction_options(None, url=url)["js_runtimes"] == {}


def test_locked_ejs_scripts_have_matching_hashes_and_corruption_fails_closed(monkeypatch):
    import yt_dlp_ejs
    version, scripts = youtube_runtime.packaged_ejs()
    assert version and len(scripts) == 2
    monkeypatch.setattr(yt_dlp_ejs.yt.solver, "core", lambda: "modified script")
    with pytest.raises(AppException) as error: youtube_runtime.packaged_ejs()
    assert error.value.code == "YTDLP_RUNTIME_UNAVAILABLE"


def test_packaged_solver_global_initialization_without_network():
    node = REAL_WHICH("node")
    if not node or not Path(node).is_file(): pytest.skip("Node unavailable for supplemental EJS initialization check")
    _, scripts = youtube_runtime.packaged_ejs()
    marker = "VIDEOHUB_OFFLINE_EJS_OK"
    result = subprocess.run([node, "--input-type=module"], input=youtube_runtime.smoke_script(scripts, marker),
        text=True, capture_output=True, timeout=3)
    assert result.returncode == 0 and result.stdout.strip() == marker


@pytest.mark.parametrize("deno", ["deno 2.2.0", "not deno", "deno 2.3"])
def test_offline_probe_rejects_old_or_invalid_deno(monkeypatch, tmp_path, deno):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=deno))
    monkeypatch.setattr(youtube_runtime.subprocess, "run", run)
    with pytest.raises(AppException): youtube_runtime.inspect_runtime(tmp_path)
    run.assert_called_once()


def test_offline_probe_checks_scripts_without_granting_network_permissions(monkeypatch, tmp_path):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="deno 2.3.0" if command[-1] == "--version" else "VIDEOHUB_OFFLINE_EJS_OK\n")
    monkeypatch.setattr(youtube_runtime.subprocess, "run", run)
    result = youtube_runtime.inspect_runtime(tmp_path)
    assert result["sandbox_verified"] and result["ejs_verified"]
    command, controls = calls[1]
    assert "--no-remote" in command and "--no-npm" in command and "--cached-only" in command
    assert not any(item.startswith("--allow-") for item in command)
    assert controls["timeout"] == 3 and controls["env"]["DENO_DIR"].startswith(str(tmp_path))


@pytest.mark.parametrize("stall", [False, True])
async def test_runtime_adapter_uses_real_watched_child_and_sanitizes_failures(monkeypatch, tmp_path, stall):
    executable = tmp_path / "deno"
    executable.write_text("#!/bin/sh\n" + ("exec sleep 20\n" if stall else
        "if [ \"$1\" = \"--version\" ]; then echo 'deno 2.3.0'; else cat >/dev/null; echo VIDEOHUB_OFFLINE_EJS_OK; fi\n"))
    executable.chmod(0o700)
    monkeypatch.setenv("VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setenv("YTDLP_JS_RUNTIME", "deno")
    monkeypatch.setenv("YTDLP_DENO_PATH", str(executable))
    if stall:
        with pytest.raises(AppException) as error: await youtube_runtime.DenoEjsCapabilities().check()
        assert error.value.code == "YTDLP_RUNTIME_UNAVAILABLE" and str(tmp_path) not in error.value.message
    else:
        result = await youtube_runtime.DenoEjsCapabilities().check()
        assert result["deno_version"] == "2.3.0" and result["sandbox_verified"]


async def test_preflight_missing_proxy_is_not_reported_as_direct_or_standalone(monkeypatch):
    monkeypatch.setattr(settings, "YOUTUBE_PROXY", SecretStr(""))
    monkeypatch.setattr(preflight.DenoEjsCapabilities, "check", AsyncMock(return_value={"sandbox_verified": True}))
    monkeypatch.setattr(preflight, "probe_version", lambda *args: "4.4.2")
    monkeypatch.setattr(preflight, "ffmpeg_features", lambda _: {"fps_mode": False, "vsync": True, "render_verified": False})
    parser = SimpleNamespace(parse=AsyncMock())
    monkeypatch.setattr(preflight, "validate_url", AsyncMock(return_value=URL))
    result = await preflight.check(URL, parser)
    assert not result["passed"] and result["egress"] == "unavailable"
    assert not result["standalone_verified"] and not result["restart_verified"]
    parser.parse.assert_not_awaited()
    assert "secret" not in json.dumps(result)


def test_youtube_metadata_download_and_subtitle_use_the_same_scoped_backend(monkeypatch, tmp_path):
    from app.modules.download import child
    from app.modules.transcript import subtitle_child
    proxy = runtime.server_proxy(URL)
    fixtures = []
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    for module in (ytdlp_worker, child, subtitle_child):
        guard = Mock(violation=None)
        guard.installed.return_value = nullcontext()
        monkeypatch.setattr(module, "NetworkGuard", lambda value: guard)
        downloader = FixtureDownloader()
        fixtures.append(downloader)
        def factory(guard, options, *, downloader=downloader, **kwargs):
            assert options["proxy"] == proxy and options["retries"] == 0
            return nullcontext(downloader)
        monkeypatch.setattr(module, "guarded_downloader", factory)
        if module is ytdlp_worker:
            assert module.extract_metadata(URL, proxy)["data"]["platform_video_id"] == "dQw4w9WgXcQ"
        elif module is child:
            def stop(event): raise AppException("TEST_STOP", "stop")
            with pytest.raises(AppException): module.download({"url": URL, "platform": "youtube", "platform_video_id": "dQw4w9WgXcQ"}, tmp_path, stop)
        else:
            events = []
            module.fetch({"url": URL, "platform": "youtube", "platform_video_id": "dQw4w9WgXcQ"}, tmp_path, events.append)
            assert events[-1] == {"subtitle_found": False}
    assert all(fixture.limits == [32] for fixture in fixtures)
