from dataclasses import asdict
from uuid import UUID

from app.core.config import settings
from app.core.exceptions import AppException
from app.modules.tasks.service import TaskService, database_call
from app.modules.transcript.repository import TranscriptRepository
from app.modules.video.resolver import VideoResolver


class TranscriptService:
    def __init__(self, resolver: VideoResolver, tasks: TaskService, transcripts: TranscriptRepository):
        self.resolver, self.tasks, self.transcripts = resolver, tasks, transcripts

    async def create(self, url: str, language: str | None):
        payload = await self.resolver.resolve(url)
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


class TranscriptionPipeline:
    """Acquire a transcript within an existing Celery task; never wait on a Solo child task."""
    def __init__(self, tasks, sessions, storage):
        self.tasks, self.sessions, self.storage = tasks, sessions, storage

    async def acquire(self, task_id, on_progress=None):
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
