"""Phase3 e2e smoke: real yt-dlp + FFmpeg + Redis + DB over a local clip server.

Gated on RUN_E2E=1 (needs ffmpeg + outbound localhost serving).
The sandbox DNS returns relay addresses that trip the SSRF filter, so this
test serves a generated clip over loopback and bypasses validation ONLY for
that local URL; the SSRF filter itself is covered by test_media.py.
"""

import functools
import http.server
import os
import threading
import uuid

import pytest

RUN_E2E = os.environ.get("RUN_E2E") == "1"

pytestmark = pytest.mark.skipif(not RUN_E2E, reason="needs RUN_E2E=1")


def _make_clip(path: str) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", path,
        ],
        check=True,
        capture_output=True,
    )


async def test_e2e_parse_download(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.core import url_safety
    from backend.app.core.config import settings
    from backend.app.db.models import DownloadTask, Video
    from backend.app.providers.storage import LocalStorageProvider
    from backend.app.services import download_service as downloads
    from backend.app.services import video_service as videos
    from backend.app.worker import tasks as wt

    clip = tmp_path / "clip.mp4"
    _make_clip(str(clip))
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(var, None)
    os.environ["no_proxy"] = "*"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    url = f"{base}/clip.mp4"
    try:
        real_validate = url_safety.validate_url

        def allow_local(u: str) -> url_safety.SafeURL:
            if u.startswith(base):
                return url_safety.SafeURL(u, "127.0.0.1")
            return real_validate(u)

        url_safety.validate_url = allow_local  # type: ignore[assignment]
        settings.worker_tmp_dir = str(tmp_path / "tmp")
        settings.storage_backend = "local"
        settings.redis_url = "redis://localhost:6379/0"

        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio"
        )
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = LocalStorageProvider(tmp_path / "media")

        async with maker() as s:
            video = await videos.create_parsing_video(s, url, None)
            vid: uuid.UUID = video.id
        await wt._parse_impl(vid, session_factory=maker)
        async with maker() as s:
            v = await s.get(Video, vid)
            assert v is not None
            assert v.status == "ready", v.error_message
            assert v.formats, "expected at least one format"
            fmt = v.formats[0]["format_id"]
            task = await downloads.create_download_task(s, vid, fmt, None, lambda tid: None)
            tid = task.id
        await asyncio.wait_for(
            wt._download_impl(tid, "e2e", session_factory=maker, storage=store),
            timeout=300,
        )
        async with maker() as s:
            t = await s.get(DownloadTask, tid)
            assert t is not None
            assert t.status == "completed", f"{t.error_code}: {t.error_message}"
            media = await downloads.task_media_file(s, tid)
            assert media is not None
            assert media.size_bytes > 0
        assert not (tmp_path / "tmp" / str(tid)).exists()
        await engine.dispose()
    finally:
        url_safety.validate_url = real_validate
        server.shutdown()
