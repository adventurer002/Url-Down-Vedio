"""Phase7: AI chain with fakes, gates, idempotency, usage metering."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.errors import AppError
from backend.app.db.models import UsageRecord, Video
from backend.app.providers.asr import TranscriptResult
from backend.app.providers.llm import ChatMessage, LLMResult
from backend.app.providers.storage import LocalStorageProvider
from backend.app.services import ai_service as ai
from backend.app.services import billing as billing_service

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


@pytest.fixture()
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(TEST_DB_URL)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


from backend.app.db.base import Base


class FakeLLM:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "fake"

    async def chat(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        response_format: dict[str, object] | None = None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(text=self._text, input_tokens=10, output_tokens=5, model="fake")


class FakeASR:
    async def transcribe(self, audio_path: str):  # type: ignore[no-untyped-def]
        from backend.app.providers.asr import Segment

        return TranscriptResult(
            language="zh",
            text="你好世界",
            segments=[Segment(start=0.0, end=1.0, text="你好世界")],
            duration_seconds=1,
            engine="fake",
        )


SUMMARY_JSON = (
    '{"summary": "摘要", "key_points": ["a"], '
    '"chapters": [{"title": "开场", "start": 0, "end": 10}], "keywords": ["k"]}'
)


async def _member(
    maker: async_sessionmaker[AsyncSession], email: str = "m@example.com"
) -> uuid.UUID:
    from backend.app.services import auth_service as auth

    async with maker() as s:
        from backend.app.db.seed import seed_plans

        await seed_plans(s)
        user, _, _ = await auth.register_user(s, email, "password123", "m")
        plan = await billing_service.get_plan_by_code(s, "monthly")
        await billing_service.activate_subscription(s, user.id, plan)
        return user.id


async def _video_with_file(
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    tmp_path: Path,
) -> uuid.UUID:
    import subprocess
    from datetime import UTC, datetime, timedelta

    from backend.app.db.models import MediaFile

    (tmp_path / "media" / "dev" / "u" / "video").mkdir(parents=True, exist_ok=True)
    src = tmp_path / "media" / "dev" / "u" / "video" / "blob.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-pix_fmt", "yuv420p", "-shortest", str(src),
        ],
        check=True,
        capture_output=True,
    )
    blob = src.read_bytes()
    async with maker() as s:
        v = Video(
            source_url="https://x.example/v", platform="t", platform_video_id="1",
            title="demo", webpage_url="https://x.example/v", status="ready",
            user_id=user_id, duration_seconds=60,
        )
        s.add(v)
        await s.flush()
        key = "dev/u/video/blob.mp4"
        s.add(
            MediaFile(
                owner_user_id=user_id, task_id=None, video_id=v.id, kind="video",
                format="mp4", size_bytes=len(blob), storage_backend="local",
                storage_key=key,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await s.commit()
        return v.id


def _no_cancel() -> bool:
    return False


async def test_full_chain_with_fakes(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "worker_tmp_dir", str(tmp_path / "tmp"))
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    store = LocalStorageProvider(tmp_path / "media")
    user_id = await _member(maker)
    vid = await _video_with_file(maker, user_id, tmp_path)

    async with maker() as s:
        task, created, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "audio_extract", lambda tid: None
        )
        assert created
        work = ai.work_dir_for(tmp_path / "tmp", task.id)
        media = await ai.run_audio_extract(s, task, store, work, _no_cancel)
        await s.commit()
        assert media.kind == "audio"
        task2, created2, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "transcribe", lambda tid: None
        )
        assert created2
        result = await ai.run_transcribe(
            s, task2, store, FakeASR(), ai.work_dir_for(tmp_path / "tmp", task2.id), _no_cancel
        )
        assert result.language == "zh"
        await s.commit()
        task3, _, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "summarize", lambda tid: None
        )
        summary = await ai.run_summarize(s, task3, FakeLLM(SUMMARY_JSON))
        assert summary.key_points == ["a"]
        assert summary.prompt_version
        await s.commit()
        task4, _, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "mindmap", lambda tid: None
        )
        mindmap = await ai.run_mindmap(s, task4, FakeLLM("# 标题\n"))
        assert mindmap.markdown.startswith("#")
        await s.commit()
        rows = (await s.execute(sa.select(UsageRecord))).scalars().all()
        actions = sorted(r.action for r in rows)
        assert actions == ["audio_extract", "mindmap", "summarize", "transcribe"]
        tr = next(r for r in rows if r.action == "transcribe")
        assert tr.asr_seconds == 1
        su = next(r for r in rows if r.action == "summarize")
        assert su.llm_input_tokens == 10 and su.llm_output_tokens == 5


async def test_trigger_idempotent_and_output_reuse(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    user_id = await _member(maker, "re@example.com")
    vid = await _video_with_file(maker, user_id, tmp_path)
    async with maker() as s:
        t1, created1, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "summarize", lambda tid: None
        )
        assert created1
        t2, created2, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "summarize", lambda tid: None
        )
        assert not created2 and t2.id == t1.id
        t1.status = "completed"
        await s.flush()
        from backend.app.db.models import Summary, Transcript

        tr = Transcript(
            video_id=vid, task_id=None, language="zh", text="t",
            segments=[], asr_engine="fake", duration_seconds=1,
        )
        s.add(tr)
        await s.flush()
        su = Summary(
            transcript_id=tr.id, video_id=vid, provider="fake", model="fake",
            summary="s", key_points=[], chapters=[], keywords=[], prompt_version="v1",
        )
        s.add(su)
        await s.flush()
        t1.output_id = su.id
        await s.commit()
        _, created3, out = await ai.get_or_create_ai_task(
            s, user_id, vid, "summarize", lambda tid: None
        )
        assert not created3 and out == su.id


async def test_summarize_requires_transcript(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    user_id = await _member(maker, "pre@example.com")
    vid = await _video_with_file(maker, user_id, tmp_path)
    async with maker() as s:
        task, _, _ = await ai.get_or_create_ai_task(
            s, user_id, vid, "summarize", lambda tid: None
        )
        with pytest.raises(AppError) as exc:
            await ai.run_summarize(s, task, FakeLLM("{}"))
        assert exc.value.code == "prerequisite_missing"


async def test_audio_requires_video_file(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:

    store = LocalStorageProvider(tmp_path / "media")
    user_id = await _member(maker, "nofile@example.com")
    async with maker() as s:
        v = Video(
            source_url="https://x.example/v", platform="t", platform_video_id="1",
            title="t", webpage_url="https://x.example/v", status="ready",
            user_id=user_id,
        )
        s.add(v)
        await s.commit()
        task, _, _ = await ai.get_or_create_ai_task(
            s, user_id, v.id, "audio_extract", lambda tid: None
        )
        with pytest.raises(AppError) as exc:
            await ai.run_audio_extract(
                s, task, store, tmp_path / "w", _no_cancel
            )
        assert exc.value.code == "prerequisite_missing"
