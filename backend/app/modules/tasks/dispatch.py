"""Route new work and recovery through the same Celery queues."""
from app.core.config import settings
from app.infrastructure.queue.celery_app import celery_app

PHASE_QUEUES = {"download": "media", "render": "render", "prepare": "media", "asr": "asr", "llm": "llm"}
LEGACY_QUEUES = {
    "download": "media", "audio_extraction": "media", "transcription": "asr",
    "ai_summary": "llm", "ai_mindmap": "llm", "ai_question": "llm",
    "ai_clip_plan": "asr", "clips_export": "render", "media_probe": "control",
}


def first_phase(task_type, payload):
    if task_type == "download":
        return "download"
    if task_type == "transcription":
        return "prepare"
    if task_type in {"ai_summary", "ai_mindmap"} and (
        not payload.get("reuse_result") or payload.get("source_kind") == "transcript"
    ):
        return "llm" if payload.get("source_kind") == "transcript" else "prepare"
    return None


def enqueue(repository, task, dispatcher):
    if task.phase:
        if not repository.reserve_phase_dispatch(task.id, task.phase):
            return False
        celery_app.send_task("tasks.execute_phase", args=[str(task.id), task.phase],
                             queue=PHASE_QUEUES[task.phase])
        return True
    queue = LEGACY_QUEUES.get(task.task_type) if settings.STAGED_TASKS_ENABLED else None
    if queue and hasattr(dispatcher, "enqueue_routed"):
        dispatcher.enqueue_routed(task.id, queue)
    else:
        dispatcher.enqueue(task.id)
    return True


def send(name, task_id, queue=None):
    options = {"queue": queue} if queue else {}
    celery_app.send_task(name, args=[str(task_id)], task_id=str(task_id), **options)
