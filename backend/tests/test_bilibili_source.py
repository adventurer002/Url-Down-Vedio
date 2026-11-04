from contextlib import contextmanager, nullcontext
from copy import deepcopy
from http.cookiejar import CookieJar
import io
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video.parsers import bilibili, dispatch, ytdlp_worker
from app.modules.video.parsers.ytdlp import metadata_from_info
from app.modules.video.service import aggregate_download_options
from app.modules.download.presets import select_streams
from app.modules.transcript.subtitle_parser import parse_subtitle

URL = "https://www.bilibili.com/video/BV1g2JX6nESP"
IMG = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
SUB = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
VIEW = {"bvid": "BV1g2JX6nESP", "aid": 123, "title": "public fixture", "state": 0,
        "rights": {}, "owner": {"name": "author"}, "duration": 13,
        "pages": [{"cid": 10, "page": 1, "part": "first", "duration": 10},
                  {"cid": 20, "page": 2, "part": "second", "duration": 3}]}


def play(duration=10):
    return {"timelength": duration * 1000, "dash": {
        "audio": [{"id": 30216, "baseUrl": "https://cdn.bilivideo.com/audio.m4s?token=private",
                   "codecs": "mp4a.40.2", "mimeType": "audio/mp4", "bandwidth": 64000}],
        "video": [{"id": 64, "base_url": "https://cdn.bilivideo.com/video.m4s",
                   "codecs": "avc1.640028", "mime_type": "video/mp4", "bandwidth": 1200000,
                   "height": 720, "width": 1280}]}}


def test_format_and_frozen_selection_survive_upstream_reordering():
    from app.modules.download.presets import source_selection
    value = play()
    value["dash"]["video"][0].update(width=854, height=356)
    value["dash"]["audio"].append({**value["dash"]["audio"][0], "id": 30280, "bandwidth": 192000})
    first = metadata_from_info({"id": "fixture", "extractor_key": "BiliBili", "title": "fixture",
                                "duration": 10, "formats": bilibili.formats_from_play(value, 10)})
    value["dash"]["audio"].reverse()
    value["dash"]["audio"][0]["baseUrl"] = "https://other.bilivideo.com/audio.m4s?new-signature=private"
    second = metadata_from_info({"id": "fixture", "extractor_key": "BiliBili", "title": "fixture",
                                 "duration": 10, "formats": bilibili.formats_from_play(value, 10)})
    assert source_selection(first) == source_selection(second)
    assert source_selection(second)["audio_format_id"] == "audio-30280-mp4a.40.2"


class FixtureDownloader:
    def __init__(self):
        self.cookiejar = CookieJar()
        self.calls = []
        self.params = {"socket_timeout": 15, "retries": 1, "extractor_retries": 1}
        self.extract_info = Mock()
        self.responses = {
            "/x/web-interface/view": {"code": 0, "data": deepcopy(VIEW)},
            "/x/frontend/finger/spi": {"code": 0, "data": {"b_3": "anonymous-3", "b_4": "anonymous-4"}},
            "/x/web-interface/nav": {"code": -101, "data": {"wbi_img": {"img_url": IMG, "sub_url": SUB}}},
            "/x/player/wbi/playurl": {"code": 0, "data": play()},
            "/x/player/wbi/v2": {"code": 0, "data": {"cid": 10, "subtitle": {"subtitles": [
                {"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/caption.json"}]}}},
            "/caption.json": {"body": [{"from": 0, "to": 1.2, "content": "字幕测试"}]},
        }

    @contextmanager
    def source_budget(self, limit):
        self.limit = len(self.calls) + limit
        yield
        assert len(self.calls) <= self.limit

    def urlopen(self, request):
        self.calls.append(request)
        response = self.responses[urlsplit(request.url).path]
        if isinstance(response, Exception):
            raise response
        data = response if isinstance(response, bytes) else json.dumps(response).encode()
        return Response(io.BytesIO(data), request.url, {})


