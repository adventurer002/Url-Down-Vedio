from dataclasses import asdict
from uuid import UUID
from types import SimpleNamespace

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.tasks.service import TaskService, database_call
from app.modules.transcript.repository import TranscriptRepository
from app.modules.video.resolver import VideoResolver


class TranscriptService:
    def __init__(self, resolver: VideoResolver, tasks: TaskService, transcripts: TranscriptRepository):
        self.resolver, self.tasks, self.transcripts = resolver, tasks, transcripts

    async def create(self, url: str | None, language: str | None, *, upload_id=None, user_id=None):
        from app.modules.video.sources import resolve_input
        payload = await resolve_input(self.resolver, url, upload_id, user_id)
        return await self.tasks.create("transcription", UUID(payload["video_id"]),
                                       {**payload, "language": language.lower() if language else None,
                                        "content_version": settings.TRANSCRIPT_CONTENT_VERSION, "preset_id": "audio_mp3"})

    async def get(self, transcript_id: UUID, user=None):
        transcript = await database_call(self.transcripts.get, transcript_id)
        try:
            await self.tasks.authorize_artifact("transcript_id", transcript_id, transcript.task_id, user)
        except AppException as error:
            if error.code != "FORBIDDEN":
                raise
            # A completed AI task also grants access to the exact source transcript of its cached result.
            from app.modules.ai.repository import AIRepository
            ids = await database_call(AIRepository(self.transcripts.sessions).ids_for_transcript, transcript_id)
            if not await self.tasks.can_read_results("result_id", ids, user):
                raise
        return asdict(transcript)

    async def ai_source(self, transcript_id, user_id):
        if user_id is None:
            raise AppException("AUTH_REQUIRED", "请登录后使用已有文稿", 401)
        data = await self.get(transcript_id, SimpleNamespace(id=user_id))
        from app.modules.billing.policy import MAX_TEXT_CHARS
        if len(data["content_text"]) > MAX_TEXT_CHARS:
            raise AppException("TRANSCRIPT_TOO_LONG", "首版单次文稿最多 60000 字符", 413)
        if not data["content_text"].strip():
            raise AppException("TRANSCRIPT_EMPTY", "文稿内容为空", 422)
        from app.modules.video.repository import VideoRepository
        metadata = await database_call(VideoRepository(self.transcripts.sessions).history_metadata, [data["video_id"]])
        return {"source_kind": "transcript", "transcript_id": str(data["id"]), "video_id": str(data["video_id"]),
            "transcript_content_hash": data["content_hash"], "content_version": data["content_version"],
            "title": metadata[data["video_id"]]["title"], "duration_seconds": 0}


class TranscriptionPipeline:
    """Acquire a transcript within an existing Celery task; never wait on a Solo child task."""
    def __init__(self, tasks, sessions, storage):
        self.tasks, self.sessions, self.storage = tasks, sessions, storage

    async def acquire(self, task_id, on_progress=None):
        task = await database_call(self.tasks.repository.get, task_id)
        if task.input_payload.get("source_kind") == "transcript":
            if await self.tasks.is_cancel_requested(task_id):
                raise AppException("TASK_CANCELLED", "任务已取消", 409)
            data = await TranscriptService(None, self.tasks, TranscriptRepository(self.sessions)).get(
                UUID(task.input_payload["transcript_id"]), SimpleNamespace(id=task.user_id) if task.user_id else None)
            if (data["video_id"] != task.video_id or data["content_hash"] != task.input_payload["transcript_content_hash"]):
                raise AppException("TRANSCRIPT_SOURCE_MISMATCH", "任务文稿来源发生变化", 409)
            from app.modules.transcript.repository import TranscriptRecord
            return TranscriptRecord(**data)

        from app.infrastructure.process_runner import ProcessRunner
        from app.modules.media.audio import AudioAcquirer
        from app.modules.media.repository import MediaRepository
        from app.modules.transcript.providers import get_asr_provider
        from app.modules.transcript.subtitles import YtDlpSubtitleProvider
        from app.modules.transcript.worker import execute_transcription

        media = MediaRepository(self.sessions)
        return await execute_transcription(task_id, self.tasks, TranscriptRepository(self.sessions), media, self.storage,
            YtDlpSubtitleProvider(ProcessRunner(settings.SUBTITLE_TIMEOUT_SECONDS, 6 * 1024 * 1024)),
            AudioAcquirer(media, self.storage, ProcessRunner(settings.DOWNLOAD_TIMEOUT_SECONDS, settings.MAX_FILE_SIZE_MB * 1024 * 1024)),
            get_asr_provider(), intermediate=True, intermediate_progress=on_progress)
