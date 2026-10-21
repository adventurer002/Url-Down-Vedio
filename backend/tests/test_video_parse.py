import asyncio
import json
import os
import socket
import sys
from unittest.mock import AsyncMock, Mock
import urllib.request

from httpx import ASGITransport, AsyncClient
import pytest

from app.core import security
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.video import service as service_module
from app.modules.video.parsers import ytdlp
from app.modules.video.parsers.ytdlp import YtDlpParser, extraction_options, metadata_from_info
from app.modules.video.parsers.ytdlp_worker import NetworkGuard, extract_metadata, guarded_downloader
from app.modules.video.router import get_video_service
from app.modules.video.service import VideoService, aggregate_download_options


def dns_record(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443))


@pytest.fixture
def info():
    return {
        "id": "abc123", "extractor_key": "Youtube", "title": "固定测试视频", "duration": 100,
        "thumbnail": "https://example.com/cover.jpg", "uploader": "author",
        "subtitles": {"zh-Hans": [{"ext": "vtt"}], "en": [{"ext": "vtt"}], "empty": []},
        "automatic_captions": {"en": [{"ext": "vtt"}], "ja": [{"ext": "vtt"}]},
        "formats": [
            {"format_id": "v1080", "height": 1080, "vcodec": "avc1.640028", "acodec": "none", "filesize": 10_000_000},
            {"format_id": "v480", "height": 480, "vcodec": "avc1.64001f", "acodec": "none", "filesize_approx": 3_000_000},
            {"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "filesize": 1_000_000},
        ],
    }


@pytest.mark.parametrize("url,code", [
    ("http://127.0.0.1/a", "SSRF_BLOCKED"), ("http://127.42.1.2", "SSRF_BLOCKED"),
    ("http://169.254.169.254/latest/meta-data", "SSRF_BLOCKED"),
    ("http://192.168.1.1", "SSRF_BLOCKED"), ("http://10.0.0.1", "SSRF_BLOCKED"),
    ("http://172.16.0.1", "SSRF_BLOCKED"), ("http://LOCALHOST./", "SSRF_BLOCKED"),
    ("http://a.localhost", "SSRF_BLOCKED"), ("http://[::1]/", "SSRF_BLOCKED"),
    ("http://[fe80::1]/", "SSRF_BLOCKED"), ("http://[fc00::1]/", "SSRF_BLOCKED"),
    ("http://[::ffff:127.0.0.1]/", "SSRF_BLOCKED"), ("http://224.0.0.1", "SSRF_BLOCKED"),
    ("http://240.0.0.1", "SSRF_BLOCKED"), ("http://0.0.0.0", "SSRF_BLOCKED"),
    ("ftp://example.com/file", "INVALID_URL"), ("file:///etc/passwd", "INVALID_URL"),
    ("https://user:password@example.com", "INVALID_URL"), ("https://", "INVALID_URL"),
    ("http://example.com:99999", "INVALID_URL"), ("http://example.com\\@localhost", "INVALID_URL"),
    ("http://example.com\n", "INVALID_URL"), ("http://[fe80::1%25en0]", "INVALID_URL"),
])
async def test_reject_unsafe_urls(url, code):
    with pytest.raises(AppException) as error:
        await security.validate_url(url)
    assert error.value.code == code


@pytest.mark.parametrize("addresses", [["192.168.1.2"], ["93.184.216.34", "::1"], ["127.0.0.1"], ["169.254.1.1"]])
async def test_reject_private_dns(monkeypatch, addresses):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[dns_record(ip) for ip in addresses]))
    with pytest.raises(AppException, match="非公开") as error:
        await security.validate_url("https://public-looking.example/video")
    assert error.value.code == "SSRF_BLOCKED"


async def test_public_ipv4_ipv6_and_tracking(monkeypatch):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[dns_record("93.184.216.34"), dns_record("2606:4700:4700::1111")]))
    assert await security.validate_url("https://Example.com/watch?v=abc&utm_source=test&si=track#part") == "https://example.com/watch?v=abc"


@pytest.mark.parametrize("kind,code", [("timeout", "DNS_TIMEOUT"), ("failure", "INVALID_URL"), ("empty", "INVALID_URL")])
async def test_dns_failure(monkeypatch, kind, code):
    async def resolve(*args, **kwargs):
        if kind == "timeout":
            await asyncio.sleep(10)
        if kind == "failure":
            raise socket.gaierror("sensitive diagnostic")
        return []
    monkeypatch.setattr(security, "DNS_TIMEOUT", 0.01)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(AppException) as error:
        await security.validate_url("https://example.com/video")
    assert error.value.code == code


def test_metadata_mapping(info):
    video = metadata_from_info(info)
    assert (video.platform, video.platform_video_id) == ("youtube", "abc123")
    assert video.title == "固定测试视频" and video.duration == 100
    assert video.uploader == "author"
    assert video.subtitle_languages == ["en", "ja", "zh-Hans"]
    assert video.formats == info["formats"]


