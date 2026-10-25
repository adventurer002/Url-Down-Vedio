from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from app.core.logging import install_log_redaction

@after_setup_logger.connect
@after_setup_task_logger.connect
def protect_worker_logs(**kwargs):
    install_log_redaction()


from app.core.config import settings

celery_app = Celery(
    "videohub", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND,
    include=["app.modules.tasks.worker", "app.modules.download.worker", "app.modules.media.worker", "app.modules.transcript.worker", "app.modules.ai.worker", "app.modules.billing.worker", "app.modules.knowledge.worker"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    broker_connection_timeout=3,
    broker_connection_max_retries=3,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    task_publish_retry=True,
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
    redis_socket_connect_timeout=3,
    redis_socket_timeout=3,
    redis_retry_on_timeout=False,
    result_backend_transport_options={"retry_policy": {"max_retries": 2, "timeout": 3}},
    result_expires=3600,
    worker_prefetch_multiplier=1,
)


@celery_app.task(name="system.ping")
def system_ping() -> dict[str, bool]:
    return {"pong": True}
