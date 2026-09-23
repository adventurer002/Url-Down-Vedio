"""Phase7 live chain: speech clip -> parse/download/audio/transcribe(real
faster-whisper) -> summarize/mindmap(real LLM from .env).

Gated on LIVE_AI=1: burns a few LLM tokens and needs ffmpeg + `say`.
"""

import functools
import http.server
import os
import subprocess
import threading
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_AI") != "1", reason="needs LIVE_AI=1")


def _make_talking_clip(path: str) -> None:
    subprocess.run(
        ["say", "-o", "/tmp/say.aiff", "你好，这是一个语音转写测试。总结功能是否正常，请告诉我。"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
            "-i", "/tmp/say.aiff",
            "-pix_fmt", "yuv420p", "-shortest", path,
        ],
        check=True,
        capture_output=True,
    )


async def test_live_ai_chain(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core import url_safety
    from backend.app.core.config import settings
    from backend.app.db.models import ProcessingTask, Video
    from backend.app.providers.asr import FasterWhisperASR
    from backend.app.providers.storage import LocalStorageProvider
    from backend.app.services import ai_service as ai
    from backend.app.services import auth_service as auth
    from backend.app.services import billing as billing_service
    from backend.app.worker import ai_tasks as wt
    from backend.app.worker import tasks as dt

    clip = tmp_path / "talk.mp4"
    _make_talking_clip(str(clip))
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(var, None)
    os.environ["no_proxy"] = "*"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    url = f"{base}/talk.mp4"
    real_validate = url_safety.validate_url
    url_safety.validate_url = lambda u: url_safety.SafeURL(u, "127.0.0.1") if u.startswith(base) else real_validate(u)  # type: ignore[assignment]
    try:
        settings.worker_tmp_dir = str(tmp_path / "tmp")
        settings.storage_backend = "local"
        settings.redis_url = "redis://localhost:6379/0"

        engine = create_async_engine(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio"
        )
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = LocalStorageProvider(tmp_path / "media")

        async with maker() as s:
            from backend.app.db.seed import seed_plans

            try:
                await seed_plans(s)
            except Exception:  # noqa: BLE001 - seed is idempotent, retry once
                await s.rollback()
                await seed_plans(s)
            user, _, _ = await auth.register_user(
                s, f"live{uuid.uuid4().hex[:6]}@example.com", "password123", "live"
            )
            plan = await billing_service.get_plan_by_code(s, "monthly")
            await billing_service.activate_subscription(s, user.id, plan)
            uid = user.id
        from backend.app.services import download_service as downloads
        from backend.app.services import video_service as videos

        async with maker() as s:
            video = await videos.create_parsing_video(s, url, uid)
            vid: uuid.UUID = video.id
        await dt._parse_impl(vid, session_factory=maker)
        async with maker() as s:
            v = await s.get(Video, vid)
            assert v is not None and v.status == "ready", (v.status if v else None, v.error_message if v else None)
            assert v.formats
            first_format = v.formats[0]
            assert isinstance(first_format, dict)
            fmt = first_format["format_id"]
            task = await downloads.create_download_task(s, vid, fmt, uid, lambda tid: None)
            did = task.id
        await asyncio.wait_for(
            dt._download_impl(did, "live", session_factory=maker, storage=store),
            timeout=300,
        )
        llm = wt.build_llm()
        asr = FasterWhisperASR("tiny")
        for step in ("audio_extract", "transcribe", "summarize", "mindmap"):
            async with maker() as s:
                t, _, _ = await ai.get_or_create_ai_task(s, uid, vid, step, lambda tid: None)
                aid = t.id
            kwargs: dict[str, object] = {}
            if step in ("audio_extract", "transcribe"):
                kwargs = {"storage": store}
            if step == "transcribe":
                kwargs["asr"] = asr
            if step in ("summarize", "mindmap"):
                kwargs["llm"] = llm
            await asyncio.wait_for(
                wt._ai_impl(aid, "live", session_factory=maker, **kwargs),  # type: ignore[arg-type]
                timeout=600,
            )
            async with maker() as s:
                done = await s.get(ProcessingTask, aid)
                assert done is not None
                assert done.status == "completed", (step, done.error_code, done.error_message)
                assert done.output_id is not None
        async with maker() as s:
            from sqlalchemy import select

            from backend.app.db.models import MindMap, Summary, Transcript

            tr = (await s.execute(select(Transcript).where(Transcript.video_id == vid))).scalar_one()
            print("\nTRANSCRIPT:", tr.text[:120])
            su = (await s.execute(select(Summary).where(Summary.video_id == vid))).scalar_one()
            print("SUMMARY:", su.summary[:120])
            assert su.key_points and su.prompt_version
            mm = (await s.execute(select(MindMap).where(MindMap.video_id == vid))).scalar_one()
            assert mm.markdown.strip()
        await engine.dispose()
    finally:
        url_safety.validate_url = real_validate
        server.shutdown()
