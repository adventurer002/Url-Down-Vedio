"""Native PostgreSQL UUID/timestamp cursor ordering under skipped cleanup records."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from test_allowances_postgres import postgres
from app.infrastructure.storage.local import LocalStorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.modules.media.repository import MediaRepository
from app.modules.tasks.models import Task
from app.modules.tasks.repository import utcnow
from app.modules.uploads.models import Upload
from app.modules.uploads.storage import UploadStorage


async def test_postgres_cleanup_cursor_passes_100_busy_uploads(postgres, tmp_path):
    sessions, tasks, _, user, _ = postgres
    storage = LocalStorageAdapter(tmp_path / "storage", 64, 0)
    files = UploadStorage(storage)
    identifiers = sorted(uuid4() for _ in range(103))
    expiry = utcnow() - timedelta(days=2)
    with sessions.begin() as session:
        for i, identifier in enumerate(identifiers):
            session.add(Upload(id=identifier, user_id=user.id, filename="fixture.mp3", extension="mp3",
                expected_bytes=1, size_bytes=1, status="ready", expires_at=expiry))
            if i < 100:
                session.add(Task(user_id=user.id, task_type="transcription", input_payload={"upload_id": str(identifier)},
                    expires_at=utcnow() + timedelta(hours=1)))
    for identifier in identifiers[-3:]:
        files.directory(identifier).mkdir()
        files.path(identifier).write_bytes(b"x")
    result = await cleanup_expired_files(storage, MediaRepository(sessions), tasks)
    assert result["uploads"] == 3
    assert all(not files.path(identifier).exists() for identifier in identifiers[-3:])
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Upload).where(Upload.purged_at.is_(None))) == 100
