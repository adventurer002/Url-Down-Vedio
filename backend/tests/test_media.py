"""Phase3 unit tests (external deps mocked). DB tests run against compose postgres."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.errors import AppError
from backend.app.core.url_safety import MAX_URL_LENGTH, validate_url
from backend.app.db.base import Base
from backend.app.db.models import Video
from backend.app.providers.ytdlp import classify_error
from backend.app.services import download_service as downloads

TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/downvedio_test"


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DB_URL)
    async with engine.connect() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.commit()
    await engine.dispose()


# ------------------------------------------------------------ url safety ---


def test_reject_non_http() -> None:
    with pytest.raises(AppError) as e:
        validate_url("ftp://example.com/x")
    assert e.value.code == "url_not_allowed"


def test_reject_empty_and_long() -> None:
    with pytest.raises(AppError):
        validate_url("")
    with pytest.raises(AppError):
        validate_url("http://x.com/" + "a" * (MAX_URL_LENGTH + 1))


def test_reject_private_ip() -> None:
    with pytest.raises(AppError) as e:
        validate_url("http://192.168.1.1/x")
    assert e.value.code == "url_not_allowed"


def test_reject_localhost() -> None:
    with pytest.raises(AppError):
        validate_url("http://127.0.0.1/x")


def test_reject_link_local() -> None:
    with pytest.raises(AppError):
        validate_url("http://169.254.1.1/x")


def test_reject_dns_to_private(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import socket as socket_mod

    def fake_getaddrinfo(host: str, port: None):  # type: ignore[no-untyped-def]
        return [(socket_mod.AF_INET, 0, 0, "", ("10.1.2.3", 80))]

    monkeypatch.setattr("backend.app.core.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(AppError):
        validate_url("http://safe-looking.example.com")


def test_classify_error() -> None:
    assert classify_error("ERROR: private video") == ("platform_blocked", False)
    assert classify_error("requested format not available: 137") == ("format_unavailable", False)
    assert classify_error("connection timed out") == ("timeout", True)


async def test_validate_public_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import socket as socket_mod

    def fake_getaddrinfo(host: str, port: None):  # type: ignore[no-untyped-def]
        return [(socket_mod.AF_INET, 0, 0, "", ("93.184.216.34", 80))]

    monkeypatch.setattr("backend.app.core.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    safe = validate_url("https://public.example.com/video")
    assert safe.host == "public.example.com"


# ------------------------------------------------------------ services ----


async def test_download_state_machine(session: AsyncSession) -> None:
    video = Video(
        source_url="https://example.com/v",
        platform="test",
        platform_video_id="1",
        title="t",
        webpage_url="https://example.com/v",
        status="ready",
        formats=[{"format_id": "hd", "resolution": "1080p"}],
    )
    session.add(video)
    await session.commit()

    task = await downloads.create_download_task(
        session, video.id, "hd", None, lambda tid: None
    )
    assert task.status == "queued"
    downloads.advance(task, "downloading")
    downloads.advance(task, "processing")
    downloads.advance(task, "uploading")
    downloads.advance(task, "completed")
    assert task.status == "completed"


def test_download_no_backwards() -> None:
    from backend.app.db.models import DownloadTask

    task = DownloadTask(
        user_id=None,
        video_id=uuid.uuid4(),
        format_id="x",
        quality="x",
        status="downloading",
        progress=0,
        celery_task_id="",
    )
    with pytest.raises(AppError):
        downloads.advance(task, "queued")
    task.status = "failed"
    with pytest.raises(AppError):
        downloads.advance(task, "completed")


async def test_cleanup_expired(session: AsyncSession) -> None:
    from datetime import datetime, timedelta

    from backend.app.db.models import MediaFile

    key = "dev/anon/video/x.mp4"
    session.add(
        MediaFile(
            owner_user_id=None,
            task_id=None,
            video_id=None,
            kind="video",
            format="mp4",
            size_bytes=10,
            storage_backend="local",
            storage_key=key,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    deleted: list[str] = []
    await session.commit()
    removed = await downloads.cleanup_expired(
        session, lambda k: deleted.append(k)
    )
    assert removed == 1
    assert deleted == [key]
    record = (await session.execute(sa.select(MediaFile))).scalars().first()
    assert record is not None
    assert record.deleted_at is not None