"""Real Redis verifies NX/TTL and atomic one-time session redemption."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from httpx import ASGITransport, AsyncClient
import jwt
import pytest
from redis.asyncio import Redis

from test_tasks import MemoryBus, redis_url, repository
from test_download import storage, video
from app.core.config import settings
from app.core.exceptions import AppException
from app.main import create_app
from app.modules.auth.dependencies import get_auth_service, task_user_id
from app.modules.auth.models import User
from app.modules.auth.repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.download.router import get_download_service, get_media_service
from app.modules.media.repository import MediaRepository
from app.modules.media.router import get_audio_service
from app.modules.media.service import MediaService
from app.modules.tasks.history import TaskHistoryService
from app.modules.tasks.router import get_history_service, get_task_service
from app.modules.tasks.service import TaskService
from app.modules.transcript.router import get_transcript_service
from app.modules.ai.router import get_ai_service
from app.modules.video.repository import VideoRepository
from app.modules.video.router import get_video_service


@pytest.fixture
async def auth(repository, storage, redis_url, monkeypatch):
    monkeypatch.setattr(settings, 'JWT_SECRET_KEY', 'stage9-isolated-test-secret-at-least-32-chars')
    monkeypatch.setattr(settings, 'AUTH_REQUIRED', False)
    monkeypatch.setattr(settings, 'APP_ENV', 'development')
    monkeypatch.setattr(settings, 'WECHAT_MP_MODE', 'mock')
    redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1)
    service = AuthService(UserRepository(repository.sessions), redis)
    tasks = TaskService(repository, MemoryBus(), SimpleNamespace(enqueue=lambda task_id: None))
    media = MediaService(MediaRepository(repository.sessions), storage)
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_task_service] = lambda: tasks
    app.dependency_overrides[get_media_service] = lambda: media
    app.dependency_overrides[get_history_service] = lambda: TaskHistoryService(tasks, VideoRepository(repository.sessions), media)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield SimpleNamespace(service=service, client=client, redis=redis, repository=repository,
                              tasks=tasks, app=app, media=media)
    await redis.aclose()


def signature(**patch):
    params = {'timestamp': '1234567890', 'nonce': 'nonce'}
    params['signature'] = hashlib.sha1(''.join(sorted([settings.WECHAT_MP_TOKEN, params['timestamp'], params['nonce']])).encode()).hexdigest()
    return {**params, **patch}


async def send(auth, openid, **fields):
    root = ET.Element('xml')
    for name, value in {'FromUserName':openid, 'ToUserName':'gh_videohub', 'CreateTime':'1234567890',
                        'MsgId':str(uuid4()), **fields}.items():
        ET.SubElement(root, name).text = value
    return await auth.client.post('/api/v1/auth/wechat/webhook', params=signature(), content=ET.tostring(root))


async def login(auth):
    openid = 'test_'+uuid4().hex
    session = await auth.service.create_session()
    response = await send(auth, openid, MsgType='text', Content=session['verify_code'])
    assert response.status_code == 200 and '登录成功' in response.text
    result = (await auth.client.get('/api/v1/auth/wechat/sessions/'+session['scene_id'])).json()['data']
    return openid, result


async def test_signature_plain_echo_and_rejection(auth):
    response = await auth.client.get('/api/v1/auth/wechat/webhook', params=signature(echostr='a<&中文'))
    assert response.status_code == 200 and response.text == 'a<&中文'
    assert response.headers['content-type'].startswith('text/plain')
    for params in [{}, signature(signature='bad')]:
        assert (await auth.client.get('/api/v1/auth/wechat/webhook', params=params)).status_code == 403
        assert (await auth.client.post('/api/v1/auth/wechat/webhook', params=params, content=b'<xml/>')).status_code == 403


async def test_unique_code_nx_ttl_and_pending_poll(auth, monkeypatch):
    from app.modules.auth import service as module
    first = await auth.service.create_session()
    values = iter([int(first['verify_code'])-100000, (int(first['verify_code'])-99999)%900000])
    monkeypatch.setattr(module.secrets, 'randbelow', lambda _: next(values))
    second = await auth.service.create_session()
    assert second['verify_code'] != first['verify_code']
    for session in [first, second]:
        assert session['verify_code'].isascii() and len(session['verify_code']) == 6
        assert session['qr_url'].startswith('data:image/svg+xml,')
        assert await auth.redis.get('wechat:code:'+session['verify_code']) == session['scene_id']
        assert 295 <= await auth.redis.ttl('wechat:session:'+session['scene_id']) <= 300
        assert await auth.service.poll(session['scene_id']) == {'status':'pending'}
    assert await auth.service.poll(str(uuid4())) == {'status':'expired'}


async def test_login_xml_cookie_and_concurrent_single_redemption(auth):
    session = await auth.service.create_session()
    openid = 'test_'+uuid4().hex
    response = await send(auth, openid, MsgType='text', Content='  '+session['verify_code']+'  ')
    root = ET.fromstring(response.text)
    assert root.findtext('ToUserName') == openid and root.findtext('FromUserName') == 'gh_videohub'
    assert '<MsgType><![CDATA[text]]></MsgType>' in response.text and '登录成功' in root.findtext('Content')
    assert await auth.redis.get('wechat:code:'+session['verify_code']) is None
    path = '/api/v1/auth/wechat/sessions/'+session['scene_id']
    responses = await asyncio.gather(*(auth.client.get(path) for _ in range(8)))
    winners = [r for r in responses if r.json()['data']['status'] == 'authenticated']
    assert len(winners) == 1
    result = winners[0].json()['data']; cookie = winners[0].headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Path=/' in cookie
    assert winners[0].headers['cache-control'] == 'no-store'
    assert result['user']['nickname'] == '微信用户_'+openid[-4:]
    with auth.repository.sessions() as db:
        user = db.get(User, UUID(result['user']['id']))
        assert user.last_login_at is not None and user.is_subscribed
    assert (await auth.client.get('/api/v1/auth/me')).status_code == 200
    assert 'Max-Age=0' in (await auth.client.post('/api/v1/auth/logout')).headers['set-cookie']
    assert (await auth.client.get('/api/v1/auth/me')).status_code == 401


async def test_wrong_code_rate_limit_and_dedup(auth):
    openid = 'test_'+uuid4().hex
    for _ in range(5):
        assert '口令无效或已过期' in (await send(auth, openid, MsgType='text', Content='000000')).text
    assert '尝试过于频繁，请稍后再试' in (await send(auth, openid, MsgType='text', Content='000000')).text
    assert 295 <= await auth.redis.ttl('wechat:errors:'+openid) <= 300
    msgid = str(uuid4())
    await send(auth, openid, MsgId=msgid, MsgType='text', Content='000000')
    response = await send(auth, openid, MsgId=msgid, MsgType='text', Content='000000')
    assert response.text == 'success' and response.headers['content-type'].startswith('text/plain')


async def test_plain_fallback_xxe_and_body_limit(auth):
    response = await send(auth, 'test_'+uuid4().hex, MsgType='text', Content='你好')
    assert response.text == 'success' and response.headers['content-type'].startswith('text/plain')
    for body, status in [(b'<!DOCTYPE xml [<!ENTITY x SYSTEM "file:///etc/passwd">]><xml><FromUserName>&x;</FromUserName></xml>', 400),
                         (b'x'*65537, 413)]:
        response = await auth.client.post('/api/v1/auth/wechat/webhook', params=signature(), content=body)
        assert response.status_code == status and response.json()['error']['code'] == 'INVALID_WECHAT_XML'


async def test_unsubscribe_persistent_block_and_resubscribe(auth):
    openid, result = await login(auth)
    assert (await send(auth, openid, MsgType='event', Event='unsubscribe', MsgId='')).text == 'success'
    key = 'user:unsubscribed:'+result['user']['id']
    assert await auth.redis.get(key) == '1'
    for _ in range(2):
        response = await auth.client.get('/api/v1/auth/me')
        assert response.status_code == 401 and response.json()['error']['code'] == 'WECHAT_UNSUBSCRIBED'
        await auth.redis.delete(key)
    assert '欢迎关注' in (await send(auth, openid, MsgType='event', Event='subscribe')).text
    assert await auth.redis.get(key) is None
    assert (await auth.client.get('/api/v1/auth/me')).status_code == 200


async def test_mock_gate_parameterized_subscribe_and_scan(auth, monkeypatch):
    session = await auth.service.create_session()
    path = f"/api/v1/auth/wechat/sessions/{session['scene_id']}/mock-confirm"
    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    monkeypatch.setattr(settings, 'WECHAT_MP_MODE', 'verify_code')
    assert (await auth.client.post(path)).status_code == 403
    monkeypatch.setattr(settings, 'WECHAT_MP_MODE', 'mock')
    assert (await auth.client.post(path)).status_code == 200
    assert (await auth.client.post(path)).status_code == 409
    monkeypatch.setattr(settings, 'WECHAT_MP_MODE', 'parameterized_qr')
    auth.service.qr = SimpleNamespace(create=AsyncMock(return_value='https://mp.weixin.qq.com/qr'))
    for event in ['subscribe', 'SCAN']:
        session = await auth.service.create_session()
        response = await send(auth, 'test_'+uuid4().hex, MsgType='event', Event=event,
                              EventKey=('qrscene_' if event == 'subscribe' else '')+session['scene_id'])
        assert '登录成功' in response.text
        assert (await auth.service.poll(session['scene_id']))['status'] == 'authenticated'


async def test_ownership_header_cookie_query_history_and_cancel(auth):
    _, a = await login(auth); _, b = await login(auth)
    own = auth.repository.create('download', user_id=UUID(a['user']['id']))
    other = auth.repository.create('download', user_id=UUID(b['user']['id']))
    anonymous = auth.repository.create('download')
    auth.repository.finish(own.id, 'completed', 100)
    headers = {'Authorization':'Bearer '+a['access_token']}
    assert (await auth.client.get(f'/api/v1/tasks/{own.id}', headers=headers)).status_code == 200
    for suffix, method in [('', 'get'), ('/cancel', 'post'), ('/events', 'get')]:
        response = await getattr(auth.client, method)(f'/api/v1/tasks/{other.id}{suffix}', headers=headers)
        assert response.status_code == 403 and response.json()['error']['code'] == 'FORBIDDEN'
    assert not auth.repository.get(other.id).cancel_requested
    assert (await auth.client.get(f'/api/v1/tasks/{other.id}')).status_code == 200
    history = (await auth.client.get('/api/v1/tasks', headers=headers)).json()['data']
    assert history['total'] == 1 and history['items'][0]['task_id'] == str(own.id)
    auth.client.cookies.clear()
    assert (await auth.client.get('/api/v1/tasks')).json()['data']['items'] == []
    assert (await auth.client.get(f'/api/v1/tasks/{anonymous.id}')).status_code == 200
    assert (await auth.client.get(f'/api/v1/tasks/{own.id}')).status_code == 403
    assert (await auth.client.get(f'/api/v1/tasks/{own.id}', params={'token':a['access_token']})).status_code == 403
    response = await auth.client.get(f'/api/v1/tasks/{own.id}/events', params={'token':a['access_token']})
    assert response.status_code == 200 and 'event: progress' in response.text
    assert (await auth.client.get('/api/v1/auth/me', params={'token':a['access_token']})).status_code == 401
    assert (await auth.client.get('/api/v1/auth/me', headers={'Authorization':'Bearer invalid'})).status_code == 401


async def test_file_header_cookie_query_and_cross_user_denial(auth, storage, video):
    from app.modules.media.models import MediaFile
    from app.modules.tasks.repository import utcnow
    _, a = await login(auth); await login(auth)  # Cookie now identifies B.
    video_id = VideoRepository(auth.repository.sessions).get_or_create(video, 'https://example.com/v')
    task = auth.repository.create('download', video_id=video_id, user_id=UUID(a['user']['id']))
    file_id = uuid4(); key = f'files/{file_id}.mp4'
    storage.resolve_file(key).write_bytes(b'file')
    with auth.repository.sessions.begin() as db:
        db.add(MediaFile(id=file_id, task_id=task.id, video_id=video_id, preset_id='video_720_mp4', storage_key=key,
                         filename='file.mp4', mime_type='video/mp4', size_bytes=4, expires_at=utcnow()+timedelta(hours=1)))
    path = f'/api/v1/files/{file_id}/download'
    assert (await auth.client.get(path)).status_code == 403
    response = await auth.client.get(path, headers={'Authorization':'Bearer '+a['access_token']})
    assert response.status_code == 200 and response.content == b'file'
    auth.client.cookies.clear()
    assert (await auth.client.get(path)).status_code == 403
    assert (await auth.client.get(path, params={'token':a['access_token']})).content == b'file'
    auth.client.cookies.set('access_token', a['access_token'])
    assert (await auth.client.get(path)).status_code == 200


async def test_creation_gate_owner_before_dispatch_and_anonymous_compatibility(auth, monkeypatch):
    seen = []
    def enqueue(task_id): seen.append(auth.repository.get(task_id).user_id)
    auth.tasks.dispatcher = SimpleNamespace(enqueue=enqueue)
    async def create(*args): return await auth.tasks.create('download')
    business = SimpleNamespace(create=create)
    for dependency in [get_download_service, get_audio_service, get_transcript_service, get_ai_service]:
        auth.app.dependency_overrides[dependency] = lambda: business
    auth.app.dependency_overrides[get_video_service] = lambda: SimpleNamespace(parse=AsyncMock(return_value={}))
    endpoints = [('/downloads', {'url':'https://example.com','preset_id':'audio_mp3'}),
                 ('/audio/extractions', {'url':'https://example.com'}), ('/transcriptions', {'url':'https://example.com'}),
                 ('/ai/summaries', {'url':'https://example.com'}), ('/ai/mindmaps', {'url':'https://example.com'})]
    monkeypatch.setattr(settings, 'AUTH_REQUIRED', True)
    for path, body in endpoints+[('/videos/parse', {'url':'https://example.com'})]:
        response = await auth.client.post('/api/v1'+path, json=body)
        assert response.status_code == 401 and response.json()['error']['code'] == 'AUTH_REQUIRED'
    assert seen == []
    openid, user = await login(auth)
    for path, body in endpoints:
        response = await auth.client.post('/api/v1'+path, json=body)
        assert response.status_code == 202 and seen[-1] == UUID(user['user']['id'])
    assert task_user_id.get() is None
    await send(auth, openid, MsgType='event', Event='unsubscribe')
    for path, body in endpoints:
        assert (await auth.client.post('/api/v1'+path, json=body)).json()['error']['code'] == 'WECHAT_UNSUBSCRIBED'
    auth.client.cookies.clear(); monkeypatch.setattr(settings, 'AUTH_REQUIRED', False)
    assert (await auth.client.post('/api/v1/downloads', json=endpoints[0][1])).status_code == 202
    assert seen[-1] is None


async def test_jwt_expiry_algorithm_and_disabled_user(auth):
    _, result = await login(auth); user_id = UUID(result['user']['id'])
    for claims, key, algorithm in [
        ({'sub':str(user_id), 'exp':datetime.now(timezone.utc)-timedelta(seconds=1)}, settings.JWT_SECRET_KEY, 'HS256'),
        ({'sub':str(user_id)}, settings.JWT_SECRET_KEY, 'HS256'), ({'sub':str(user_id)}, '', 'none')]:
        token = jwt.encode(claims, key, algorithm=algorithm)
        assert (await auth.client.get('/api/v1/auth/me', headers={'Authorization':'Bearer '+token})).status_code == 401
    with auth.repository.sessions.begin() as db: db.get(User, user_id).status = 'disabled'
    assert (await auth.client.get('/api/v1/auth/me')).status_code == 401


async def test_concurrent_binding_and_unsubscribe_before_redemption(auth):
    session = await auth.service.create_session()
    results = await asyncio.gather(*(auth.service.authenticate('test_'+uuid4().hex, session['verify_code']) for _ in range(2)))
    assert sum(results) == 1
    state = json.loads(await auth.redis.get('wechat:session:'+session['scene_id']))
    with auth.repository.sessions() as db: openid = db.get(User, UUID(state['user_id'])).wechat_openid
    await auth.service.subscription(openid, False)
    response = await auth.client.get('/api/v1/auth/wechat/sessions/'+session['scene_id'])
    assert response.status_code == 401 and response.json()['error']['code'] == 'WECHAT_UNSUBSCRIBED'


async def test_already_connected_sse_stops_after_unsubscribe(auth):
    openid, result = await login(auth); user_id = UUID(result['user']['id'])
    task = auth.repository.create('download', user_id=user_id)
    subscription, initial = await auth.tasks.open_events(task.id)
    stream = auth.tasks.events(task.id, subscription, initial, lambda: auth.service.user(user_id))
    assert (await anext(stream))['status'] == 'queued'
    await auth.service.subscription(openid, False)
    with pytest.raises(AppException, match='重新关注'): await anext(stream)
    subscription.close.assert_awaited_once()


async def test_shared_cache_requires_a_completed_grant(auth):
    _, a = await login(auth); _, b = await login(auth)
    owner = auth.repository.create('audio_extraction', user_id=UUID(a['user']['id']))
    file_id = uuid4()
    user_b = await auth.service.user(UUID(b['user']['id']))
    with pytest.raises(AppException) as denied:
        await auth.tasks.authorize_file(file_id, owner.id, user_b)
    assert denied.value.status_code == 403
    reused = auth.repository.create('audio_extraction', user_id=user_b.id)
    auth.repository.finish(reused.id, 'failed', 0, {'file_id':str(file_id)})
    with pytest.raises(AppException): await auth.tasks.authorize_file(file_id, owner.id, user_b)
    completed = auth.repository.create('audio_extraction', user_id=user_b.id)
    auth.repository.finish(completed.id, 'completed', 100, {'file_id':str(file_id)})
    await auth.tasks.authorize_file(file_id, owner.id, user_b)
    with pytest.raises(AppException): await auth.tasks.authorize_file(file_id, owner.id, None)


async def test_redis_failure_is_sanitized_and_webhook_retry_can_recover(auth):
    from redis.exceptions import ConnectionError
    original = auth.service.subscription
    auth.service.subscription = AsyncMock(side_effect=AppException('DATABASE_UNAVAILABLE', '数据库暂不可用', 503))
    openid = 'test_'+uuid4().hex
    fields = {'MsgType':'event', 'Event':'subscribe', 'MsgId':str(uuid4())}
    response = await send(auth, openid, **fields)
    assert response.status_code == 503
    auth.service.subscription = original
    assert '欢迎关注' in (await send(auth, openid, **fields)).text
    auth.service.redis = SimpleNamespace(get=AsyncMock(side_effect=ConnectionError('redis://secret:password@internal')))
    response = await auth.client.post('/api/v1/auth/wechat/sessions/'+str(uuid4())+'/mock-confirm')
    assert response.status_code == 503 and response.json()['error']['code'] == 'AUTH_UNAVAILABLE'
    assert 'password' not in response.text


async def test_parameterized_qr_adapter_retries_and_sanitizes_errors(monkeypatch):
    import httpx
    from pydantic import SecretStr
    from app.modules.auth.qr import ParameterizedQRProvider, StaticQRProvider
    monkeypatch.setattr(settings, 'WECHAT_MP_APP_ID', 'test-app')
    monkeypatch.setattr(settings, 'WECHAT_MP_APP_SECRET', SecretStr('test-only-secret'))
    monkeypatch.setattr(settings, 'WECHAT_MP_STATIC_QR_URL', 'https://example.com/wechat.png')
    assert await StaticQRProvider().create('scene') == 'https://example.com/wechat.png'
    original = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token':'wechat-test-access-token'})
        if len(calls) == 2:
            raise httpx.ConnectTimeout('upstream unavailable')
        assert json.loads(request.content)['action_info']['scene']['scene_str'] == 'scene'
        return httpx.Response(200, json={'ticket':'ticket/encoded'})
    def factory(**kwargs):
        assert kwargs['timeout'].connect == 2 and kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    assert (await ParameterizedQRProvider().create('scene')).endswith('ticket=ticket%2Fencoded')
    assert len(calls) == 4
    calls.clear()
    def unavailable(request):
        calls.append(request)
        return httpx.Response(503)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(unavailable)))
    with pytest.raises(AppException) as error: await ParameterizedQRProvider().create('scene')
    assert error.value.code == 'WECHAT_UNAVAILABLE' and len(calls) == 2
    assert 'test-only-secret' not in error.value.message


async def test_result_routes_block_cross_user_and_preserve_shared_cache_grants(auth, video):
    from app.modules.ai.repository import AIRepository
    from app.modules.ai.service import AIService
    from app.modules.ai.providers.base import Usage
    from app.modules.transcript.repository import TranscriptRepository
    from app.modules.transcript.service import TranscriptService
    from app.modules.transcript.data import make_transcript
    _, a = await login(auth); _, b = await login(auth)
    video_id = VideoRepository(auth.repository.sessions).get_or_create(video, 'https://example.com/results')
    source = auth.repository.create('transcription', video_id=video_id, user_id=UUID(a['user']['id']))
    transcripts = TranscriptRepository(auth.repository.sessions)
    results = AIRepository(auth.repository.sessions)
    with auth.repository.sessions.begin() as db:
        transcript = transcripts.store(db, video_id, source.id, 'subtitle',
            make_transcript('zh', [{'start':0, 'end':1, 'text':'私有任务中的公开资源文稿'}]), 'v1', 'none')
        result = results.store(db, source.id, transcript, 'summary', 'heuristic', 'model-test', 'v1', 'zh',
                               {'overview':'私有任务结果'}, Usage())
    auth.app.dependency_overrides[get_transcript_service] = lambda: TranscriptService(None, auth.tasks, transcripts)
    auth.app.dependency_overrides[get_ai_service] = lambda: AIService(None, auth.tasks, results)
    paths = [f'/api/v1/transcriptions/{transcript.id}', f'/api/v1/ai/results/{result.id}']
    for path in paths:
        assert (await auth.client.get(path)).status_code == 403
        assert (await auth.client.get(path, headers={'Authorization':'Bearer '+a['access_token']})).status_code == 200
    shared = auth.repository.create('ai_summary', video_id=video_id, user_id=UUID(b['user']['id']))
    auth.repository.finish(shared.id, 'completed', 100, {'result_id':str(result.id)})
    for path in paths: assert (await auth.client.get(path)).status_code == 200
    auth.client.cookies.clear()
    for path in paths: assert (await auth.client.get(path)).status_code == 403
    openid = 'test_'+uuid4().hex
    user = await auth.service.subscription(openid, True, login=True)
    from app.modules.auth.tokens import issue_token
    auth.client.cookies.set('access_token', issue_token(user.id))
    await auth.service.subscription(openid, False)
    for path in paths:
        response = await auth.client.get(path)
        assert response.status_code == 401 and response.json()['error']['code'] == 'WECHAT_UNSUBSCRIBED'
