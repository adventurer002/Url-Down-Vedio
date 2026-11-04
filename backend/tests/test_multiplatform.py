import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import httpx
from pydantic import SecretStr
import pytest

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.video import preflight
from app.modules.video.parsers import dispatch, links, runtime, ytdlp, ytdlp_worker


@pytest.mark.parametrize("url,platform", [
    ("https://www.bilibili.com/video/BVabc?p=2", "bilibili"),
    ("https://b23.tv/abc", "bilibili"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
    ("https://v.douyin.com/abc/", "douyin"),
    ("https://www.iesdouyin.com/share/video/123", "douyin"),
    ("https://bilibili.com.evil.example/video/1", "other"),
    ("https://notyoutube.com/watch?v=x", "other"),
    ("https://douyin.com.evil.example/", "other"),
])
def test_platform_domain_boundaries(url, platform):
    assert links.platform_for(url) == platform


@pytest.mark.parametrize("value", [
    "https://youtu.be/dQw4w9WgXcQ?t=3",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ?si=tracking",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=share",
])
def test_youtube_aliases_normalize_without_network(value):
    assert links.canonical_link(value) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_share_text_and_part_identity():
    assert links.canonical_link("复制打开看看 https://v.douyin.com/abcdef/ 。") == "https://v.douyin.com/abcdef/"
    assert links.canonical_link("分享 https://www.bilibili.com/video/BVabc?p=2，") == "https://www.bilibili.com/video/BVabc?p=2"
    assert links.canonical_link("https://www.bilibili.com/video/BVabc?p=2，") == "https://www.bilibili.com/video/BVabc?p=2"
    assert links.canonical_link("https://www.bilibili.com/video/BVabc?p=1") != links.canonical_link("https://www.bilibili.com/video/BVabc?p=2")


@pytest.mark.parametrize("value,code", [
    ("https://a.example/a https://b.example/b", "INVALID_URL"),
    ("没有链接", "INVALID_URL"),
    ("https://user:password@example.com/", "INVALID_URL"),
    ("分享 http://169.254.169.254/latest", "SSRF_BLOCKED"),
    ("https://youtu.be/invalid", "INVALID_URL"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=aaaaaaaaaaa", "INVALID_URL"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=", "INVALID_URL"),
])
def test_ambiguous_or_unsafe_link_rejected(value, code):
    with pytest.raises(AppException) as error:
        links.canonical_link(value)
    assert error.value.code == code


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("short-link resolution must not download response bodies")
        yield b""

    async def aclose(self):
        pass


async def test_short_link_checks_every_hop_and_never_reads_body(monkeypatch):
    calls = []
    validate = AsyncMock(side_effect=lambda url: url)
    monkeypatch.setattr(links, "validate_url", validate)

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "b23.tv":
            return httpx.Response(302, headers={"location": "https://www.bilibili.com/video/BVabc?p=2"})
        return httpx.Response(200, stream=UnreadableBody())

    result = await links.resolve_short_link("https://b23.tv/abc", transport=httpx.MockTransport(respond))
    assert result == "https://www.bilibili.com/video/BVabc?p=2"
    assert [call.args[0] for call in validate.await_args_list] == calls


@pytest.mark.parametrize("target,code", [
    ("http://127.0.0.1/private", "SSRF_BLOCKED"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "UNSUPPORTED_URL"),
    ("https://www.bilibili.com.evil.example/", "UNSUPPORTED_URL"),
])
async def test_short_redirect_cannot_change_network_policy_or_access_private_ip(monkeypatch, target, code):
    async def validate(url):
        # Run the actual IP/URL validator before the mocked public DNS stage.
        from app.core.security import normalize_url
        normalize_url(url)
        return url

    monkeypatch.setattr(links, "validate_url", validate)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    with pytest.raises(AppException) as error:
        await links.resolve_short_link("https://b23.tv/abc", transport=httpx.MockTransport(respond))
    assert error.value.code == code and len(requests) == 1


async def test_short_redirect_budget_and_cancellation(monkeypatch):
    monkeypatch.setattr(links, "validate_url", AsyncMock(side_effect=lambda url: url))
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/next"})

    with pytest.raises(AppException):
        await links.resolve_short_link("https://b23.tv/abc", transport=httpx.MockTransport(redirect))
    assert len(requests) == 6
    closed = asyncio.Event()

    async def stuck(request):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    cancelled = AsyncMock(side_effect=[False, True])
    with pytest.raises(AppException) as error:
        await links.resolve_short_link("https://b23.tv/abc", transport=httpx.MockTransport(stuck), cancelled=cancelled)
    assert error.value.code == "TASK_CANCELLED" and closed.is_set()


@pytest.mark.parametrize("status,reason", [(412, "restricted"), (429, "rate_limited")])
async def test_short_link_explicit_platform_failures(monkeypatch, status, reason):
    monkeypatch.setattr(links, "validate_url", AsyncMock(side_effect=lambda url: url))
    with pytest.raises(AppException) as error:
        await links.resolve_short_link("https://b23.tv/abc", transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    assert error.value.source_failure == reason


def test_platform_mode_ignores_global_proxy_and_domestic_runtime(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YTDLP_PROXY", SecretStr("file:///invalid-global-secret"))
    monkeypatch.setattr(settings, "YOUTUBE_PROXY", SecretStr("http://operator:secret@proxy.example:8080"))
    monkeypatch.setattr(settings, "YTDLP_JS_RUNTIME", "deno")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    for url in ["https://www.bilibili.com/video/BVabc", "https://v.douyin.com/abc", "https://vimeo.com/123"]:
        assert runtime.server_proxy(url) is None
        options = ytdlp.extraction_options(None, url=url)
        assert options["proxy"] == "" and options["js_runtimes"] == {}
        assert options["cookiefile"] is None and options["remote_components"] == []
    assert runtime.server_proxy("https://youtu.be/dQw4w9WgXcQ").endswith("proxy.example:8080")
    with pytest.raises(AppException) as error:
        runtime.javascript_runtime_options("https://www.youtube.com/")
    assert error.value.code == "YTDLP_RUNTIME_UNAVAILABLE" and "secret" not in error.value.message
    monkeypatch.setattr(settings, "YOUTUBE_PROXY", SecretStr(""))
    with pytest.raises(AppException) as error:
        runtime.server_proxy("https://www.youtube.com/")
    assert error.value.code == "SOURCE_EGRESS_UNAVAILABLE"


def test_shared_backend_checks_platform_and_preserves_bilibili_parts():
    downloader = Mock()
    downloader.extract_info.return_value = {"extractor_key": "BiliBili", "id": "BVabc_p2"}
    assert dispatch.extract_source(downloader, "https://www.bilibili.com/video/BVabc?p=2")["id"] == "BVabc_p2"
    downloader.extract_info.return_value = {"extractor_key": "Youtube", "id": "other"}
    with pytest.raises(AppException) as error:
        dispatch.extract_source(downloader, "https://www.bilibili.com/video/BVabc")
    assert error.value.code == "VIDEO_ID_CHANGED"


def test_metadata_download_subtitle_all_use_the_shared_backend(monkeypatch, tmp_path):
    from app.modules.download import child
    from app.modules.transcript import subtitle_child
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(child.shutil, "which", lambda _: "/opt/ffmpeg")
    url = "https://www.bilibili.com/video/BVabc"
    info = {"id": "BVabc", "extractor_key": "BiliBili", "title": "fixture", "duration": 1, "formats": []}
    for module in [ytdlp_worker, child, subtitle_child]:
        guard = Mock(violation=None)
        guard.installed.return_value = nullcontext()
        monkeypatch.setattr(module, "NetworkGuard", Mock(return_value=guard))
        downloader = Mock()
        downloader.extract_info.side_effect = AssertionError("must dispatch through shared backend")
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=downloader)
        factory.return_value.__exit__ = Mock(return_value=False)
        monkeypatch.setattr(module, "guarded_downloader", factory)
        extractor = Mock(return_value=info)
        monkeypatch.setattr(module, "extract_source", extractor)
        if module is ytdlp_worker:
            assert module.extract_metadata(url)["data"]["platform"] == "bilibili"
        elif module is child:
            def stop(event):
                if event.get("_source_metadata_ready"):
                    raise AppException("TEST_STOP", "stop")
            with pytest.raises(AppException, match="stop"):
                module.download({"url": url, "platform": "bilibili", "platform_video_id": "BVabc"}, tmp_path, stop)
        else:
            events = []
            module.fetch({"url": url, "platform": "bilibili", "platform_video_id": "BVabc"}, tmp_path, events.append)
            assert events[-1] == {"subtitle_found": False}
        extractor.assert_called_once_with(downloader, url)
        assert factory.call_args.args[1]["proxy"] == ""


def test_bare_403_does_not_prove_platform_circuit_failure():
    assert ytdlp.source_failure(RuntimeError("HTTP Error 403: expired URL")) is None
    assert ytdlp.source_failure(RuntimeError("HTTP Error 412")) == "restricted"
    assert ytdlp.source_failure(RuntimeError("Sign in to confirm you're not a bot")) == "restricted"
    assert ytdlp.source_failure(RuntimeError("HTTP Error 429")) == "rate_limited"


async def test_platform_preflight_isolated_and_ffmpeg_capabilities_are_not_delivery_proof(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_EGRESS_MODE", "platform")
    monkeypatch.setattr(settings, "YTDLP_JS_RUNTIME", "disabled")
    monkeypatch.setattr(settings, "YOUTUBE_PROXY", SecretStr(""))
    monkeypatch.setattr(preflight, "probe_version", lambda *args: "4.4.2")
    monkeypatch.setattr(preflight.shutil, "which", lambda _: "/opt/ffmpeg")
    monkeypatch.setattr(preflight.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout="-vsync sync method")))
    native = await preflight.check(platform="bilibili")
    youtube = await preflight.check(platform="youtube")
    assert native["passed"] and native["egress"] == "direct" and not native["metadata_verified"]
    assert native["ffmpeg_features"] == {"fps_mode": False, "vsync": True, "render_verified": False}
    assert not youtube["passed"]
    assert {item["code"] for item in youtube["checks"]} >= {"SOURCE_EGRESS_UNAVAILABLE", "YTDLP_RUNTIME_UNAVAILABLE"}
    assert not youtube["download_verified"]