@pytest.fixture(autouse=True)
def native_settings(isolated_product_settings, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")


def test_wbi_signature_known_vector_and_filtering():
    result = bilibili.sign_wbi({"foo": "114", "bar": "514", "baz": "1919810"}, IMG, SUB, 1702204169)
    assert result["w_rid"] == "6149fdadf571698ca7e6a567265cd0ee"
    assert bilibili.sign_wbi({"foo": "a!'()*b"}, IMG, SUB, 1702204169)["foo"] == "ab"
    with pytest.raises(bilibili.SourceSchemaChanged):
        bilibili.sign_wbi({}, "invalid", SUB)


@pytest.mark.parametrize("suffix,identity,cid,duration", [
    ("", "BV1g2JX6nESP_p1", 10, 10), ("?p=1", "BV1g2JX6nESP_p1", 10, 10),
    ("?p=2", "BV1g2JX6nESP_p2", 20, 3),
])
def test_native_metadata_streams_subtitles_and_part_identity(suffix, identity, cid, duration):
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/playurl"]["data"] = play(duration)
    downloader.responses["/x/player/wbi/v2"]["data"]["cid"] = cid
    info = dispatch.extract_source(downloader, URL + suffix)
    video = metadata_from_info(info)
    assert video.platform_video_id == identity and video.duration == duration
    assert video.subtitle_languages == ["zh-CN"]
    assert {option.id for option in aggregate_download_options(video)} == {"video_720_mp4", "audio_mp3"}
    assert len(select_streams(video, "video_720_mp4")) == 2
    assert select_streams(video, "audio_mp3")[0]["abr"] == 64
    assert len(downloader.calls) == 5  # Metadata never fetches media or caption bodies.
    for request in downloader.calls[-2:]:
        params = parse_qs(urlsplit(request.url).query)
        assert params["cid"] == [str(cid)] and params["w_rid"] and params["wts"]
    cookies = list(downloader.cookiejar)
    assert {item.name for item in cookies} == {"buvid3", "buvid4"}
    assert all(item.domain == "api.bilibili.com" and item.secure for item in cookies)
    assert "Cookie" not in info["http_headers"]
    assert all("Cookie" not in stream["http_headers"] for stream in info["formats"])
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_single_part_and_av_alias_match_locked_extractor_identity(explicit):
    downloader = FixtureDownloader()
    downloader.responses["/x/web-interface/view"]["data"]["pages"] = [deepcopy(VIEW["pages"][0])]
    info = dispatch.extract_source(downloader, "https://www.bilibili.com/video/av123" + ("?p=1" if explicit else ""))
    assert info["id"] == "BV1g2JX6nESP" + ("_p1" if explicit else "")


@pytest.mark.parametrize("url", [URL + "?p=0", URL + "?p=2&p=1", URL + "?p=", URL + "?p=-1",
    "https://www.bilibili.com/bangumi/play/ep1", "https://live.bilibili.com/123"])
def test_bad_or_out_of_scope_links_never_request_upstream(url):
    downloader = FixtureDownloader()
    with pytest.raises(AppException):
        dispatch.extract_source(downloader, url)
    assert downloader.calls == []
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("code,failure", [(-352, "restricted"), (-412, "restricted"), (-799, "rate_limited"), (-509, "rate_limited")])
def test_api_risk_errors_never_trigger_fallback(code, failure):
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/playurl"] = {"code": code, "message": "private upstream detail"}
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, URL)
    assert error.value.source_failure == failure and "private" not in error.value.message
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("status", [403, 412, 429, 500])
def test_http_failure_sanitized_and_no_fallback(status):
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/playurl"] = HTTPError(Response(io.BytesIO(b"secret"), "https://api.bilibili.com/", {}, status=status))
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, URL)
    assert getattr(error.value, "source_failure", None) == ({412: "restricted", 429: "rate_limited"}.get(status))
    assert "secret" not in error.value.message
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("change", ["paid", "interactive", "missing_part", "wrong_id", "preview", "truncated"])
def test_unsupported_or_changed_sources_fail_closed(change):
    downloader = FixtureDownloader()
    view = downloader.responses["/x/web-interface/view"]["data"]
    if change == "paid": view["is_upower_exclusive"] = True
    if change == "interactive": view["rights"]["is_stein_gate"] = 1
    if change == "wrong_id": view["bvid"] = "BV1111111111"
    if change == "preview": downloader.responses["/x/player/wbi/playurl"]["data"]["is_preview"] = 1
    if change == "truncated": downloader.responses["/x/player/wbi/playurl"]["data"]["timelength"] = 1000
    with pytest.raises(AppException):
        dispatch.extract_source(downloader, URL + ("?p=8" if change == "missing_part" else ""))
    downloader.extract_info.assert_not_called()


