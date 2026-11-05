"""Browser SSRF boundaries and original-audio/DASH source regressions."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import socket
import threading
import tempfile
from pathlib import Path
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers import douyin, douyin_browser as browser
from app.modules.video.parsers.ytdlp import metadata_from_info
from app.modules.download.presets import select_streams, source_selection
from app.modules.download.child import ffmpeg_command
from app.modules.media.audio import select_audio_stream
from test_douyin_source import detail, IDENTIFIER


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"],
    ["::1"], ["fc00::1"], ["8.8.8.8", "192.168.1.1"], ["::ffff:127.0.0.1"], []])
def test_browser_tunnel_rejects_every_nonpublic_dns_result(monkeypatch, addresses):
    monkeypatch.setattr(browser.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses])
    with pytest.raises(ValueError):
        browser.public_addresses("public-looking.example")


@contextmanager
def proxy_server():
    temporary = tempfile.TemporaryDirectory(prefix="vhdyt-", dir="/tmp")
    path = Path(temporary.name) / "proxy.sock"
    with browser.UnixServer(str(path), browser.Tunnel) as server:
        server.slots = threading.BoundedSemaphore(2)
        server.budget_lock = threading.Lock()
        server.connections = 0
        server.budget = [0]
        server.hosts, server.errors = Counter(), Counter()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, path
        finally:
            server.shutdown()
            thread.join(timeout=2)
            temporary.cleanup()


@pytest.mark.parametrize("target", ["127.0.0.1:443", "user:secret@public.example:443", "public.example:22",
                                    "public.example:443/path", "www.google.com:443", "douyin.com.evil.example:443"])
def test_proxy_never_connects_to_rejected_destination(monkeypatch, target):
    monkeypatch.setattr(browser, "public_addresses", lambda host: (_ for _ in ()).throw(ValueError("Private DNS")))
    connect = Mock(side_effect=AssertionError("No upstream expected"))
    monkeypatch.setattr(browser.socket, "create_connection", connect)
    with proxy_server() as (_, path), socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        assert client.recv(4096).startswith(b"HTTP/1.1 403")
    connect.assert_not_called()


def test_proxy_pins_checked_ip_instead_of_resolving_hostname_again(monkeypatch):
    monkeypatch.setattr(browser, "public_addresses", lambda host: ["8.8.8.8"])
    upstream = Mock()
    connect = Mock(return_value=upstream)
    monkeypatch.setattr(browser.socket, "create_connection", connect)
    monkeypatch.setattr(browser, "bridge", lambda *a: None)
    with proxy_server() as (server, path), socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(b"CONNECT www.douyin.com:443 HTTP/1.1\r\n\r\n")
        assert client.recv(4096).startswith(b"HTTP/1.1 200")
        assert server.connections == 1
    connect.assert_called_once_with(("8.8.8.8", 443), timeout=5)
    upstream.close.assert_called_once()


@pytest.fixture
def dash_detail(detail, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "DOUYIN_SOURCE_BACKEND", "browser")
    video = detail["video"]
    video["format"] = "dash"
    video["bit_rate"] = [{"format": "dash", "is_h265": 1, "gear_name": "dash720", "bit_rate": 200000,
                          "play_addr": {**video["play_addr"], "width": 1280, "height": 720}}]
    video["bit_rate_audio"] = [{"audio_meta": {"media_type": "audio", "file_id": "original-audio",
        "size": 12345, "bitrate": 128000, "url_list": {"main_url": "https://v.example.douyinvod.com/original.m4a"}}}]
    detail["music"] = {"play_url": {"url_list": ["https://v.example.douyinvod.com/background.mp3"]}}
    return detail


def test_browser_dash_video_requires_original_independent_audio(dash_detail):
    video = metadata_from_info(douyin.info_from_detail(dash_detail, IDENTIFIER))
    streams = select_streams(video, "video_720_mp4")
    assert len(streams) == 2
    assert streams[0]["acodec"] == "none"
    assert streams[1]["vcodec"] == "none"
    assert select_audio_stream(video)["url"].endswith("/original.m4a")
    assert not any("background.mp3" in item["url"] for item in video.formats)
    dash_detail["video"]["bit_rate_audio"] = []
    video = metadata_from_info(douyin.info_from_detail(dash_detail, IDENTIFIER))
    with pytest.raises(AppException) as error:
        select_streams(video, "video_720_mp4")
    assert error.value.code == "PRESET_UNAVAILABLE"


def test_browser_format_identity_survives_upstream_reordering(dash_detail):
    dash_detail["video"]["bit_rate"].append({**deepcopy(dash_detail["video"]["bit_rate"][0]),
                                            "gear_name": "dash360", "bit_rate": 100000})
    before = source_selection(metadata_from_info(douyin.info_from_detail(dash_detail, IDENTIFIER)))
    dash_detail["video"]["bit_rate"].reverse()
    after = source_selection(metadata_from_info(douyin.info_from_detail(dash_detail, IDENTIFIER)))
    assert before == after


def test_default_video_format_identity_excludes_signed_sibling_urls(dash_detail):
    before = douyin.info_from_detail(dash_detail, IDENTIFIER)
    dash_detail["video"]["bit_rate"][0]["play_addr"]["url_list"] = [
        "https://v.example.douyinvod.com/rotated.mp4?expires=123456"]
    after = douyin.info_from_detail(dash_detail, IDENTIFIER)
    assert [stream["format_id"] for stream in before["formats"]] == [
        stream["format_id"] for stream in after["formats"]]


@pytest.mark.parametrize("hint,codec", [("video_h264_720p_100000", "h264"), ("", "unknown")])
def test_browser_only_copies_video_with_explicit_avc_source_hint(dash_detail, hint, codec, tmp_path):
    stream = dash_detail["video"]["bit_rate"][0]
    stream.update(is_h265=0, is_bytevc1=0)
    stream["play_addr"]["url_key"] = hint
    video = metadata_from_info(douyin.info_from_detail(dash_detail, IDENTIFIER))
    streams = select_streams(video, "video_720_mp4")
    assert streams[0]["vcodec"] == codec
    command = ffmpeg_command([tmp_path / "video.mp4", tmp_path / "audio.m4a"], streams,
                             tmp_path / "output.mp4", "ffmpeg")
    assert command[command.index("-c:v") + 1] == ("copy" if codec == "h264" else "libx264")


def test_browser_source_is_used_by_registered_adapter(dash_detail, monkeypatch):
    extract = Mock(return_value=dash_detail)
    monkeypatch.setattr(browser, "browser_detail", extract)
    downloader = Mock()
    result = douyin.DouyinSourceBackend().extract(downloader, "https://www.douyin.com/video/" + IDENTIFIER)
    assert result["id"] == IDENTIFIER
    extract.assert_called_once_with(IDENTIFIER)
    downloader.urlopen.assert_not_called()
    downloader.extract_info.assert_not_called()


def test_browser_rejects_missing_runtime_without_unrestricted_fallback(monkeypatch):
    monkeypatch.setattr(settings, "DOUYIN_BROWSER_EXECUTABLE_PATH", "/does-not-exist/chrome")
    with pytest.raises(AppException) as error:
        browser.browser_detail(IDENTIFIER)
    assert error.value.code == "SOURCE_BROWSER_UNAVAILABLE"


def test_browser_driver_uses_private_tmp_after_mount(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", "/tmp/hidden-parent-directory")
    monkeypatch.setenv("HOME", "/tmp/hidden-parent-directory")
    monkeypatch.setattr(tempfile, "tempdir", "/tmp/hidden-parent-directory")
    mount = Mock()
    monkeypatch.setattr(browser.subprocess, "run", mount)
    monkeypatch.setattr(browser.socket, "create_connection", Mock(side_effect=OSError))
    monkeypatch.setattr(browser.threading, "Thread", Mock())
    server = Mock(server_address=("127.0.0.1", 12345))
    context = Mock()
    context.__enter__ = Mock(return_value=server)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(browser, "LocalServer", Mock(return_value=context))

    async def inspect_page(args, proxy):
        assert os.environ["TMPDIR"] == os.environ["HOME"] == "/tmp"
        assert tempfile.gettempdir() == "/tmp"
        return {"detail_found": True}

    monkeypatch.setattr(browser, "inspect_page", inspect_page)
    monkeypatch.setattr(browser, "Path", lambda _: tmp_path)
    browser.browser_child(SimpleNamespace(proxy_fd=123))
    assert (tmp_path / "browser-result.json").is_file()
    assert mount.call_args_list[0].args[0][-1] == "/tmp"
