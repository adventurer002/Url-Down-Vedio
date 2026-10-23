import asyncio
from dataclasses import asdict
import hashlib
import hmac
import logging
import re
import time
from uuid import UUID
from xml.sax.saxutils import escape

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from xml.etree.ElementTree import ParseError

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.response import success_response
from app.modules.auth.dependencies import get_auth_service, required_user
from app.modules.auth.service import redis_call

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def validate_signature(request):
    params = request.query_params
    if not all(params.get(k) for k in ("signature", "timestamp", "nonce")):
        raise AppException("WECHAT_SIGNATURE_INVALID", "微信签名校验失败", 403)
    token = settings.WECHAT_MP_TOKEN
    expected = hashlib.sha1(
        "".join(sorted([token, params["timestamp"], params["nonce"]])).encode()
    ).hexdigest()
    logger.info(
        "wechat webhook signature check: signature=%s timestamp=%s nonce=%s token=%s expected_sha1=%s",
        params["signature"],
        params["timestamp"],
        params["nonce"],
        token,
        expected,
    )
    if not hmac.compare_digest(expected, params["signature"]):
        raise AppException("WECHAT_SIGNATURE_INVALID", "微信签名校验失败", 403)


def reply(openid, account, content):
    content = content.replace("]]>", "]]]]><![CDATA[>")
    xml = (
        f"<xml><ToUserName>{escape(openid)}</ToUserName><FromUserName>{escape(account)}</FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content></xml>"
    )
    return Response(xml, media_type="application/xml")


@router.post("/wechat/sessions")
async def create_session(service=Depends(get_auth_service)):
    return success_response(await service.create_session())


@router.get("/wechat/sessions/{scene_id}")
async def poll_session(
    scene_id: UUID, response: Response, service=Depends(get_auth_service)
):
    result = await service.poll(str(scene_id))
    response.headers["Cache-Control"] = "no-store"
    if result["status"] == "authenticated":
        response.set_cookie(
            "access_token",
            result["access_token"],
            httponly=True,
            samesite="lax",
            path="/",
            max_age=settings.JWT_EXPIRE_MINUTES * 60,
            secure=settings.APP_ENV != "development",
        )
    return success_response(result)


@router.post("/wechat/sessions/{scene_id}/mock-confirm")
async def mock_confirm(scene_id: UUID, service=Depends(get_auth_service)):
    if settings.APP_ENV != "development" and settings.WECHAT_MP_MODE != "mock":
        raise AppException("FORBIDDEN", "生产环境禁止模拟登录", 403)
    return success_response(await service.mock_confirm(str(scene_id)))


@router.get("/me")
async def me(user=Depends(required_user)):
    return success_response(asdict(user))


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(
        "access_token",
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.APP_ENV != "development",
    )
    return success_response({"success": True})


@router.get("/wechat/webhook")
async def verify_webhook(request: Request):
    validate_signature(request)
    echostr = request.query_params.get("echostr", "")
    return PlainTextResponse(content=echostr, media_type="text/plain")


@router.post("/wechat/webhook")
async def webhook(request: Request, service=Depends(get_auth_service)):
    validate_signature(request)
    body = bytearray()
    try:
        async with asyncio.timeout(3):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise AppException("INVALID_WECHAT_XML", "微信消息过大", 413)
    except TimeoutError:
        raise AppException("WECHAT_BODY_TIMEOUT", "读取微信消息超时", 408) from None
    try:
        root = ElementTree.fromstring(bytes(body), forbid_dtd=True)
    except (ParseError, DefusedXmlException):
        raise AppException("INVALID_WECHAT_XML", "微信消息 XML 无效", 400) from None
    value = lambda name: root.findtext(name, default="")
    openid, account = value("FromUserName"), value("ToUserName")
    if not openid or len(openid) > 64:
        raise AppException("INVALID_WECHAT_XML", "微信用户标识无效", 400)
    msgid = value("MsgId") or f"{openid}_{value('CreateTime')}"
    key = f"wechat:msgid:{msgid}"
    if not await redis_call(service.redis.set, key, "1", nx=True, ex=60):
        return PlainTextResponse("success")
    try:
        if value("MsgType") == "text" and re.fullmatch(
            r"[0-9]{6}", value("Content").strip()
        ):
            return reply(
                openid, account, await service.verify(openid, value("Content").strip())
            )
        if value("MsgType") == "event":
            event = value("Event").lower()
            if event == "unsubscribe":
                await service.subscription(openid, False)
            elif event in {"subscribe", "scan"}:
                if event == "subscribe":
                    await service.subscription(openid, True)
                scene = value("EventKey").removeprefix("qrscene_")
                if settings.WECHAT_MP_MODE == "parameterized_qr" and scene:
                    if await service.authenticate_scene(openid, scene):
                        return reply(
                            openid,
                            account,
                            "🎉 登录成功！请返回 VideoHub 网页继续使用。",
                        )
                if event == "subscribe":
                    return reply(
                        openid,
                        account,
                        "欢迎关注！如需登录网页端，请直接发送网页弹窗中的 6 位数字口令。",
                    )
        return PlainTextResponse("success")
    except BaseException:
        # Let WeChat retry after a transient dependency failure.
        await redis_call(service.redis.delete, key)
        raise
