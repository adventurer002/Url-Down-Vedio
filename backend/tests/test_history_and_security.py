from datetime import timedelta
import io
import logging
import os
import time
from unittest.mock import Mock
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from test_tasks import MemoryBus, repository
from test_download import storage, video
from test_media_and_transcript import seed_media, task_for
from app.modules.auth.dependencies import optional_user
from app.modules.auth.repository import UserRepository
from app.core.exceptions import AppException
from app.core.logging import SensitiveURLFilter, install_log_redaction, redact_urls
from app.infrastructure.storage import LocalStorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.main import create_app
from app.modules.media.models import MediaFile
from app.modules.media.repository import MediaRepository
from app.modules.media.service import MediaService
from app.modules.tasks.history import TaskHistoryService
from app.modules.tasks.models import Task
from app.modules.tasks.repository import utcnow
from app.modules.tasks.router import get_history_service
from app.modules.tasks.service import TaskService
from app.modules.system.router import get_cleanup_service
from app.modules.video.repository import VideoRepository


def history(repository, storage):
    return TaskHistoryService(TaskService(repository, MemoryBus()), VideoRepository(repository.sessions),
                              MediaService(MediaRepository(repository.sessions), storage))


async def test_task_history_pagination_order_title_and_empty_page(repository, storage, video):
    user = UserRepository(repository.sessions).subscription("history-user", True)
    ids = []
    for index in range(5):
        task = task_for(repository, video)
        ids.append(task.id)
        with repository.sessions.begin() as session:
            session.execute(update(Task).where(Task.id == task.id).values(user_id=user.id, created_at=utcnow() + timedelta(seconds=index)))
    service = history(repository, storage)
    app = create_app(); app.dependency_overrides[get_history_service] = lambda: service
    app.dependency_overrides[optional_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get('/api/v1/tasks?limit=2&offset=1')
        assert response.status_code == 200
        page = response.json()['data']
        assert page['total'] == 5 and page['limit'] == 2 and page['offset'] == 1
        assert [item['task_id'] for item in page['items']] == [str(ids[3]), str(ids[2])]
        assert page['items'][0]['video_title'] == video.title
        assert page['items'][0]['video']['platform_video_id'] == video.platform_video_id
        assert 'url' not in page['items'][0]['input_payload']
        assert (await client.get('/api/v1/tasks?offset=100')).json()['data']['items'] == []
        for query in ['limit=0', 'limit=101', 'offset=-1', 'limit=oops']:
            invalid = await client.get('/api/v1/tasks?' + query)
            assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'VALIDATION_ERROR'


async def test_history_uses_matching_progress_snapshot_and_database_on_redis_failure(repository, storage, video):
    task = task_for(repository, video)
    repository.start(task.id)
    bus = MemoryBus(); await bus.publish(task.id, 'running', 68)
    service = history(repository, storage); service.tasks.bus = bus
    assert (await service.list(20, 0))['items'][0]['progress'] == 68
    # A stale running snapshot cannot override committed cancellation.
    repository.request_cancel(task.id); repository.finish(task.id, 'cancelled', 20)
    item = (await service.list(20, 0))['items'][0]
    assert item['status'] == 'cancelled' and item['progress'] == 20
    async def unavailable(_): raise AppException('REDIS_UNAVAILABLE', 'offline', 503)
    bus.snapshot = unavailable
    assert (await service.list(20, 0))['items'][0]['status'] == 'cancelled'


async def test_history_only_exposes_valid_physical_media_and_retains_ai_ids(repository, storage, video):
    item = seed_media(repository, storage, video)
    with repository.sessions.begin() as session:
        session.execute(update(Task).where(Task.id == item.task_id).values(result_payload={'file_id': str(item.id), 'download_url': '/api/v1/files/x/download'}))
    service = history(repository, storage)
    row = (await service.list(20, 0))['items'][0]
    assert row['download_available'] and row['result_payload']['file_id'] == str(item.id)
    storage.remove_file(item.storage_key)
    row = (await service.list(20, 0))['items'][0]
    assert not row['download_available'] and not row['result_payload']
    ai = task_for(repository, video, 'ai_summary'); repository.start(ai.id)
    repository.finish(ai.id, 'completed', 100, {'result_id':'ai-result', 'transcript_id':'transcript'})
    assert next(item for item in (await service.list(20, 0))['items'] if item['task_id'] == str(ai.id))['result_payload']['result_id'] == 'ai-result'


@pytest.mark.parametrize('key', ['token','access_token','key','auth','sig','signature','cookie','sessdata','TOKEN','SeSsDaTa','%74oken','api_key'])
def test_sensitive_query_values_are_redacted(key):
    result = redact_urls(f'https://example.com/watch?id=public&{key}=top-secret%26suffix&quality=720#chapter')
    assert 'top-secret' not in result and 'suffix' not in result
    assert f'{key}=***REDACTED***' in result
    assert 'id=public' in result and 'quality=720' in result


@pytest.mark.parametrize('url', [
    'https://example.com/?token=first&token=second&access_token=third',
    '/api/v1/downloads?url=https%3A%2F%2Fexample.com%2F%3Ftoken%3Dfirst%26sig%3Dsecond',
    'https%253A%252F%252Fexample.com%252F%253Ftoken%253Dfirst%2526sig%253Dsecond',
    'https://user:first@example.com/?sessdata=second',
    'https%3A%2F%2Fexample.com%3Furl%3Dhttps%3A%2F%2Fother.com%3Ftoken%3Dfirst%26sig%3Dsecond',
])
def test_repeated_encoded_and_basic_auth_urls(url):
    safe = redact_urls(url)
    assert 'first' not in safe and 'second' not in safe and 'third' not in safe
    assert '***REDACTED***' in safe


def test_log_filter_covers_args_exception_extra_and_preserves_uvicorn_format():
    stream = io.StringIO(); handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveURLFilter()); handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.getLogger('videohub.test.security'); logger.setLevel(logging.INFO); logger.handlers = [handler]; logger.propagate = False
    install_log_redaction()
    logger.info('%s?token=%s&public=yes', 'https://example.com/', 'secret-one')
    logger.info('request %s', {'url': 'https://example.com/?signature=secret-two'})
    try: raise RuntimeError('https://example.com/?cookie=secret-three')
    except RuntimeError: logger.exception('download failed')
    record = logging.LogRecord('test', logging.INFO, '', 1, 'done', (), None)
    record.url = 'https://example.com/?auth=secret-four'
    handler.addFilter(SensitiveURLFilter()); handler.formatter = logging.Formatter('%(message)s %(url)s')
    handler.handle(record)
    text = stream.getvalue()
    assert not any(secret in text for secret in ['secret-one','secret-two','secret-three','secret-four'])
    assert 'Traceback' in text and 'RuntimeError' in text and 'public=yes' in text
    from uvicorn.logging import AccessFormatter
    access = logging.LogRecord('uvicorn.access', 20, '', 1, '%s - "%s %s HTTP/%s" %d',
        ('127.0.0.1:1234','GET','/watch?token=secret-access&id=public','1.1',200), None)
    SensitiveURLFilter().filter(access)
    rendered = AccessFormatter('%(request_line)s %(status_code)s', use_colors=False).format(access)
    assert 'secret-access' not in rendered and 'id=public' in rendered and '200' in rendered
    logger.handlers = []


