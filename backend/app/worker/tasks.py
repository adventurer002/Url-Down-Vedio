from backend.app.worker.celery_app import celery_app


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.cleanup_expired_files",
    soft_time_limit=300,
    time_limit=600,
)
def cleanup_expired_files() -> str:
    return "ok"


@celery_app.task(  # type: ignore[untyped-decorator]
    name="backend.app.worker.tasks.expire_subscriptions",
    soft_time_limit=300,
    time_limit=600,
)
def expire_subscriptions() -> str:
    return "ok"
