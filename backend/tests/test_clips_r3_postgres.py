"""0011 -> 0012 preserves artifacts; shared relations reject lossy rollback."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.modules.auth.repository import UserRepository
from app.modules.clips.models import ClipExportItem
from app.modules.clips.repository import ClipRepository
from app.modules.media.repository import MediaRepository, MediaRecord
from app.modules.tasks.repository import TaskRepository, utcnow
from app.modules.transcript.repository import TranscriptRepository
from app.modules.transcript.data import make_transcript
from app.modules.video.repository import VideoRepository
from app.modules.video.parsers.base import ParsedVideo


def test_shared_assets_migration_preserves_old_artifacts_and_guards_downgrade():
    base = sa.create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1})
    schema = "test_clip_asset_migration_" + uuid4().hex
    try:
        with base.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    except Exception:
        base.dispose()
        pytest.skip("PostgreSQL unavailable for isolated migration test")
    engine = sa.create_engine(settings.DATABASE_URL, connect_args={"connect_timeout": 1,
        "options": f"-c search_path={schema} -c statement_timeout=5000"})
    try:
        config = Config("alembic.ini")
        with patch("sqlalchemy.create_engine", return_value=engine):
            command.upgrade(config, "0011")
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            users = UserRepository(sessions)
            user = users.subscription("old-clips-owner", True, login=True)
            other = users.subscription("different-clips-owner", True, login=True)
            videos = VideoRepository(sessions)
            video_id = videos.get_or_create(ParsedVideo("youtube", "old-source", "old source", duration=12),
                                           "https://example.com/old-source")
            tasks = TaskRepository(sessions)
            transcript = TranscriptRepository(sessions)
            clips = ClipRepository(sessions)
            media = MediaRepository(sessions)
            plan_task = tasks.create("simulate_progress", video_id, user_id=user.id)
            assert tasks.start(plan_task.id)
            holder = {}
            def plan_writer(session):
                text = transcript.store(session, video_id, plan_task.id, "subtitle",
                    make_transcript("zh", [{"start": 0, "end": 3, "text": "旧内容"}]), "v1", "none")
                holder["plan_id"] = clips.store_plan(session, plan_task, text, "p"*64, {"candidates": []})
                return {"clip_plan_id": str(holder["plan_id"])}
            tasks.complete_with(plan_task.id, {}, plan_writer)
            plan_id, file_id = holder["plan_id"], uuid4()
            old_export, new_export = uuid4(), uuid4()
            seed = tasks.create("simulate_progress", video_id, {"plan_id": str(plan_id), "export_key": "old"}, user_id=user.id)
            assert tasks.start(seed.id)
            expiry = utcnow() + timedelta(hours=3)
            record = MediaRecord(file_id, seed.id, video_id, "clip_720_mp4", f"files/{file_id}.mp4",
                                 "old.mp4", "video/mp4", 1024, expiry, "clip", "a"*64, 0, 3)
            metadata = sa.MetaData()
            old_exports = sa.Table("clip_exports", metadata, autoload_with=engine)
            old_items = sa.Table("clip_export_items", metadata, autoload_with=engine)
            item = {"file_id": str(file_id), "expires_at": expiry.isoformat()}
            def old_writer(session):
                media.register(session, record)
                session.execute(old_exports.insert().values(id=old_export, user_id=user.id, task_id=seed.id,
                    plan_id=plan_id, cache_key="old", content={"items": [item]}))
                session.execute(old_items.insert().values(id=uuid4(), export_id=old_export, file_id=file_id,
                    position=0, content=item))
                return {"clip_export_id": str(old_export)}
            tasks.complete_with(seed.id, {}, old_writer)
            command.upgrade(config, "head")
            command.check(config)
            assert clips.export(user.id, old_export)["items"] == [item]
            assert media.get(file_id).expires_at == expiry
            assert clips.can_read_file(user.id, [file_id]) and not clips.can_read_file(other.id, [file_id])
            second = tasks.create("simulate_progress", video_id,
                {"plan_id": str(plan_id), "export_key": "new"}, user_id=user.id)
            assert tasks.start(second.id)
            tasks.complete_with(second.id, {}, lambda session: (
                clips.register_export(session, second, new_export, {"items": [{**item, "asset_key": "b"*64}]}),
                {"clip_export_id": str(new_export)})[1])
            with sessions() as session:
                assert session.scalar(sa.select(sa.func.count()).select_from(ClipExportItem).where(ClipExportItem.file_id == file_id)) == 2
            assert clips.export(user.id, new_export)["items"][0]["file_id"] == str(file_id)
            with pytest.raises(RuntimeError, match="Shared clip assets exist"):
                command.downgrade(config, "0011")
            command.check(config)
            with sessions.begin() as session:
                session.execute(sa.delete(ClipExportItem).where(ClipExportItem.export_id == new_export))
            command.downgrade(config, "0011")
            with engine.connect() as connection:
                assert connection.execute(sa.text("SELECT file_id FROM clip_export_items")).scalar_one() == file_id
            command.upgrade(config, "head")
            command.check(config)
            assert media.get(file_id).expires_at == expiry
    finally:
        engine.dispose()
        with base.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()
