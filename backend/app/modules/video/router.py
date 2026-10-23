from app.modules.auth.dependencies import creation_user
from fastapi import APIRouter, Depends

from app.core.response import success_response
from app.modules.video.parsers.ytdlp import YtDlpParser
from app.modules.video.schemas import ParseRequest, ParseResponse
from app.modules.video.service import VideoService

router = APIRouter(prefix="/videos", tags=["videos"])


def get_video_service() -> VideoService:
    return VideoService(YtDlpParser())


@router.post("/parse", response_model=ParseResponse)
async def parse_video(body: ParseRequest, service: VideoService = Depends(get_video_service), user=Depends(creation_user)):
    return success_response(await service.parse(body.url))
