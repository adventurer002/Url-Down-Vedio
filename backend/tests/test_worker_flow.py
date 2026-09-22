"""Worker flow tests with fakes: parse finish, download success, cancel triple."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.models import DownloadTask, Video
from backend.app.providers.downloader import DownloadProgress, VideoInfo
from backend.app.providers.storage import LocalStorageProvider
from backend.app.services.progress import cancel_key
from backend.app.worker import tasks as worker_tasks

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


class FakeDownloader:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self._fail = fail_with

    async def extract_info(self, url: str) -> VideoInfo:
        if self._fail:
            raise self._fail
        return VideoInfo(
            platform="test",
            platform_video_id="abc",
            title="demo",
            webpage_url=url,
            formats=[],
            raw_info={"id": "abc"},
        )

    async def download(
        self,
        url: str,
        format_id: str,
        dest_dir: Path,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool],
        proxy: str | None = None,
        cookie_file: Path | None = None,
    ) -> Path:
        on_progress(DownloadProgress(downloaded_bytes=5, total_bytes=10, percent=50.0))
        if should_cancel():
            from backend.app.providers.downloader import DownloadCancelled

            raise DownloadCancelled()
        out = dest_dir / "abc.mp4"
        out.write_bytes(b"0" * 64)
        on_progress(DownloadProgress(downloaded_bytes=10, total_bytes=10, percent=100.0))
        return out




def _patch_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.core import url_safety

    monkeypatch.setattr(
        url_safety, "validate_url", lambda url: url_safety.SafeURL(url, "x")
    )

async def _make_video(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as s:
        v = Video(
            source_url="https://public.example.com/v",
            platform="unknown",
            platform_video_id="",
            title="t",
            webpage_url="https://public.example.com/v",
            status="parsing",
        )
        s.add(v)
        await s.commit()
        return v.id


async def test_parse_impl_ready(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_url(monkeypatch)
    vid = await _make_video(maker)
    await worker_tasks._parse_impl(vid, session_factory=maker, downloader=FakeDownloader())
    async with maker() as s:
        v = await s.get(Video, vid)
        assert v is not None
        assert v.status == "ready"
        assert v.title == "demo"


async def test_parse_impl_failed(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.providers.ytdlp import PublicDownloadError

    _patch_url(monkeypatch)
    vid = await _make_video(maker)
    await worker_tasks._parse_impl(
        vid,
        session_factory=maker,
        downloader=FakeDownloader(PublicDownloadError("platform_blocked", "blocked", False)),
    )
    async with maker() as s:
        v = await s.get(Video, vid)
        assert v is not None
        assert v.status == "failed"


async def test_download_impl_success(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.app.core.config import settings

    _patch_url(monkeypatch)
    monkeypatch.setattr(settings, "worker_tmp_dir", str(tmp_path / "tmp"))
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    store = LocalStorageProvider(tmp_path / "media")

    vid = await _make_video(maker)
    async with maker() as s:
        v = await s.get(Video, vid)
        assert v is not None
        v.status = "ready"
        await s.commit()
    from backend.app.services import download_service as downloads

    async with maker() as s:
        task = await downloads.create_download_task(s, vid, "hd", None, lambda tid: None)
        task_id = task.id
    await worker_tasks._download_impl(
        task_id, "celery-test", session_factory=maker,
        downloader=FakeDownloader(), storage=store,
    )
    async with maker() as s:
        done = await s.get(DownloadTask, task_id)
        assert done is not None
        assert done.status == "completed", f"{done.error_code}: {done.error_message}"
        assert float(done.progress) == 100
        media = await downloads.task_media_file(s, task_id)
        assert media is not None
        assert media.expires_at is not None
    assert not (tmp_path / "tmp" / str(task_id)).exists()


async def test_download_impl_cancel(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import redis as sync_redis

    from backend.app.core.config import settings

    _patch_url(monkeypatch)
    monkeypatch.setattr(settings, "worker_tmp_dir", str(tmp_path / "tmp"))
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    store = LocalStorageProvider(tmp_path / "media")

    vid = await _make_video(maker)
    async with maker() as s:
        v = await s.get(Video, vid)
        assert v is not None
        v.status = "ready"
        await s.commit()
    from backend.app.services import download_service as downloads

    async with maker() as s:
        task = await downloads.create_download_task(s, vid, "hd", None, lambda tid: None)
        task_id = task.id
    r = sync_redis.Redis.from_url("redis://localhost:6379/0")
    r.set(cancel_key(task_id), "1", ex=60)
    await worker_tasks._download_impl(
        task_id, "celery-test", session_factory=maker,
        downloader=FakeDownloader(), storage=store,
    )
    r.delete(cancel_key(task_id))
    async with maker() as s:
        cancelled = await s.get(DownloadTask, task_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
    assert not (tmp_path / "tmp" / str(task_id)).exists()


async def test_cleanup_expired(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import datetime, timedelta

    from backend.app.core.config import settings
    from backend.app.db.models import MediaFile
    from backend.app.worker import tasks as wt

    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    media_dir = tmp_path / "media"
    store = LocalStorageProvider(media_dir)

    async with maker() as s:
        v = Video(
            source_url="https://x.example/v", platform="t", platform_video_id="1",
            title="t", webpage_url="https://x.example/v", status="ready",
        )
        s.add(v)
        await s.flush()
        key = "dev/anon/video/deadbeef.mp4"
        (media_dir / "dev" / "anon" / "video").mkdir(parents=True)
        (media_dir / "dev" / "anon" / "video" / "deadbeef.mp4").write_bytes(b"0" * 16)
        s.add(
            MediaFile(
                owner_user_id=None, task_id=None, video_id=v.id, kind="video",
                format="mp4", size_bytes=16, storage_backend="local",
                storage_key=key,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()
    removed = await wt._cleanup_impl(session_factory=maker, storage=store)
    assert removed == 1
    assert not (media_dir / "dev" / "anon" / "video" / "deadbeef.mp4").exists()
    async with maker() as s:
        rows = (await s.execute(select(MediaFile))).scalars().all()
        assert rows[0].deleted_at is not None
