"""Bounded reuse of completed model calls within the same user and source scope."""
from contextvars import ContextVar
from datetime import timedelta
import hashlib
import json

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.modules.tasks.models import TaskCheckpoint
from app.modules.tasks.repository import utcnow
from app.modules.tasks.service import database_call

active_checkpoints = ContextVar("active_checkpoints", default=None)
MAX_VALUE_BYTES = 2 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Checkpoints:
    def __init__(self, sessions, task):
        self.sessions = sessions
        payload = task.input_payload
        self.scope = [
            str(task.user_id or task.id), str(task.video_id), payload.get("source_kind"),
            payload.get("content_sha256"), payload.get("content_version"),
        ]

    def key(self, version, inputs):
        return digest([self.scope, version, inputs])

    async def get(self, key):
        return await database_call(self._get, key)

    def _get(self, key):
        with self.sessions() as session:
            return session.scalar(
                TaskCheckpoint.__table__.select()
                .with_only_columns(TaskCheckpoint.value)
                .where(TaskCheckpoint.key == key, TaskCheckpoint.expires_at > utcnow())
            )

    async def put(self, key, value):
        if len(json.dumps(value, ensure_ascii=False).encode()) <= MAX_VALUE_BYTES:
            await database_call(self._put, key, value)

    def _put(self, key, value):
        now = utcnow()
        expires = now + timedelta(hours=24)
        try:
            with self.sessions.begin() as session:
                changed = session.execute(update(TaskCheckpoint).where(
                    TaskCheckpoint.key == key, TaskCheckpoint.expires_at <= now
                ).values(value=value, expires_at=expires)).rowcount
                if changed or session.get(TaskCheckpoint, key) is not None:
                    return
                session.add(TaskCheckpoint(key=key, value=value, expires_at=expires))
        except IntegrityError:
            # Another worker stored the same result first.
            pass

    @staticmethod
    def purge(sessions):
        with sessions.begin() as session:
            return session.execute(delete(TaskCheckpoint).where(
                TaskCheckpoint.expires_at <= utcnow()
            )).rowcount
