"""Douyin source identity, bounded I/O and native frame regressions."""
from contextlib import nullcontext
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

import pytest
from pydantic import SecretStr
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers import dispatch, douyin, links, runtime, ytdlp
from app.modules.video.service import aggregate_download_options
from app.modules.download.presets import select_streams, source_selection
from app.modules.media.audio import select_audio_stream
from test_tasks import repository

IDENTIFIER = "7700513046734006379"
URL = "https://www.douyin.com/video/" + IDENTIFIER


@pytest.fixture
def detail():
    return {"aweme_id": IDENTIFIER, "aweme_type": 0, "desc": "公开竖屏视频", "duration": 12000,
            "status": {"is_private": False}, "author": {"nickname": "作者"},
            "video": {"duration": 12000, "width": 540, "height": 960,
                      "play_addr": {"url_list": ["https://v.example.douyinvod.com/video.mp4"], "data_size": 100000}}}


def share_page(detail):
    return '<script>window._ROUTER_DATA = ' + json.dumps({"loaderData": {
        "video_(id)/page": {"itemId": IDENTIFIER, "videoInfoRes": {"status_code": 0, "item_list": [detail]}}}}) + ';</script>'


class FixtureDownloader:
    def __init__(self, page, fallback=None):
        self.page = page.encode()
        self.params = {"retries": 1, "socket_timeout": 15}
        self.fallback = fallback
        self.requests = []
        self.extract_info = Mock(side_effect=self.extract)
        self.budgets = []

    def extract(self, url, download):
        assert url == URL and download is False
        assert self.params["retries"] == self.params["extractor_retries"] == 0
        if isinstance(self.fallback, Exception):
            raise self.fallback
        return self.fallback

    def urlopen(self, request):
        self.requests.append(request)
        return io.BytesIO(self.page)

    def source_budget(self, limit):
        self.budgets.append(limit)
        return nullcontext()


@pytest.mark.parametrize("value", [URL + "?utm_source=share&t=4", "https://www.douyin.com/jingxuan?modal_id=" + IDENTIFIER,
    "https://www.douyin.com/?modal_id=" + IDENTIFIER, "https://www.iesdouyin.com/share/video/" + IDENTIFIER + "/?from=share",
    "打开抖音看这个 https://www.douyin.com/jingxuan?modal_id=" + IDENTIFIER + " 。"])
def test_video_identity_normalization(value):
    assert links.canonical_link(value) == URL
    assert douyin.video_reference(value) == (URL, IDENTIFIER)


@pytest.mark.parametrize("value,code", [
    ("https://www.douyin.com/jingxuan?modal_id=1&modal_id=2", "INVALID_URL"),
    (URL + "?modal_id=123", "INVALID_URL"),
    ("https://www.douyin.com/jingxuan?modal_id=", "INVALID_URL"),
    ("https://www.douyin.com/jingxuan?modal_id=NaN", "INVALID_URL"),
    ("https://douyin.com.evil.example/video/123", "UNSUPPORTED_URL"),
    ("https://www.douyin.com/user/123?modal_id=" + IDENTIFIER, "UNSUPPORTED_URL"),
    ("https://www.douyin.com/note/" + IDENTIFIER, "UNSUPPORTED_URL"),
    ("https://live.douyin.com/123", "UNSUPPORTED_URL"),
])
def test_reject_ambiguous_and_non_video_links(value, code):
    with pytest.raises(AppException) as error:
        douyin.video_reference(value)
    assert error.value.code == code


