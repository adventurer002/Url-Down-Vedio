"""Disposable admin UI QA: temporary SQLite/Redis, local mock identity, no production writes."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from redis.asyncio import Redis
import uvicorn
from app.core.config import settings
from app.infrastructure.database.base import Base
from app.main import create_app
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.service import AuthService
from app.modules.auth.repository import UserRepository
from app.modules.auth.qr import StaticQRProvider
from app.modules.admin.router import get_admin_service
from app.modules.admin.service import AdminService
from app.modules.tasks.repository import TaskRepository
from app.modules.tasks.router import get_task_service
from app.modules.tasks.service import TaskService
from app.modules.tasks.models import Task, TaskStatus
from app.modules.video.models import Video
from app.modules.system.service import StorageStatusService, get_storage_status_service, get_cleanup_service
from app.infrastructure.storage.local import LocalStorageAdapter
from app.infrastructure.storage.cleanup import cleanup_expired_files
from app.modules.media.repository import MediaRepository
from app.modules.billing import service as billing_service
from app.api.v1 import router as health_module
from sqlalchemy import text
import os
from uuid import uuid4

async def offline_dispatch(*args, **kwargs): return 0
class Bus:
    async def snapshot(self, task_id): return None
    async def request_task_cancel(self, task_id, persist): return await persist()


def main():
    billing_service.resume_queued = offline_dispatch
    settings.APP_ENV = 'development'
    settings.AUTH_REQUIRED = True
    settings.ALLOWANCES_ENABLED = False
    settings.WECHAT_MP_MODE = 'mock'
    settings.WECHAT_MP_STATIC_QR_URL = ''
    settings.JWT_SECRET_KEY = 'disposable-visual-fixture-secret-at-least-32-chars'
    settings.CORS_ORIGINS = ['http://127.0.0.1:5176']
    with tempfile.TemporaryDirectory(prefix='vh-admin-') as temporary:
        root = Path(temporary)
        redis_socket = root / 'redis.sock'
        log = open(root / 'redis.log', 'w')
        process = subprocess.Popen(['redis-server','--port','0','--unixsocket',str(redis_socket),'--save','','--appendonly','no','--unixsocketperm','700'], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while not redis_socket.exists() and time.monotonic() < deadline: time.sleep(.02)
            assert redis_socket.exists()
            redis = Redis(unix_socket_path=str(redis_socket), decode_responses=True)
            engine = create_engine('sqlite:///' + str(root / 'fixture.db'), connect_args={'check_same_thread':False})
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, expire_on_commit=False)
            users = UserRepository(sessions)
            admin = users.subscription('visual-admin', True, login=True)
            user = users.subscription('visual-user', True, login=True)
            service = AdminService(sessions)
            service.bootstrap(admin.id, 'visual-operator', '隔离浏览器验收', 'visual-bootstrap')
            storage = LocalStorageAdapter(root / 'storage')
            orphan = storage.root / 'files' / (str(uuid4()) + '.mp4')
            orphan.write_bytes(b'isolated preview expired media' * 4096)
            old = time.time() - 72 * 3600
            os.utime(orphan, (old,old))
            extra=[]
            for number in range(24):
                person=users.subscription('preview-user-' + str(number), True, login=True)
                extra.append(person)
                service.grant_user(users.get(admin.id), person.id, 'ai_100' if number%2 else 'monthly', '隔离演示权益', 'preview-grant-' + str(number), 'req_fixture_' + str(number))
            from app.modules.auth.models import User
            with sessions.begin() as session:
                session.get(User, admin.id).nickname = '本地验收管理员'
                session.get(User, user.id).nickname = '操作验收用户'
                for number,person in enumerate(extra):
                    session.get(User,person.id).nickname='演示用户 %02d' % number
                titles = ['在山海之间，找回生活的节奏','高效学习的三个方法','一小时读懂人工智能','城市漫步与生活记录','从目标到行动的实践指南']
                for index, (title, state) in enumerate(zip(titles, ['completed','completed','processing','queued','failed'])):
                    video = Video(platform='bilibili', platform_video_id='visual-' + str(index), source_url='https://example.com/fixture/' + str(index), title=title, duration=120, raw_metadata={})
                    session.add(video); session.flush()
                    created = datetime.now(timezone.utc) - timedelta(minutes=index*2)
                    session.add(Task(user_id=user.id, video_id=video.id, task_type=['download','transcription','ai_summary','ai_mindmap','download'][index], status=TaskStatus(state), progress=45 if state == 'processing' else 100 if state == 'completed' else 0, input_payload={}, created_at=created, started_at=None if state == 'queued' else created + timedelta(seconds=1), finished_at=created + timedelta(seconds=20) if state in {'completed','failed'} else None, error_code='SOURCE_UNAVAILABLE' if state == 'failed' else None))
            async def fixture_db_ready():
                try:
                    with sessions() as session: session.execute(text('SELECT 1'))
                    return True
                except Exception: return False
            async def fixture_redis_ready():
                try:
                    async with asyncio.timeout(1): return bool(await redis.ping())
                except Exception: return False
            health_module.check_db_ready = fixture_db_ready
            health_module.check_redis_ready = fixture_redis_ready
            class Cleaner:
                def run(self):
                    return asyncio.run(cleanup_expired_files(storage, MediaRepository(sessions), TaskRepository(sessions)))
            class FixtureAuth(AuthService):
                async def mock_confirm(self, scene_id):
                    if not await self.authenticate_scene('visual-admin', scene_id): raise RuntimeError('fixture session invalid')
                    return {'status':'authenticated'}
            app = create_app()
            # Only fixture-backed routes are available: never fall through to a configured
            # local database, Worker, download adapter or paid model provider.
            from fastapi.responses import JSONResponse
            @app.middleware("http")
            async def fixture_routes(request, call_next):
                path = request.url.path
                allowed = (path.startswith("/api/v1/admin/") or path.startswith("/api/v1/health/")
                           or path.startswith("/api/v1/auth/wechat/sessions")
                           or path in {"/api/v1/auth/me", "/api/v1/auth/logout", "/docs", "/openapi.json", "/wechat_qr.png"})
                if not allowed:
                    return JSONResponse({"error": {"code": "FIXTURE_ONLY", "message": "本地验收服务仅开放管理与模拟登录接口", "request_id": "req_fixture"}}, status_code=503)
                return await call_next(request)
            app.dependency_overrides[get_auth_service] = lambda: FixtureAuth(users, redis, StaticQRProvider())
            app.dependency_overrides[get_cleanup_service] = lambda: Cleaner()
            app.dependency_overrides[get_admin_service] = lambda: service
            app.dependency_overrides[get_task_service] = lambda: TaskService(TaskRepository(sessions), Bus())
            app.dependency_overrides[get_storage_status_service] = lambda: StorageStatusService(root / 'storage')
            print('Disposable fixture ready: http://127.0.0.1:18766 (no production data)', flush=True)
            # This process only owns ephemeral fixture data and listens on loopback.
            uvicorn.run(app, host='127.0.0.1', port=18766, log_level='critical', access_log=False)
        finally:
            process.terminate(); process.wait(timeout=5); log.close()


if __name__ == "__main__":
    main()
