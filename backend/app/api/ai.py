"""AI endpoints: member-only triggers + outputs. One task per (video, type)."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.auth_deps import require_user
from backend.app.api.deps import get_session
from backend.app.core.errors import AppError
from backend.app.db.models import (
    MindMap,
    ProcessingTask,
    Summary,
    Transcript,
    UsageRecord,
    User,
    Video,
)
from backend.app.schemas.ai import (
    Chapter,
    MindMapOut,
    ProcessingTaskOut,
    SummaryOut,
    TranscriptOut,
    TranscriptSegment,
    TriggerResponse,
    UsageEntry,
    UsageList,
)
from backend.app.services import ai_service as ai
from backend.app.services import permission as perm

router = APIRouter(prefix="/api/v1")

FEATURE_OF = {
    "audio_extract": "audio_extract",
    "transcribe": "transcribe",
    "summarize": "summarize",
    "mindmap": "mindmap",
}


async def _gate(session: AsyncSession, user: User, task_type: str) -> None:
    result = await perm.check_feature(session, user.id, FEATURE_OF[task_type])
    if not result.allowed:
        raise AppError(result.code, result.message)


def _task_out(task: ProcessingTask) -> ProcessingTaskOut:
    return ProcessingTaskOut(
        id=task.id,
        video_id=task.video_id,
        task_type=task.task_type,
        status=task.status,
        output_id=task.output_id,
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=task.created_at,
        finished_at=task.finished_at,
    )


async def _trigger(
    session: AsyncSession, user: User, video_id: uuid.UUID, task_type: str
) -> TriggerResponse:
    from backend.app.worker import ai_tasks

    await _gate(session, user, task_type)
    task, created, output_id = await ai.get_or_create_ai_task(
        session,
        user.id,
        video_id,
        task_type,
        lambda tid: ai_tasks.ai_process.delay(tid).id,
    )
    if output_id is not None:
        return TriggerResponse(output_id=output_id, reused=True)
    return TriggerResponse(task_id=task.id, reused=not created)


@router.post("/videos/{video_id}/audio", response_model=TriggerResponse, status_code=202)
async def trigger_audio(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TriggerResponse:
    return await _trigger(session, user, video_id, "audio_extract")


@router.post(
    "/videos/{video_id}/transcribe", response_model=TriggerResponse, status_code=202
)
async def trigger_transcribe(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TriggerResponse:
    return await _trigger(session, user, video_id, "transcribe")


@router.post(
    "/videos/{video_id}/summarize", response_model=TriggerResponse, status_code=202
)
async def trigger_summarize(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TriggerResponse:
    transcript = await ai.find_existing_output(session, video_id, "transcribe")
    if transcript is None:
        raise AppError("prerequisite_missing", "请先完成转写")
    return await _trigger(session, user, video_id, "summarize")


@router.post(
    "/videos/{video_id}/mindmap", response_model=TriggerResponse, status_code=202
)
async def trigger_mindmap(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TriggerResponse:
    summary = await ai.find_existing_output(session, video_id, "summarize")
    if summary is None:
        raise AppError("prerequisite_missing", "请先生成总结")
    return await _trigger(session, user, video_id, "mindmap")


@router.get("/tasks/{task_id}", response_model=ProcessingTaskOut)
async def get_ai_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> ProcessingTaskOut:
    task = await session.get(ProcessingTask, task_id)
    if task is None or task.user_id != user.id:
        raise AppError("not_found", "任务不存在")
    return _task_out(task)


async def _owned_video(session: AsyncSession, user: User, video_id: uuid.UUID) -> Video:
    video = await session.get(Video, video_id)
    if video is None or (video.user_id is not None and video.user_id != user.id):
        raise AppError("not_found", "视频不存在")
    return video


@router.get("/videos/{video_id}/transcript", response_model=TranscriptOut)
async def get_transcript(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> TranscriptOut:
    await _gate(session, user, "transcribe")
    await _owned_video(session, user, video_id)
    row = (
        await session.execute(
            select(Transcript)
            .where(Transcript.video_id == video_id)
            .order_by(Transcript.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("not_found", "暂无转写结果")
    return TranscriptOut(
        id=row.id,
        language=row.language,
        text=row.text,
        segments=[TranscriptSegment(**s) for s in row.segments],
    )


@router.get("/videos/{video_id}/summary", response_model=SummaryOut)
async def get_summary(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> SummaryOut:
    await _gate(session, user, "summarize")
    await _owned_video(session, user, video_id)
    row = (
        await session.execute(
            select(Summary)
            .where(Summary.video_id == video_id)
            .order_by(Summary.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("not_found", "暂无总结结果")
    return SummaryOut(
        id=row.id,
        provider=row.provider,
        model=row.model,
        summary=row.summary,
        key_points=list(row.key_points),
        chapters=[Chapter(**c) for c in row.chapters],
        keywords=list(row.keywords),
    )


@router.get("/videos/{video_id}/mindmap", response_model=MindMapOut)
async def get_mindmap(
    video_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> MindMapOut:
    await _gate(session, user, "mindmap")
    await _owned_video(session, user, video_id)
    row = (
        await session.execute(
            select(MindMap)
            .where(MindMap.video_id == video_id)
            .order_by(MindMap.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("not_found", "暂无思维导图")
    return MindMapOut(id=row.id, markdown=row.markdown)


@router.get("/usage", response_model=UsageList)
async def list_usage(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> UsageList:
    stmt = (
        select(UsageRecord)
        .where(UsageRecord.user_id == user.id)
        .order_by(UsageRecord.created_at.desc())
    )
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return UsageList(
        items=[
            UsageEntry(
                action=r.action,
                llm_input_tokens=r.llm_input_tokens,
                llm_output_tokens=r.llm_output_tokens,
                asr_seconds=r.asr_seconds,
                storage_bytes=r.storage_bytes,
                cost_cents=r.cost_cents,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
