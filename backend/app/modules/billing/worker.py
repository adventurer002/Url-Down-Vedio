"""Route confirmed operations through the existing Celery workers."""
import asyncio
from uuid import UUID

from app.infrastructure.database.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.modules.billing.service import resume_queued
from app.modules.tasks.repository import TaskRepository


@celery_app.task(name="billing.execute")
def execute(task_id):
    tasks = TaskRepository(SessionLocal)
    identifier = UUID(task_id)
    task = tasks.get(identifier)
    try:
        if task.input_payload.get("reuse_result"):
            if tasks.start(identifier):
                return tasks.complete_with(identifier, task.input_payload["reuse_result"], lambda session: None).status
            return tasks.get(identifier).status
        if task.task_type == "ai_question":
            from app.modules.knowledge.worker import question_task
            return question_task.run(task_id)
        if task.task_type == "download":
            from app.modules.download.worker import download_task
            return download_task.run(task_id)
        if task.task_type == "audio_extraction":
            from app.modules.media.worker import extract_audio_task
            return extract_audio_task.run(task_id)
        if task.task_type == "transcription":
            from app.modules.transcript.worker import transcription_task
            return transcription_task.run(task_id)
        from app.modules.ai.worker import ai_task
        return ai_task.run(task_id)
    finally:
        if tasks.get(identifier).status in {"completed", "failed", "cancelled", "expired"}:
            asyncio.run(resume_queued(tasks, task.user_id))
