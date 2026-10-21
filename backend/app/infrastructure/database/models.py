"""Register module-owned models for SQLAlchemy foreign keys and Alembic."""

from app.modules.media.models import MediaFile
from app.modules.tasks.models import Task
from app.modules.video.models import Video

__all__ = ["Video", "Task", "MediaFile"]