def test_quota_counts_tmp_and_files_and_enforces_configured_limit(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'STORAGE_MAX_QUOTA_MB', 1)
    adapter = LocalStorageAdapter(tmp_path / 'storage', settings.STORAGE_MAX_QUOTA_MB, 0)
    (adapter.root / 'files' / (str(uuid4()) + '.mp4')).write_bytes(b'x' * (600 * 1024))
    directory = adapter.create_task_directory(uuid4()); (directory / 'partial').write_bytes(b'x' * (425 * 1024))
    with pytest.raises(AppException) as error: adapter.check_capacity()
    assert error.value.code == 'STORAGE_QUOTA_EXCEEDED' and error.value.status_code == 507


async def test_cleanup_freed_bytes_physical_expired_orphan_and_terminal_tmp(repository, storage, video):
    expired = seed_media(repository, storage, video, content=b'expired-data')
    valid = seed_media(repository, storage, video, content=b'keep-valid-file')
    with repository.sessions.begin() as session:
        session.execute(update(MediaFile).where(MediaFile.id == expired.id).values(expires_at=utcnow()-timedelta(seconds=1)))
    failed = task_for(repository, video); repository.finish(failed.id, 'failed', 0)
    tmp = storage.create_task_directory(failed.id); (tmp / 'partial').write_bytes(b'partial-data')
    active = task_for(repository, video); repository.start(active.id)
    active_tmp = storage.create_task_directory(active.id); (active_tmp / 'active').write_bytes(b'preserve')
    orphan_key = f'files/{uuid4()}.mp4'; orphan = storage.resolve_file(orphan_key); orphan.write_bytes(b'orphan-data')
    old = time.time()-48*3600; os.utime(orphan, (old,old))
    counts = await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository)
    assert counts['freed_bytes'] == len(b'expired-data') + len(b'partial-data') + len(b'orphan-data')
    assert counts['files'] == counts['temporary_directories'] == counts['orphan_files'] == 1
    assert not storage.resolve_file(expired.storage_key).exists() and not tmp.exists()
    assert storage.resolve_file(valid.storage_key).exists() and active_tmp.exists()
    with repository.sessions() as session: assert session.get(MediaFile, expired.id) is None
    assert (await cleanup_expired_files(storage, MediaRepository(repository.sessions), repository))['freed_bytes'] == 0


