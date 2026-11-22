"""Read-only, credential-free timeout diagnostics for the API/Worker environment."""
import argparse
from datetime import timezone
import json
from uuid import UUID

from app.core.config import settings


TIMEOUT_FIELDS = (
    "DOWNLOAD_TIMEOUT_SECONDS", "DOWNLOAD_RENDER_TIMEOUT_SECONDS", "SUBTITLE_TIMEOUT_SECONDS", "ASR_TOTAL_TIMEOUT_SECONDS",
    "ASR_REQUEST_TIMEOUT_SECONDS", "ASR_PREPROCESS_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS",
    "LLM_STAGE_TIMEOUT_SECONDS", "AI_TASK_TIMEOUT_SECONDS", "CLOUD_CONNECT_TIMEOUT_SECONDS",
    "CLOUD_READ_TIMEOUT_SECONDS", "CLOUD_WRITE_TIMEOUT_SECONDS",
)


def timeout_report(task=None):
    report = {"effective_seconds": {key: getattr(settings, key) for key in TIMEOUT_FIELDS}, "notes": []}
    if settings.AI_TASK_TIMEOUT_SECONDS <= settings.ASR_TOTAL_TIMEOUT_SECONDS:
        report["notes"].append("AI_TASK_BUDGET_LIMITS_ASR: AI 外层时限先于完整 ASR 阶段预算到期")
    if settings.LLM_STAGE_TIMEOUT_SECONDS > settings.AI_TASK_TIMEOUT_SECONDS:
        report["notes"].append("LLM_STAGE_USES_REMAINING_AI_BUDGET: 模型阶段受 AI 整体剩余时限约束")
    if task is not None:
        def aware(value):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        duration = None
        if task.started_at and task.finished_at:
            duration = max(0, (aware(task.finished_at) - aware(task.started_at)).total_seconds())
        report["task"] = {"task_id": str(task.id), "task_type": task.task_type,
                          "status": task.status, "progress": task.progress, "error_code": task.error_code,
                          "duration_seconds": duration}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=UUID, help="Read one task's status through TaskRepository")
    args = parser.parse_args()
    task = None
    if args.task_id:
        from sqlalchemy.exc import SQLAlchemyError
        from app.core.exceptions import AppException
        from app.infrastructure.database.session import SessionLocal
        from app.modules.tasks.repository import TaskRepository
        try:
            task = TaskRepository(SessionLocal).get(args.task_id)
        except (AppException, SQLAlchemyError):
            print(json.dumps({"error": "TASK_DIAGNOSTIC_UNAVAILABLE"}))
            raise SystemExit(1) from None
    print(json.dumps(timeout_report(task), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
