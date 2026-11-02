"""Concurrent private source identity, unbilled probe CAS, and migration safety."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from test_allowances_postgres import postgres
from app.core.config import settings
from app.modules.auth.repository import UserRepository
from app.modules.tasks.models import Task
from app.modules.uploads.models import Upload
from app.modules.uploads.repository import UploadRepository
from app.modules.video.models import Video
from app.modules.video.repository import VideoRepository


def uploaded(sessions, user):
    uploads = UploadRepository(sessions)
    identifier = uuid4()
    uploads.create(identifier, user.id, "recording.mp3", "mp3", 1024)
    uploads.received(identifier, user.id, 1024, "a" * 64)
    return uploads, identifier


def test_postgres_concurrent_probe_completion_is_one_unbilled_task(postgres):
    sessions, tasks, quotas, user, _ = postgres
    uploads, identifier = uploaded(sessions, user)
    before = quotas.balance(user.id)["balances"]
    barrier = Barrier(3)
    def create(_):
        barrier.wait(timeout=5)
        return tasks.create("media_probe", input_payload={"upload_id": str(identifier)}, user_id=user.id)
    with ThreadPoolExecutor(max_workers=3) as pool:
        created = list(pool.map(create, range(3)))
    assert len({item.id for item in created}) == 1
    assert tasks.start(created[0].id)
    assert not tasks.start(created[0].id)
    assert quotas.balance(user.id)["balances"] == before
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Task).where(Task.task_type == "media_probe")) == 1


def test_postgres_probe_cancel_and_registration_share_cas(postgres):
    sessions, tasks, _, user, _ = postgres
    uploads, identifier = uploaded(sessions, user)
    task = tasks.create("media_probe", input_payload={"upload_id": str(identifier)}, user_id=user.id)
    assert tasks.start(task.id)
    barrier = Barrier(2)
    def complete():
        barrier.wait(timeout=5)
        return tasks.complete_with(task.id, {}, lambda session:
            uploads.ready(session, identifier, user.id, task.id, 60, "audio"))
    def cancel():
        barrier.wait(timeout=5)
        return tasks.request_cancel(task.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete), pool.submit(cancel)]
        for future in futures:
            future.result(timeout=10)
    final = tasks.get(task.id)
    if final.cancel_requested and final.status != "completed":
        final = tasks.finish(task.id, "cancelled", 0)
    item = uploads.get(identifier, user.id)
    assert (item["status"] == "ready") == (final.status == "completed")
    assert bool(item["video_id"]) == (final.status == "completed")


def test_postgres_upload_identity_is_unique_per_user(postgres):
    sessions, _, _, user, _ = postgres
    videos = VideoRepository(sessions)
    barrier = Barrier(3)
    def create(_):
        barrier.wait(timeout=5)
        with sessions.begin() as session:
            return videos.register_upload(session, user.id, "b" * 64, "recording.mp3", 60, "audio")
    with ThreadPoolExecutor(max_workers=3) as pool:
        identifiers = list(pool.map(create, range(3)))
    assert len(set(identifiers)) == 1
    other = UserRepository(sessions).subscription("other-upload-owner", True, login=True)
    with sessions.begin() as session:
        other_id = videos.register_upload(session, other.id, "b" * 64, "recording.mp3", 60, "audio")
    assert other_id != identifiers[0]


def test_postgres_migration_preserves_old_sources_and_refuses_lossy_downgrade():
    base = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1})
    schema = "test_upload_migration_" + uuid4().hex
    try:
        with base.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception:
        base.dispose()
        pytest.skip("PostgreSQL unavailable for isolated migration test")
    scoped = create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1,
        "options": f"-c search_path={schema} -c statement_timeout=5000"})
    try:
        config = Config("alembic.ini")
        with patch("sqlalchemy.create_engine", return_value=scoped):
            command.upgrade(config, "0009")
            old_id = uuid4()
            with scoped.begin() as connection:
                connection.execute(text("INSERT INTO videos (id,platform,platform_video_id,source_url,title) "
                    "VALUES (:id,'youtube','existing','https://example.com/old','existing')"), {"id": old_id})
            command.upgrade(config, "head")
            command.check(config)
            sessions = sessionmaker(bind=scoped, expire_on_commit=False)
            with sessions() as session:
                old = session.get(Video, old_id)
                assert old.source_kind == "link" and old.owner_id is None and old.source_url == "https://example.com/old"
            user = UserRepository(sessions).subscription("migration-upload", True, login=True)
            uploaded(sessions, user)
            # Received bytes have value even before a source has been registered.
            with pytest.raises(RuntimeError, match="Private media exists"):
                command.downgrade(config, "0009")
            with sessions.begin() as session:
                VideoRepository(sessions).register_upload(session, user.id, "c" * 64, "audio.mp3", 60, "audio")
            with pytest.raises(RuntimeError, match="Private media exists"):
                command.downgrade(config, "0009")
            command.check(config)
    finally:
        scoped.dispose()
        with base.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()
