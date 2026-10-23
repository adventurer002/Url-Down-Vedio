import asyncio
from io import BytesIO
from typing import Protocol
from urllib.parse import quote

import httpx
import qrcode
import qrcode.image.svg

from app.core.config import settings
from app.core.exceptions import AppException


class QRProvider(Protocol):
    async def create(self, scene_id: str) -> str: ...


class StaticQRProvider:
    async def create(self, scene_id: str) -> str:
        if settings.WECHAT_MP_STATIC_QR_URL:
            return settings.WECHAT_MP_STATIC_QR_URL
        # Real SVG QR, explicitly a local demo until an official-account URL is configured.
        buffer = BytesIO()
        qrcode.make("VideoHub local mock login", image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
        return "data:image/svg+xml," + quote(buffer.getvalue().decode())


class ParameterizedQRProvider:
    """Optional capability for accounts entitled to temporary parameterized QR codes."""
    async def create(self, scene_id: str) -> str:
        if not settings.WECHAT_MP_APP_ID or not settings.WECHAT_MP_APP_SECRET.get_secret_value():
            raise AppException("WECHAT_NOT_CONFIGURED", "带参二维码需要公众号 AppID 与 AppSecret", 503)
        try:
            # Fixed official endpoint, no redirects/proxies. Two bounded attempts total.
            async with asyncio.timeout(12):
                async with httpx.AsyncClient(timeout=httpx.Timeout(4, connect=2), trust_env=False,
                                             follow_redirects=False) as client:
                    for attempt in range(2):
                        try:
                            response = await client.get("https://api.weixin.qq.com/cgi-bin/token", params={
                                "grant_type": "client_credential", "appid": settings.WECHAT_MP_APP_ID,
                                "secret": settings.WECHAT_MP_APP_SECRET.get_secret_value()})
                            response.raise_for_status()
                            token = response.json()["access_token"]
                            response = await client.post("https://api.weixin.qq.com/cgi-bin/qrcode/create",
                                params={"access_token": token}, json={"expire_seconds": 300,
                                "action_name": "QR_STR_SCENE", "action_info": {"scene": {"scene_str": scene_id}}})
                            response.raise_for_status()
                            return "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=" + quote(response.json()["ticket"], safe="")
                        except (httpx.TransportError, httpx.HTTPStatusError):
                            if attempt:
                                raise
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError):
            raise AppException("WECHAT_UNAVAILABLE", "微信二维码服务暂不可用", 503) from None


def get_qr_provider() -> QRProvider:
    return ParameterizedQRProvider() if settings.WECHAT_MP_MODE == "parameterized_qr" else StaticQRProvider()