def test_native_source_uses_one_bounded_page_and_no_sdk(detail, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    downloader = FixtureDownloader(share_page(detail))
    info = dispatch.extract_source(downloader, URL)
    assert info["id"] == IDENTIFIER and info["duration"] == 12
    assert info["extractor_key"] == "Douyin" and info["subtitles"] == {}
    assert downloader.budgets == [12] and len(downloader.requests) == 1
    assert downloader.requests[0].extensions["timeout"] == 5
    assert downloader.requests[0].url == "https://www.iesdouyin.com/share/video/" + IDENTIFIER + "/"
    downloader.extract_info.assert_not_called()
    assert info["formats"][0]["width"] == 540 and info["formats"][0]["height"] == 960
    assert "Cookie" not in info["formats"][0]["http_headers"]


def test_encoded_render_data(detail):
    page = '<script id="RENDER_DATA" type="application/json">' + quote(json.dumps({"item_list": [detail]})) + '</script>'
    assert douyin.share_detail(page, IDENTIFIER) == detail


@pytest.mark.parametrize("change,code", [
    ({"aweme_id": "123"}, "VIDEO_ID_CHANGED"), ({"aweme_type": 68}, "UNSUPPORTED_URL"),
    ({"is_preview": True}, "UNSUPPORTED_URL"), ({"is_pay": True}, "UNSUPPORTED_URL"),
    ({"status": {"is_private": True}}, "UNSUPPORTED_URL"),
    ({"status": {"private_status": 1}}, "UNSUPPORTED_URL"),
    ({"status": {"is_delete": True}}, "UNSUPPORTED_URL"), ({"duration": 60000}, "UNSUPPORTED_URL"),
])
def test_reject_identity_privacy_previews_and_truncation(detail, change, code):
    downloader = FixtureDownloader(share_page({**detail, **change}))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == code
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("url,code", [("https://127.0.0.1/a.mp4", "SSRF_BLOCKED"),
    ("https://douyinvod.com.evil.example/a.mp4", "UNSUPPORTED_URL"),
    ("https://user:secret@v.example.douyinvod.com/a.mp4", "INVALID_URL"), ("file:///tmp/private", "INVALID_URL")])
def test_media_url_rejects_unsafe_sources(detail, url, code):
    detail["video"]["play_addr"]["url_list"] = [url]
    downloader = FixtureDownloader(share_page(detail))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == code
    downloader.extract_info.assert_not_called()


def test_sdk_fallback_once_without_retries_and_restores_options(detail):
    downloader = FixtureDownloader("<html>layout changed</html>", douyin.info_from_detail(detail, IDENTIFIER))
    before = dict(downloader.params)
    assert douyin.DouyinSourceBackend().extract(downloader, URL)["id"] == IDENTIFIER
    downloader.extract_info.assert_called_once_with(URL, download=False)
    assert downloader.params == before and downloader.budgets == [12]


def test_no_fallback_after_explicit_http_restriction():
    downloader = FixtureDownloader("")
    downloader.urlopen = Mock(side_effect=HTTPError(Mock(status=429)))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == "SOURCE_RATE_LIMITED" and error.value.source_failure == "rate_limited"
    downloader.extract_info.assert_not_called()


def test_fresh_anonymous_cookie_requirement_is_sanitized_and_cools_down():
    downloader = FixtureDownloader("", DownloadError("Fresh cookies (not necessarily logged in) are needed secret=https://private"))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == "SOURCE_ACCESS_RESTRICTED" and error.value.source_failure == "restricted"
    assert "secret" not in error.value.message and downloader.params == {"retries": 1, "socket_timeout": 15}


def test_share_page_limit_prevents_sdk_fallback():
    downloader = FixtureDownloader("x" * (douyin.PAGE_LIMIT + 1))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == "SOURCE_RESPONSE_TOO_LARGE"
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("change,code", [({"id": "123"}, "VIDEO_ID_CHANGED"),
    ({"extractor_key": "Youtube"}, "VIDEO_ID_CHANGED"), ({"availability": "private"}, "UNSUPPORTED_URL"),
    ({"_type": "playlist"}, "UNSUPPORTED_URL"), ({"duration": float("nan")}, "UNSUPPORTED_URL")])
def test_sdk_fallback_revalidates_scope(detail, change, code):
    downloader = FixtureDownloader("", {**douyin.info_from_detail(detail, IDENTIFIER), **change})
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == code


def test_native_frames_without_inventing_720p(detail, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    video = ytdlp.metadata_from_info(douyin.info_from_detail(detail, IDENTIFIER))
    options = aggregate_download_options(video)
    assert [item.id for item in options] == ["video_source_mp4", "audio_mp3"]
    assert options[0].height == 960 and "540×960" in options[0].label
    assert select_streams(video, "video_source_mp4")[0]["height"] == 960
    with pytest.raises(AppException) as error:
        select_streams(video, "video_720_mp4")
    assert error.value.code == "PRESET_UNAVAILABLE"
    assert select_streams(video, "audio_mp3")[0]["height"] == 960
    with pytest.raises(AppException) as error:
        select_audio_stream(video)
    assert error.value.code == "AUDIO_STREAM_UNAVAILABLE"
    assert "url" not in source_selection(video)
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "legacy")
    assert "video_source_mp4" not in [item.id for item in aggregate_download_options(video)]


def test_domestic_source_ignores_environment_and_global_proxy(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YTDLP_PROXY", SecretStr("http://secret@127.0.0.1:7890"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert runtime.server_proxy(URL) is None
    options = ytdlp.extraction_options(None, url=URL)
    assert options["proxy"] == "" and options["js_runtimes"] == {}
    assert options["retries"] == options["extractor_retries"] == 0


async def test_source_selection_resolver_checks_identity(detail, monkeypatch):
    from app.modules.video import resolver as module
    video = ytdlp.metadata_from_info(douyin.info_from_detail(detail, IDENTIFIER))
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(module, "prepare_source_link", AsyncMock(return_value=URL))
    resolver = module.VideoResolver(SimpleNamespace(parse=AsyncMock(return_value=video)), None)
    payload = {"url": URL, "platform": "douyin", "platform_video_id": IDENTIFIER}
    assert (await resolver.download_selection(payload))["source_mode"] == "muxed_video"
    with pytest.raises(AppException) as error:
        await resolver.download_selection({**payload, "platform_video_id": "123"})
    assert error.value.code == "VIDEO_ID_CHANGED"


@pytest.mark.parametrize("mode", ["metadata", "download", "subtitle"])
def test_same_adapter_in_all_three_entry_points(detail, monkeypatch, tmp_path, mode):
    from app.modules.download import child
    from app.modules.transcript import subtitle_child
    from app.modules.video.parsers import ytdlp_worker
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    modules = {"metadata": ytdlp_worker, "download": child, "subtitle": subtitle_child}
    module = modules[mode]
    guard = Mock(violation=None)
    guard.installed.return_value = nullcontext()
    monkeypatch.setattr(module, "NetworkGuard", Mock(return_value=guard))
    downloader = FixtureDownloader(share_page(detail))
    monkeypatch.setattr(module, "guarded_downloader", lambda *args, **kwargs: nullcontext(downloader))
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    payload = {"url": URL, "platform": "douyin", "platform_video_id": IDENTIFIER, "preset_id": "audio_mp3"}
    events = []
    if mode == "metadata":
        assert module.extract_metadata(URL)["data"]["platform_video_id"] == IDENTIFIER
    elif mode == "download":
        def stop(event):
            assert event["_source_metadata_ready"]
            raise AppException("TEST_STOP", "Stop before media I/O")
        with pytest.raises(AppException, match="Stop before media"):
            module.download(payload, tmp_path, stop)
    else:
        module.fetch(payload, tmp_path, events.append)
        assert events[-1] == {"subtitle_found": False}
    assert len(downloader.requests) == 1
    downloader.extract_info.assert_not_called()


def test_changed_source_snapshot_is_rejected_before_transfer(detail, monkeypatch, tmp_path):
    from app.modules.download import child
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    guard = Mock(violation=None)
    guard.installed.return_value = nullcontext()
    monkeypatch.setattr(child, "NetworkGuard", Mock(return_value=guard))
    downloader = FixtureDownloader(share_page(detail))
    monkeypatch.setattr(child, "guarded_downloader", lambda *args, **kwargs: nullcontext(downloader))
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    video = ytdlp.metadata_from_info(douyin.info_from_detail(detail, IDENTIFIER))
    snapshot = {**source_selection(video), "width": 720}
    with pytest.raises(AppException) as error:
        child.download({"url": URL, "platform": "douyin", "platform_video_id": IDENTIFIER,
                        "preset_id": "video_source_mp4", "source_selection": snapshot}, tmp_path,
                       lambda _: pytest.fail("Source must fail before declaring metadata ready"))
    assert error.value.code == "SOURCE_SELECTION_CHANGED" and not list(tmp_path.iterdir())


async def test_native_source_quote_snapshot_is_visible_bound_and_idempotent(detail, monkeypatch, repository):
    from uuid import UUID
    from app.modules.auth.repository import UserRepository
    from app.modules.auth.dependencies import required_user
    from app.modules.billing.repository import AllowanceRepository
    from app.modules.billing.service import BillingService
    from app.modules.billing.router import get_billing_service
    from app.modules.tasks.service import TaskService
    from app.modules.video.repository import VideoRepository
    from app.modules.video.resolver import VideoResolver
    from app.modules.video import resolver as resolver_module
    from app.main import create_app
    from test_tasks import MemoryBus
    from httpx import AsyncClient, ASGITransport

    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "ALLOWANCES_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 64)
    monkeypatch.setattr(resolver_module, "prepare_source_link", AsyncMock(return_value=URL))
    user = UserRepository(repository.sessions).subscription("douyin-test-identity", True, login=True)
    video = ytdlp.metadata_from_info(douyin.info_from_detail(detail, IDENTIFIER))
    videos = VideoRepository(repository.sessions)
    from app.modules.video.parsers.operations import _metadata_deadline
    deadlines = []

    async def parsed(url):
        deadlines.append(_metadata_deadline.get())
        return video

    parser = SimpleNamespace(parse=AsyncMock(side_effect=parsed))
    dispatcher = Mock()
    tasks = TaskService(repository, MemoryBus(), dispatcher)
    service = BillingService(AllowanceRepository(repository.sessions), VideoResolver(parser, videos), videos, tasks)
    app = create_app()
    app.dependency_overrides[get_billing_service] = lambda: service
    app.dependency_overrides[required_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/v1/billing/quotes", json={"url": URL, "action": "download", "preset_id": "video_source_mp4"})
        assert response.status_code == 200
        quoted = response.json()["data"]
        selection = quoted["maximum"]["source_selection"]
        assert selection["source_mode"] == "muxed_video" and selection["width"] == 540 and selection["height"] == 960
        assert selection["max_source_bytes"] == 64 * 1024**2
        assert "url" not in selection
        first = await client.post("/api/v1/billing/tasks", json={"quote_id": quoted["quote_id"]})
        second = await client.post("/api/v1/billing/tasks", json={"quote_id": quoted["quote_id"]})
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
        task = repository.get(UUID(first.json()["data"]["task_id"]))
        assert task.input_payload["source_selection"] == source_selection(video)
    assert dispatcher.enqueue.call_args_list[0] == dispatcher.enqueue.call_args_list[1]
    assert len(deadlines) == 2 and deadlines[0] is not None and deadlines[0] == deadlines[1]


def test_bare_403_does_not_poison_shared_platform_cooldown():
    downloader = FixtureDownloader("")
    downloader.urlopen = Mock(side_effect=HTTPError(Mock(status=403)))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == "SOURCE_ACCESS_RESTRICTED"
    assert not getattr(error.value, "source_failure", None)
    downloader.extract_info.assert_not_called()


def test_protected_native_media_never_falls_back_to_sdk(detail):
    detail["video"]["has_drm"] = True
    downloader = FixtureDownloader(share_page(detail))
    with pytest.raises(AppException) as error:
        douyin.DouyinSourceBackend().extract(downloader, URL)
    assert error.value.code == "UNSUPPORTED_URL"
    downloader.extract_info.assert_not_called()


def test_frozen_source_byte_ceiling_survives_a_later_system_quota_increase(detail, monkeypatch, tmp_path):
    from app.modules.download import child
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 512)
    guard = Mock(violation=None)
    guard.installed.return_value = nullcontext()
    monkeypatch.setattr(child, "NetworkGuard", Mock(return_value=guard))
    downloader = FixtureDownloader(share_page(detail))
    monkeypatch.setattr(child, "guarded_downloader", lambda *args, **kwargs: nullcontext(downloader))
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    video = ytdlp.metadata_from_info(douyin.info_from_detail(detail, IDENTIFIER))
    payload = {"url": URL, "platform": "douyin", "platform_video_id": IDENTIFIER,
               "preset_id": "video_source_mp4", "source_selection": source_selection(video),
               "allowance": {"limits": {"video_seconds": 1200, "file_bytes": 256 * 1024**2},
                             "source_selection": {"max_source_bytes": 99999}}}
    with pytest.raises(AppException) as error:
        child.download(payload, tmp_path, lambda _: None)
    assert error.value.code == "FILE_TOO_LARGE" and not list(tmp_path.iterdir())
