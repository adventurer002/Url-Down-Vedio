"""AI chain: audio_extract -> transcribe -> summarize -> mindmap.

Each step is idempotent per (video, task_type): a non-terminal task is
reused, an existing output is returned without re-execution.
"""

import json
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.errors import AppError
from backend.app.db.models import (
    MediaFile,
    MindMap,
    ProcessingTask,
    Summary,
    Transcript,
    UsageRecord,
    Video,
)
from backend.app.providers.asr import ASRError, ASRProvider, TranscriptResult
from backend.app.providers.downloader import DownloadCancelled
from backend.app.providers.ffmpeg import FFmpegCancelled, extract_audio
from backend.app.providers.llm import ChatMessage, LLMProvider
from backend.app.providers.storage import StorageProvider, build_key
from backend.app.services import prompts
from backend.app.services.progress import ProgressReporter

NON_TERMINAL = ("queued", "running")


async def find_existing_output(
    session: AsyncSession, video_id: uuid.UUID, task_type: str
) -> uuid.UUID | None:
    if task_type == "audio_extract":
        row = (
            await session.execute(
                select(MediaFile.id)
                .where(
                    MediaFile.video_id == video_id,
                    MediaFile.kind == "audio",
                    MediaFile.deleted_at.is_(None),
                )
                .order_by(MediaFile.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row
    if task_type == "transcribe":
        return (
            await session.execute(
                select(Transcript.id)
                .where(Transcript.video_id == video_id)
                .order_by(Transcript.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if task_type == "summarize":
        return (
            await session.execute(
                select(Summary.id)
                .where(Summary.video_id == video_id)
                .order_by(Summary.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if task_type == "mindmap":
        return (
            await session.execute(
                select(MindMap.id)
                .where(MindMap.video_id == video_id)
                .order_by(MindMap.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return None


async def get_or_create_ai_task(
    session: AsyncSession,
    user_id: uuid.UUID,
    video_id: uuid.UUID,
    task_type: str,
    celery_delay: Callable[[str], str | None],
) -> tuple[ProcessingTask, bool, uuid.UUID | None]:
    """Returns (task, created, existing_output_id)."""
    video = await session.get(Video, video_id)
    if video is None or (video.user_id is not None and video.user_id != user_id):
        raise AppError("not_found", "视频不存在")
    live = (
        await session.execute(
            select(ProcessingTask)
            .where(
                ProcessingTask.video_id == video_id,
                ProcessingTask.task_type == task_type,
                ProcessingTask.status.in_(NON_TERMINAL),
            )
            .order_by(ProcessingTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if live is not None:
        return live, False, None
    output_id = await find_existing_output(session, video_id, task_type)
    if output_id is not None:
        dummy = ProcessingTask(
            user_id=user_id,
            video_id=video_id,
            task_type=task_type,
            status="completed",
            celery_task_id="",
            output_id=output_id,
        )
        return dummy, False, output_id
    task = ProcessingTask(
        user_id=user_id,
        video_id=video_id,
        task_type=task_type,
        status="queued",
        celery_task_id="",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    celery_id = celery_delay(str(task.id))
    if celery_id:
        task.celery_task_id = celery_id
        await session.commit()
        await session.refresh(task)
    return task, True, None


async def _record_usage(
    session: AsyncSession,
    user_id: uuid.UUID,
    action: str,
    ref_id: uuid.UUID | None,
    llm_input_tokens: int = 0,
    llm_output_tokens: int = 0,
    asr_seconds: int = 0,
    storage_bytes: int = 0,
) -> None:
    session.add(
        UsageRecord(
            user_id=user_id,
            action=action,
            ref_id=ref_id,
            llm_input_tokens=llm_input_tokens,
            llm_output_tokens=llm_output_tokens,
            asr_seconds=asr_seconds,
            storage_bytes=storage_bytes,
            cost_cents=0,
        )
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def _latest_video_media(session: AsyncSession, video_id: uuid.UUID) -> MediaFile | None:
    return (
        await session.execute(
            select(MediaFile)
            .where(
                MediaFile.video_id == video_id,
                MediaFile.kind == "video",
                MediaFile.deleted_at.is_(None),
            )
            .order_by(MediaFile.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_audio_media(session: AsyncSession, video_id: uuid.UUID) -> MediaFile | None:
    return (
        await session.execute(
            select(MediaFile)
            .where(
                MediaFile.video_id == video_id,
                MediaFile.kind == "audio",
                MediaFile.deleted_at.is_(None),
            )
            .order_by(MediaFile.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _materialize(
    storage: StorageProvider, key: str, work_dir: Path, name: str
) -> Path:
    dest = work_dir / name
    local = await storage.open_local(key)
    if local is not None:
        dest.write_bytes(local.read_bytes())
        return dest
    await storage.download_to(key, dest)
    return dest


async def mark_failed(
    session: AsyncSession, task_id: uuid.UUID, code: str, message: str
) -> None:
    task = await session.get(ProcessingTask, task_id)
    if task is None:
        return
    task.status = "failed"
    task.error_code = code
    task.error_message = message
    task.finished_at = _now()
    await session.commit()


async def run_audio_extract(
    session: AsyncSession,
    task: ProcessingTask,
    storage: StorageProvider,
    work_dir: Path,
    should_cancel: Callable[[], bool],
    reporter: ProgressReporter | None = None,
) -> MediaFile:
    video = await session.get(Video, task.video_id)
    if video is None:
        raise AppError("not_found", "视频不存在")
    source = await _latest_video_media(session, task.video_id)
    if source is None:
        raise AppError("prerequisite_missing", "还没有可用的视频文件，请先下载")
    local_src = await _materialize(storage, source.storage_key, work_dir, "source.mp4")
    audio_path = work_dir / "audio.mp3"
    try:
        extract_audio(local_src, audio_path, should_cancel)
    except FFmpegCancelled:
        raise DownloadCancelled()
    user_part = str(task.user_id)
    key = build_key(settings.app_env, user_part, "audio", ".mp3")
    await storage.upload(audio_path, key)
    media = MediaFile(
        owner_user_id=task.user_id,
        task_id=None,
        video_id=task.video_id,
        kind="audio",
        format="mp3",
        size_bytes=audio_path.stat().st_size,
        duration_seconds=video.duration_seconds,
        storage_backend=settings.storage_backend,
        storage_key=key,
        expires_at=_now() + timedelta(hours=settings.file_ttl_hours_free),
    )
    session.add(media)
    await session.flush()
    await _record_usage(
        session, task.user_id, "audio_extract", task.id,
        storage_bytes=media.size_bytes,
    )
    return media


async def run_transcribe(
    session: AsyncSession,
    task: ProcessingTask,
    storage: StorageProvider,
    asr: ASRProvider,
    work_dir: Path,
    should_cancel: Callable[[], bool],
    reporter: ProgressReporter | None = None,
) -> TranscriptResult:
    audio = await _latest_audio_media(session, task.video_id)
    if audio is None:
        audio = await run_audio_extract(
            session, task, storage, work_dir, should_cancel, reporter
        )
        await session.flush()
    local_audio = await _materialize(storage, audio.storage_key, work_dir, "audio.mp3")
    if should_cancel():
        raise DownloadCancelled()
    try:
        result = await asr.transcribe(str(local_audio))
    except ASRError as e:
        raise AppError("download_failed", str(e), {"error_code": "asr_error"})
    transcript = Transcript(
        video_id=task.video_id,
        task_id=None,
        language=result.language,
        text=result.text,
        segments=[{"start": s.start, "end": s.end, "text": s.text} for s in result.segments],
        asr_engine=result.engine,
        duration_seconds=result.duration_seconds,
    )
    session.add(transcript)
    await session.flush()
    await _record_usage(
        session, task.user_id, "transcribe", task.id,
        asr_seconds=result.duration_seconds,
    )
    return result


def _parse_summary_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AppError("download_failed", "AI 返回格式异常", {"error_code": "llm_error"})
    if not isinstance(data, dict) or "summary" not in data:
        raise AppError("download_failed", "AI 返回缺少总结字段", {"error_code": "llm_error"})
    data.setdefault("key_points", [])
    data.setdefault("chapters", [])
    data.setdefault("keywords", [])
    return data


async def run_summarize(
    session: AsyncSession,
    task: ProcessingTask,
    llm: LLMProvider,
) -> Summary:
    transcript = (
        await session.execute(
            select(Transcript)
            .where(Transcript.video_id == task.video_id)
            .order_by(Transcript.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if transcript is None:
        raise AppError("prerequisite_missing", "请先完成转写")
    video = await session.get(Video, task.video_id)
    title = video.title if video else ""
    result = await llm.chat(
            [
                ChatMessage(role="system", content=prompts.SUMMARIZE_SYSTEM),
                ChatMessage(
                    role="user",
                    content=prompts.SUMMARIZE_USER.format(
                        title=title, transcript=transcript.text[:12000]
                    ),
                ),
            ],
        max_tokens=2048,
        temperature=0.3,
    )
    data = _parse_summary_json(result.text)
    host = urlsplit(settings.llm_base_url).hostname or "llm"
    summary = Summary(
        transcript_id=transcript.id,
        video_id=task.video_id,
        provider=host.split(".")[0],
        model=result.model or llm.model_name,
        summary=str(data["summary"]),
        key_points=list(data["key_points"]),
        chapters=list(data["chapters"]),
        keywords=list(data["keywords"]),
        prompt_version=settings.llm_prompt_version,
    )
    session.add(summary)
    await session.flush()
    await _record_usage(
        session, task.user_id, "summarize", task.id,
        llm_input_tokens=result.input_tokens,
        llm_output_tokens=result.output_tokens,
    )
    return summary


async def run_mindmap(
    session: AsyncSession,
    task: ProcessingTask,
    llm: LLMProvider,
) -> MindMap:
    summary = (
        await session.execute(
            select(Summary)
            .where(Summary.video_id == task.video_id)
            .order_by(Summary.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if summary is None:
        raise AppError("prerequisite_missing", "请先生成总结")
    video = await session.get(Video, task.video_id)
    title = video.title if video else ""
    result = await llm.chat(
            [
                ChatMessage(role="system", content=prompts.MINDMAP_SYSTEM),
                ChatMessage(
                    role="user",
                    content=prompts.MINDMAP_USER.format(
                        title=title,
                        summary=summary.summary[:6000],
                        key_points="\n".join(f"- {k}" for k in summary.key_points),
                    ),
                ),
            ],
        max_tokens=2048,
        temperature=0.3,
    )
    mindmap = MindMap(
        summary_id=summary.id,
        video_id=task.video_id,
        markdown=result.text.strip(),
    )
    session.add(mindmap)
    await session.flush()
    await _record_usage(
        session, task.user_id, "mindmap", task.id,
        llm_input_tokens=result.input_tokens,
        llm_output_tokens=result.output_tokens,
    )
    return mindmap


def work_dir_for(root: Path, task_id: uuid.UUID) -> Path:
    d = root / f"ai-{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