def test_aggregate_separate_streams_and_missing_tier(info):
    options = aggregate_download_options(metadata_from_info(info))
    assert [option.id for option in options] == ["video_1080_mp4", "video_480_mp4", "audio_mp3"]
    assert [option.estimated_size_bytes for option in options] == [11_000_000, 4_000_000, 2_400_000]


def test_muxed_unknown_size_and_drm_filter(info):
    info["formats"] = [
        {"height": 720, "vcodec": "avc1", "acodec": "mp4a", "tbr": 1000},
        {"height": 1080, "vcodec": "avc1", "acodec": "mp4a", "has_drm": True},
    ]
    options = aggregate_download_options(metadata_from_info(info))
    assert [option.id for option in options] == ["video_720_mp4", "audio_mp3"]
    assert options[0].estimated_size_bytes == 12_500_000
    info["formats"][0].pop("tbr")
    assert aggregate_download_options(metadata_from_info(info))[0].estimated_size_bytes is None


def test_no_audio_cannot_form_av_tier(info):
    info["formats"] = info["formats"][:2]
    assert aggregate_download_options(metadata_from_info(info)) == []


def test_audio_only_and_transcode_estimate(info):
    info["formats"] = [info["formats"][-1]]
    assert [option.id for option in aggregate_download_options(metadata_from_info(info))] == ["audio_mp3"]
    info["formats"].append({"height": 1080, "vcodec": "vp9", "acodec": "none", "filesize": 1000})
    assert aggregate_download_options(metadata_from_info(info))[0].estimated_size_bytes is None


@pytest.mark.parametrize("change", [{"_type": "playlist"}, {"id": None}, {"is_live": True}, {"availability": "private"}])
def test_reject_nonpublic_and_playlist(info, change):
    with pytest.raises(AppException) as error:
        metadata_from_info({**info, **change})
    assert error.value.code == "UNSUPPORTED_URL"


def test_network_guard_rechecks_dns_at_connect(monkeypatch):
    resolver = Mock(side_effect=[[dns_record("93.184.216.34")], [dns_record("127.0.0.1")]])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    guard = NetworkGuard()
    guard.check_url("https://example.com/video")
    with pytest.raises(AppException) as error:
        guard.getaddrinfo("example.com", 443)
    assert error.value.code == "SSRF_BLOCKED"


def test_redirect_guard_precedes_http_connection(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[dns_record("93.184.216.34")]))
    guard = NetworkGuard()
    with guarded_downloader(guard, extraction_options(None)) as downloader:
        handler = next(iter(downloader._request_director.handlers.values()))
        opener = handler._create_instance(proxies={}, cookiejar=downloader.cookiejar)
        # A redirect re-enters OpenerDirector.open and runs request processors.
        processors = opener.process_request["http"]
        request = urllib.request.Request("http://169.254.169.254/latest/meta-data")
        with pytest.raises(AppException) as error:
            for processor in processors:
                request = processor.http_request(request)
        assert error.value.code == "SSRF_BLOCKED"


@pytest.mark.parametrize("target,code", [
    ("http://127.0.0.1/admin", "SSRF_BLOCKED"),
    ("ftp://example.com/file", "INVALID_URL"),
    ("file:///etc/passwd", "INVALID_URL"),
])
def test_redirect_rejects_unsafe_targets(monkeypatch, target, code):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[dns_record("93.184.216.34")]))
    with guarded_downloader(NetworkGuard(), extraction_options(None)) as downloader:
        handler = next(iter(downloader._request_director.handlers.values()))
        opener = handler._create_instance(proxies={}, cookiejar=downloader.cookiejar)
        redirect = next(item for item in opener.handlers if isinstance(item, urllib.request.HTTPRedirectHandler))
        with pytest.raises(AppException) as error:
            redirect.redirect_request(urllib.request.Request("https://example.com/video"), None, 302, "Found", {}, target)
        assert error.value.code == code


def test_real_downloader_never_processes_or_writes_media(monkeypatch, tmp_path, info):
    from yt_dlp.extractor.common import InfoExtractor

    class FixtureIE(InfoExtractor):
        _VALID_URL = r"https://fixture\.test/watch/(?P<id>[^/]+)"

        def _real_extract(self, url):
            return dict(info)

    monkeypatch.chdir(tmp_path)
    options = extraction_options(None)
    options["allowed_extractors"] = []
    with guarded_downloader(NetworkGuard(), options) as downloader:
        downloader.add_info_extractor(FixtureIE())
        downloader.process_info = Mock(side_effect=AssertionError("must never download"))
        result = downloader.extract_info("https://fixture.test/watch/abc123", download=False)
        assert result["id"] == "abc123"
        downloader.process_info.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_trusted_proxy_does_not_authorize_private_target(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[dns_record("127.0.0.1")]))
    guard = NetworkGuard("http://127.0.0.1:7890")
    assert guard.getaddrinfo("127.0.0.1", 7890)
    with pytest.raises(AppException):
        guard.check_url("http://127.0.0.1:7890")


