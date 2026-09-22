import sentry_sdk
from celery import Celery

from backend.app.core.config import settings

if settings.sentry_dsn:
    sentry_sdk.init(dsn=str(settings.sentry_dsn))

celery_app = Celery("downvedio", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={"backend.app.worker.tasks.*": {"queue": "default"}},
    beat_schedule={
        "cleanup_expired_files": {"task": "backend.app.worker.tasks.cleanup_expired_files", "schedule": 3600.0},
        "expire_subscriptions": {"task": "backend.app.worker.tasks.expire_subscriptions", "schedule": 86400.0},
    },
)