def test_fallback_once_only_after_validating_public_identity_and_duration():
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/playurl"]["data"] = {"timelength": 10000, "dash": {}}
    expected = {"extractor_key": "BiliBili", "id": "BV1g2JX6nESP_p1", "duration": 10}
    original = dict(downloader.params)
    def compatibility(*args, **kwargs):
        assert downloader.params["retries"] == 0 and downloader.params["socket_timeout"] == 5
        return expected
    downloader.extract_info.side_effect = compatibility
    assert dispatch.extract_source(downloader, URL) == expected
    downloader.extract_info.assert_called_once_with(URL, download=False)
    assert downloader.params == original
    downloader.extract_info.side_effect = None
    downloader.extract_info.return_value = {**expected, "id": "wrong"}
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, URL)
    assert error.value.code == "VIDEO_ID_CHANGED"


def test_invalid_view_schema_cannot_fall_back_without_public_identity_proof():
    downloader = FixtureDownloader()
    downloader.responses["/x/web-interface/view"] = {"code": 0, "data": {}}
    with pytest.raises(bilibili.SourceSchemaChanged):
        dispatch.extract_source(downloader, URL)
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "http://169.254.169.254/", "https://bilivideo.com.evil.example/a"])
def test_untrusted_media_descriptors_never_trigger_compatibility(url):
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/playurl"]["data"]["dash"]["audio"][0]["baseUrl"] = url
    with pytest.raises(AppException):
        dispatch.extract_source(downloader, URL)
    downloader.extract_info.assert_not_called()


def test_caption_is_fetched_on_demand_and_uses_existing_transcript_contract():
    downloader = FixtureDownloader()
    track = dispatch.extract_source(downloader, URL)["subtitles"]["zh-CN"][0]
    assert len(downloader.calls) == 5
    prepared = dispatch.prepare_source_subtitle(downloader, URL, track)
    assert len(downloader.calls) == 6 and prepared["ext"] == "json3" and "url" not in prepared
    transcript = parse_subtitle(prepared["data"], "json3", "zh-CN")
    assert transcript.content_text == "字幕测试"
    assert transcript.segments == [{"start": 0, "end": 1.2, "text": "字幕测试"}]


def test_response_size_and_total_budget_have_no_retry():
    downloader = FixtureDownloader()
    downloader.responses["/x/web-interface/view"] = b"x" * (2 * 1024 * 1024 + 1)
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, URL)
    assert error.value.code == "SOURCE_RESPONSE_TOO_LARGE" and len(downloader.calls) == 2
    client = bilibili.BilibiliClient(downloader, timeout=-1)
    with pytest.raises(AppException) as error:
        client.api("/x/web-interface/view")
    assert error.value.code == "PARSE_TIMEOUT" and len(downloader.calls) == 2


def test_guard_request_budget_counts_actual_requests_and_restores_after_extraction():
    guard = ytdlp_worker.NetworkGuard()
    with guard.request_budget(2):
        guard.before_request()
        guard.before_request()
        with pytest.raises(AppException): guard.before_request()
    guard.before_request()  # Media transfer does not inherit the metadata limit.


def test_guard_deadline_does_not_leak_into_long_media_transfer():
    guard = ytdlp_worker.NetworkGuard()
    with guard.request_budget(16):
        guard.request_deadline = 0
        with pytest.raises(AppException) as error:
            guard.before_request()
        assert error.value.code == "PARSE_TIMEOUT"
    assert guard.request_deadline is None
    guard.before_request()


def test_ambiguous_pages_are_rejected_before_fallback():
    downloader = FixtureDownloader()
    downloader.responses["/x/web-interface/view"]["data"]["pages"][1]["page"] = 1
    with pytest.raises(bilibili.SourceSchemaChanged):
        dispatch.extract_source(downloader, URL + "?p=1")
    downloader.extract_info.assert_not_called()


def test_login_only_subtitles_do_not_advertise_caption_capability():
    downloader = FixtureDownloader()
    downloader.responses["/x/player/wbi/v2"]["data"]["need_login_subtitle"] = True
    assert metadata_from_info(dispatch.extract_source(downloader, URL)).subtitle_languages == []