def test_worker_metadata_only_options_and_conversion(monkeypatch, info):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[dns_record("93.184.216.34")]))
    downloader = Mock()
    downloader.__enter__ = Mock(return_value=downloader)
    downloader.__exit__ = Mock(return_value=False)
    downloader.extract_info.return_value = info
    factory = Mock(return_value=downloader)
    from app.modules.video.parsers import ytdlp_worker
    monkeypatch.setattr(ytdlp_worker, "guarded_downloader", factory)
    result = extract_metadata("https://example.com/video", "http://127.0.0.1:7890")
    assert result["data"]["platform"] == "youtube"
    options = factory.call_args.args[1]
    assert options["skip_download"] and options["simulate"] and options["noplaylist"]
    assert options["socket_timeout"] == 15 and options["retries"] == 1
    assert options["proxy"] == "http://127.0.0.1:7890" and options["cookiefile"] is None
    downloader.extract_info.assert_called_once_with("https://example.com/video", download=False)


@pytest.mark.parametrize("failure,code", [(TimeoutError("secret"), "PARSE_TIMEOUT"), (RuntimeError("Unsupported URL: secret"), "UNSUPPORTED_URL"), (RuntimeError("secret"), "PARSE_FAILED")])
def test_sdk_error_mapping(failure, code):
    error = ytdlp.map_extraction_error(failure)
    assert error.code == code and "secret" not in error.message


async def test_adapter_child_protocol(monkeypatch, info):
    payload = {"data": json.loads(json.dumps(metadata_from_info(info).__dict__))}
    process = Mock(pid=12345, returncode=0)
    process.stdout.read = AsyncMock(side_effect=[json.dumps(payload).encode(), b""])
    process.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cleanup = AsyncMock()
    monkeypatch.setattr(ytdlp, "_terminate_process_group", cleanup)
    video = await YtDlpParser().parse("https://example.com/video")
    assert video.platform_video_id == "abc123"
    assert spawn.call_args.kwargs["start_new_session"] is True
    cleanup.assert_awaited_once_with(process)


@pytest.mark.parametrize("code,status", [("PARSE_TIMEOUT", 504), ("UNSUPPORTED_URL", 422), ("SSRF_BLOCKED", 400)])
async def test_adapter_child_errors(monkeypatch, code, status):
    process = Mock(returncode=0)
    process.stdout.read = AsyncMock(side_effect=[json.dumps({"error": code}).encode(), b""])
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(ytdlp, "_terminate_process_group", AsyncMock())
    with pytest.raises(AppException) as error:
        await YtDlpParser().parse("https://example.com/video")
    assert (error.value.code, error.value.status_code) == (code, status)


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_total_timeout_terminates_real_subprocess(monkeypatch, ignore_term):
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        script = "import signal, time; "
        if ignore_term:
            script += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        process = await original_spawn(sys.executable, "-c", script + "time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AppException) as error:
        await YtDlpParser(timeout=0.15).parse("https://example.com/video")
    assert error.value.code == "PARSE_TIMEOUT"
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(processes[0].pid, 0)


async def test_output_overflow_terminates_process_without_pipe_deadlock(monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original_spawn(
            sys.executable, "-c", "import os; block=b'x'*65536\nwhile True: os.write(1, block)", **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ytdlp, "MAX_OUTPUT_BYTES", 65536)
    with pytest.raises(AppException) as error:
        await asyncio.wait_for(YtDlpParser().parse("https://example.com/video"), timeout=3)
    assert error.value.code == "PARSE_FAILED"
    assert processes[0].returncode is not None


async def test_request_cancellation_cleans_up_child(monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    spawned = asyncio.Event()

    async def spawn(*args, **kwargs):
        process = await original_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(YtDlpParser().parse("https://example.com/video"))
    await asyncio.wait_for(spawned.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_api_contract_and_no_raw_formats(monkeypatch, info):
    app = create_app()
    parser = Mock(parse=AsyncMock(return_value=metadata_from_info(info)))
    app.dependency_overrides[get_video_service] = lambda: VideoService(parser)
    monkeypatch.setattr(service_module, "validate_url", AsyncMock(return_value="https://example.com/video"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/videos/parse", json={"url": "https://example.com/video?utm_source=test"})
        assert response.status_code == 200
        data = response.json()["data"]
        assert set(data) == {"platform", "platform_video_id", "title", "duration", "thumbnail", "uploader", "has_subtitles", "subtitle_languages", "download_options"}
        assert data["has_subtitles"] is True and "formats" not in data
        parser.parse.assert_awaited_once_with("https://example.com/video")
        invalid = await client.post("/api/v1/videos/parse", json={"url": "https://example.com", "proxy": "http://localhost", "cookies": "secret"})
        assert invalid.status_code == 422


async def test_api_ssrf_prevents_parser_call():
    app = create_app()
    parser = Mock(parse=AsyncMock())
    app.dependency_overrides[get_video_service] = lambda: VideoService(parser)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/videos/parse", json={"url": "http://127.0.0.1/private"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SSRF_BLOCKED"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    parser.parse.assert_not_awaited()
