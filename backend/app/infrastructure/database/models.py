"""Register module-owned models for SQLAlchemy foreign keys and Alembic."""

from app.modules.media.models import MediaFile
from app.modules.tasks.models import Task
from app.modules.video.models import Video
from app.modules.transcript.models import Transcript
from app.modules.ai.models import AIResult

from app.modules.auth.models import User

__all__ = ["Video", "Task", "MediaFile", "Transcript", "AIResult", "User"]
