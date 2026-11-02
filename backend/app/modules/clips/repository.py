from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppException
from app.modules.clips.models import ClipExport, ClipExportItem, ClipPlan, ClipTimeline
from app.modules.tasks.repository import TaskRepository


def plan_record(item):
    return {"id": item.id, "task_id": item.task_id, "video_id": item.video_id,
            "transcript_id": item.transcript_id, "revision": item.revision,
            "cache_key": item.cache_key, **item.content, "created_at": item.created_at}


class ClipRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def plan(self, user_id, identifier):
        with self.sessions() as session:
            item = session.scalar(select(ClipPlan).where(ClipPlan.id == identifier, ClipPlan.user_id == user_id,
                ClipPlan.task_id.in_(TaskRepository.completed_owned_ids(user_id))))
            if item is None:
                raise AppException("CLIP_PLAN_NOT_FOUND", "切片方案不存在", 404)
            return plan_record(item)

    def cached_plan(self, user_id, key):
        with self.sessions() as session:
            item = session.scalar(select(ClipPlan).where(ClipPlan.user_id == user_id, ClipPlan.cache_key == key,
                ClipPlan.task_id.in_(TaskRepository.completed_owned_ids(user_id))))
            return plan_record(item) if item else None

    def store_plan(self, session, task, transcript, key, content):
        item = session.scalar(select(ClipPlan).where(ClipPlan.user_id == task.user_id, ClipPlan.cache_key == key))
        if item:
            return item.id
        try:
            with session.begin_nested():
                item = ClipPlan(id=uuid4(), user_id=task.user_id, task_id=task.id, video_id=task.video_id,
                    transcript_id=transcript.id, cache_key=key, revision=1, content=content)
                session.add(item)
                session.flush()
                return item.id
        except IntegrityError:
            item = session.scalar(select(ClipPlan).where(ClipPlan.user_id == task.user_id, ClipPlan.cache_key == key))
            if item is None:
                raise
            return item.id

    def cached_timeline(self, user_id, key):
        with self.sessions() as session:
            item = session.scalar(select(ClipTimeline).where(ClipTimeline.user_id == user_id, ClipTimeline.cache_key == key))
            return item.content if item else None

    def timelines_for_keys(self, user_id, keys):
        with self.sessions() as session:
            return {item.cache_key: item.content for item in session.scalars(select(ClipTimeline).where(
                ClipTimeline.user_id == user_id, ClipTimeline.cache_key.in_(keys)))}

    def candidate_timelines(self, user_id, plan_id, ids, model, base, version):
        with self.sessions() as session:
            return [item.content for item in session.scalars(select(ClipTimeline).where(
                ClipTimeline.user_id == user_id, ClipTimeline.plan_id == plan_id,
                ClipTimeline.content["kind"].as_string() == "candidate",
                ClipTimeline.content["item"]["candidate_id"].as_string().in_(ids),
                ClipTimeline.content["provider_model"].as_string() == model,
                ClipTimeline.content["provider_base"].as_string() == base,
                ClipTimeline.content["version"].as_string() == version
            ).order_by(ClipTimeline.created_at.desc()).limit(256))]

    def assets_for_keys(self, user_id, keys, instant):
        from app.modules.media.repository import MediaRepository
        with self.sessions() as session:
            result = {}
            for key in keys:  # At most five keys; bound history per asset, not globally across all keys.
                result[key] = [item.content for item in session.scalars(select(ClipExportItem).join(ClipExport).where(
                    ClipExport.user_id == user_id, ClipExport.task_id.in_(TaskRepository.completed_owned_ids(user_id)),
                    ClipExportItem.asset_key == key,
                    ClipExportItem.file_id.in_(MediaRepository.available_ids_query(instant))
                ).order_by(ClipExport.created_at.desc()).limit(10))]
            return result

    def store_timeline(self, session, user_id, plan_id, key, content):
        item = session.scalar(select(ClipTimeline).where(ClipTimeline.user_id == user_id, ClipTimeline.cache_key == key))
        if item is None:
            session.add(ClipTimeline(user_id=user_id, plan_id=plan_id, cache_key=key, content=content))
            session.flush()

    def exports_for_key(self, user_id, key):
        with self.sessions() as session:
            return [{"id": item.id, "task_id": item.task_id, **item.content}
                    for item in session.scalars(select(ClipExport).where(
                        ClipExport.user_id == user_id, ClipExport.cache_key == key,
                        ClipExport.task_id.in_(TaskRepository.completed_owned_ids(user_id))).order_by(ClipExport.created_at.desc()).limit(10))]

    def export(self, user_id, identifier):
        with self.sessions() as session:
            item = session.scalar(select(ClipExport).where(ClipExport.id == identifier, ClipExport.user_id == user_id,
                ClipExport.task_id.in_(TaskRepository.completed_owned_ids(user_id))))
            if item is None:
                raise AppException("CLIP_EXPORT_NOT_FOUND", "切片成品不存在", 404)
            return {"id": item.id, "task_id": item.task_id, "plan_id": item.plan_id,
                    **item.content, "created_at": item.created_at}

    def register_export(self, session, task, identifier, content):
        item = ClipExport(id=identifier, user_id=task.user_id, task_id=task.id,
            plan_id=UUID(task.input_payload["plan_id"]), cache_key=task.input_payload["export_key"], content=content)
        session.add(item)
        session.flush()
        for position, data in enumerate(content["items"]):
            session.add(ClipExportItem(export_id=identifier, file_id=UUID(data["file_id"]), position=position, content=data, asset_key=data.get("asset_key")))
        session.flush()

    def can_read_file(self, user_id, file_ids):
        if user_id is None:
            return False
        with self.sessions() as session:
            return session.scalar(select(ClipExportItem.id).join(ClipExport).where(
                ClipExport.user_id == user_id, ClipExport.task_id.in_(TaskRepository.completed_owned_ids(user_id)),
                ClipExportItem.file_id.in_(file_ids)).limit(1)) is not None