async def test_cleanup_endpoint_returns_bytes_and_unified_busy_error():
    app = create_app()
    service = Mock(); service.run.return_value = {'files':1,'temporary_directories':0,'orphan_files':0,'freed_bytes':1024,'used_bytes':0}
    app.dependency_overrides[get_cleanup_service] = lambda: service
    app.dependency_overrides[optional_user] = lambda: SimpleNamespace(role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/system/cleanup')
        assert response.status_code == 200 and response.json()['data']['freed_bytes'] == 1024
        service.run.side_effect = AppException('STORAGE_BUSY','存储正在使用',503)
        response = await client.post('/api/v1/system/cleanup')
        assert response.status_code == 503 and response.json()['error']['code'] == 'STORAGE_BUSY'


def test_non_sensitive_query_email_is_not_mistaken_for_basic_auth():
    value = 'https://example.com?email=user@example.com&quality=720'
    assert redact_urls(value) == value


async def test_history_delete_is_owned_persistent_and_keeps_jobs_and_grants(repository, storage):
    users = UserRepository(repository.sessions)
    owner = users.subscription("delete-history-owner", True)
    other = users.subscription("delete-history-other", True)
    active = repository.create("transcription", user_id=owner.id)
    repository.start(active.id)
    file_id = uuid4()
    completed = repository.create("download", user_id=owner.id)
    repository.finish(completed.id, "completed", 100, {"file_id": str(file_id)})
    foreign = repository.create("download", user_id=other.id)
    service = history(repository, storage)
    app = create_app()
    app.dependency_overrides[get_history_service] = lambda: service
    app.dependency_overrides[optional_user] = lambda: owner
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.delete(f"/api/v1/tasks/history/{foreign.id}")).status_code == 404
        assert (await client.delete(f"/api/v1/tasks/history/{uuid4()}")).status_code == 404
        deleted = await client.delete(f"/api/v1/tasks/history/{active.id}")
        assert deleted.json()["data"]["deleted_count"] == 1
        assert (await client.get("/api/v1/tasks")).json()["data"]["total"] == 1
        assert repository.get(active.id).status == "running"
        assert not repository.get(active.id).cancel_requested
        repository.finish(active.id, "completed", 100)
        assert (await client.get("/api/v1/tasks")).json()["data"]["total"] == 1
        # Bulk clear covers records beyond the visible first page.
        for _ in range(25):
            repository.create("download", user_id=owner.id)
        cleared = await client.delete("/api/v1/tasks/history")
        assert cleared.json()["data"]["deleted_count"] == 26
        assert (await client.get("/api/v1/tasks")).json()["data"]["total"] == 0
        assert (await client.delete("/api/v1/tasks/history")).json()["data"]["deleted_count"] == 0
        assert (await client.delete(f"/api/v1/tasks/history/{active.id}")).json()["data"]["deleted_count"] == 0
    assert repository.get(completed.id).result_payload["file_id"] == str(file_id)
    assert repository.has_result_grant("file_id", [file_id], owner.id)
    assert (await service.list(20, 0, user_id=other.id))["total"] == 1
    app.dependency_overrides[optional_user] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.delete("/api/v1/tasks/history")).status_code == 401
        assert (await client.delete(f"/api/v1/tasks/history/{foreign.id}")).status_code == 401


@pytest.mark.parametrize("role,status", [(None,401),("user",403)])
async def test_cleanup_rejects_non_admin_without_running_service(role,status):
    app = create_app()
    service = Mock()
    app.dependency_overrides[get_cleanup_service] = lambda: service
    app.dependency_overrides[optional_user] = lambda: SimpleNamespace(role=role) if role else None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/system/cleanup")
    assert response.status_code == status
    service.run.assert_not_called()