def test_native_caption_child_writes_selected_track_without_downloading_media(monkeypatch, tmp_path):
    from app.modules.transcript import subtitle_child
    fixture = FixtureDownloader()
    original = subtitle_child.guarded_downloader

    class FixtureGuard:
        violation = None
        def __init__(self, proxy=None):
            assert proxy is None
        def installed(self): return nullcontext()
        def check_url(self, url): assert url.startswith(URL)

    def factory(guard, options, *, metadata_only=True):
        if metadata_only:
            return nullcontext(fixture)
        assert options["skip_download"] and options["writesubtitles"]
        downloader = original(guard, options, metadata_only=False)
        downloader.urlopen = fixture.urlopen
        return downloader

    monkeypatch.setattr(subtitle_child, "NetworkGuard", FixtureGuard)
    monkeypatch.setattr(subtitle_child, "guarded_downloader", factory)
    events = []
    subtitle_child.fetch({"url": URL, "platform": "bilibili", "platform_video_id": "BV1g2JX6nESP_p1",
                          "language": "zh-CN"}, tmp_path, events.append)
    assert events[0] == {"_source_metadata_ready": True}
    result = events[-1]
    transcript = parse_subtitle((tmp_path / result["filename"]).read_text(), "json3", result["language"])
    assert transcript.content_text == "字幕测试"
    assert len(fixture.calls) == 6
    assert all("bilivideo" not in request.url for request in fixture.calls)


def test_native_metadata_and_download_children_share_identity_and_risk_evidence(monkeypatch, tmp_path):
    from app.modules.download import child

    class FixtureGuard:
        violation = None
        def __init__(self, proxy=None): assert proxy is None
        def installed(self): return nullcontext()
        def check_url(self, url): assert url.startswith(URL)

    fixture = FixtureDownloader()
    for module in (ytdlp_worker, child):
        monkeypatch.setattr(module, "NetworkGuard", FixtureGuard)
        monkeypatch.setattr(module, "guarded_downloader", lambda *args, **kwargs: nullcontext(fixture))
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    result = ytdlp_worker.extract_metadata(URL)
    assert result["data"]["platform_video_id"] == "BV1g2JX6nESP_p1"
    def stop(event):
        assert event == {"_source_metadata_ready": True}
        raise AppException("TEST_STOP", "fixture stop")
    with pytest.raises(AppException) as error:
        child.download({"url": URL, "platform": "bilibili", "platform_video_id": "BV1g2JX6nESP_p1"}, tmp_path, stop)
    assert error.value.code == "TEST_STOP"
    fixture.responses["/x/web-interface/view"] = {"code": -352}
    result = ytdlp_worker.extract_metadata(URL)
    assert result == {"error": "SOURCE_ACCESS_RESTRICTED", "source_failure": "restricted", "source_stage": "view"}


@pytest.mark.parametrize("value", ["encoded%2Fidentifier%3D%3D", "value+with/slash=", None])
def test_anonymous_identifiers_allow_cookie_octets_and_optional_buvid4(value):
    downloader = FixtureDownloader()
    downloader.responses["/x/frontend/finger/spi"]["data"]["b_4"] = value
    assert dispatch.extract_source(downloader, URL)["id"] == "BV1g2JX6nESP_p1"


@pytest.mark.parametrize("value", ["injected; cookie=value", "line\r\nbreak", "quote\"value", "bad\\value"])
def test_anonymous_cookie_injection_rejected_without_fallback(value):
    downloader = FixtureDownloader()
    downloader.responses["/x/frontend/finger/spi"]["data"]["b_3"] = value
    with pytest.raises(bilibili.SourceSchemaChanged):
        dispatch.extract_source(downloader, URL)
    downloader.extract_info.assert_not_called()


@pytest.mark.parametrize("field", ["backupUrl", "backup_url"])
def test_nonstandard_primary_cdn_uses_validated_standard_backup(field):
    downloader = FixtureDownloader()
    audio = downloader.responses["/x/player/wbi/playurl"]["data"]["dash"]["audio"][0]
    audio["baseUrl"] = "https://pcdn.bilivideo.com:4483/audio.m4s"
    audio[field] = ["http://127.0.0.1/private", "https://cdn.bilivideo.com/audio.m4s?token=retained"]
    info = dispatch.extract_source(downloader, URL)
    assert info["formats"][0]["url"] == "https://cdn.bilivideo.com/audio.m4s?token=retained"
    downloader.extract_info.assert_not_called()
