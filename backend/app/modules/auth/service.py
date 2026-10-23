import asyncio
from dataclasses import asdict
import json
import secrets
from uuid import UUID, uuid4

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.auth.qr import get_qr_provider
from app.modules.auth.tokens import issue_token
from app.modules.tasks.service import database_call

TTL = 300
# Never consume a pending session while polling. Compare-and-delete is one atomic operation.
REDEEM = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local state = cjson.decode(raw)
if state.status == 'authenticated' then redis.call('DEL', KEYS[1]) end
return raw
"""
AUTHENTICATE = """
local raw = redis.call('GET', KEYS[1])
if not raw or redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local state = cjson.decode(raw)
if state.status ~= 'pending' then return 0 end
state.status = 'authenticated'
state.user_id = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(state), 'KEEPTTL')
redis.call('DEL', KEYS[2])
return 1
"""
INCREMENT = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then redis.call('EXPIRE', KEYS[1], 300) end
return value
"""


async def redis_call(method, *args, **kwargs):
    try:
        async with asyncio.timeout(2):
            return await method(*args, **kwargs)
    except (RedisError, TimeoutError):
        raise AppException("AUTH_UNAVAILABLE", "登录服务暂不可用，请稍后重试", 503) from None


class AuthService:
    def __init__(self, users, redis, qr=None):
        self.users, self.redis, self.qr = users, redis, qr

    async def create_session(self):
        scene_id = str(uuid4())
        qr_url = await (self.qr or get_qr_provider()).create(scene_id)
        for _ in range(30):
            code = str(secrets.randbelow(900000) + 100000)
            if await redis_call(self.redis.set, f"wechat:code:{code}", scene_id, nx=True, ex=TTL):
                break
        else:
            raise AppException("AUTH_BUSY", "登录请求较多，请稍后重试", 503)
        state = {"status": "pending", "verify_code": code, "user_id": None}
        await redis_call(self.redis.set, f"wechat:session:{scene_id}", json.dumps(state), ex=TTL)
        return {"scene_id": scene_id, "mode": settings.WECHAT_MP_MODE, "qr_url": qr_url,
                "verify_code": code, "expires_in": TTL}

    async def user(self, user_id):
        # DB remains authoritative, including when the Redis invalidation marker expires.
        user = await database_call(self.users.get, user_id)
        if user is None or user.status != "active":
            raise AppException("INVALID_TOKEN", "用户不存在或已停用", 401)
        if not user.is_subscribed:
            raise AppException("WECHAT_UNSUBSCRIBED", "请重新关注公众号后登录", 401)
        return user

    async def poll(self, scene_id):
        raw = await redis_call(self.redis.eval, REDEEM, 1, f"wechat:session:{scene_id}")
        if raw is None:
            return {"status": "expired"}
        state = json.loads(raw)
        if state["status"] != "authenticated":
            return {"status": "pending"}
        user = await self.user(UUID(state["user_id"]))
        return {"status": "authenticated", "access_token": issue_token(user.id), "user": asdict(user)}

    async def subscription(self, openid, subscribed, login=False):
        user = await database_call(self.users.subscription, openid, subscribed, login)
        key = f"user:unsubscribed:{user.id}"
        if subscribed:
            await redis_call(self.redis.delete, key)
        else:
            await redis_call(self.redis.set, key, "1", ex=settings.JWT_EXPIRE_MINUTES * 60)
        return user

    async def authenticate(self, openid, code):
        scene_id = await redis_call(self.redis.get, f"wechat:code:{code}")
        if not scene_id:
            return False
        raw = await redis_call(self.redis.get, f"wechat:session:{scene_id}")
        if not raw or json.loads(raw)["status"] != "pending":
            return False
        user = await self.subscription(openid, True, login=True)
        if user.status != "active":
            return False
        return bool(await redis_call(self.redis.eval, AUTHENTICATE, 2,
            f"wechat:session:{scene_id}", f"wechat:code:{code}", scene_id, str(user.id)))

    async def authenticate_scene(self, openid, scene_id):
        try:
            scene_id = str(UUID(scene_id))
        except ValueError:
            return False
        raw = await redis_call(self.redis.get, f"wechat:session:{scene_id}")
        return bool(raw) and await self.authenticate(openid, json.loads(raw)["verify_code"])

    async def verify(self, openid, code):
        key = f"wechat:errors:{openid}"
        if int(await redis_call(self.redis.get, key) or 0) >= 5:
            return "尝试过于频繁，请稍后再试"
        if await self.authenticate(openid, code):
            await redis_call(self.redis.delete, key)
            return "🎉 登录成功！请返回 VideoHub 网页继续使用。"
        await redis_call(self.redis.eval, INCREMENT, 1, key)
        return "口令无效或已过期，请刷新网页上的登录二维码重试。"

    async def mock_confirm(self, scene_id):
        raw = await redis_call(self.redis.get, f"wechat:session:{scene_id}")
        if not raw or not await self.authenticate("mock_" + str(scene_id), json.loads(raw)["verify_code"]):
            raise AppException("SESSION_EXPIRED", "登录会话已过期或已使用，请刷新重试", 409)
        return {"status": "authenticated"}
